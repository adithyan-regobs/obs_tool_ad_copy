from typing import Optional, List
from uuid import UUID
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio
import logging
import re

from app.schemas.alert_schemas import CreateAlert
from app.repository.alert_configs_repository import ObsAlertsRepository
from app.domain.validators.obs_alerts_rules import AlertConfigValidationError
from app.domain.factories.obs_alerts_factory import make_alert_configs
from app.core.enum import ObsVendorEnum, IntegrationStatusEnum, DeploymentStatusEnum, WorkflowSourceTableEnum, PRStatusEnum

# GitOps workflow tracking
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.domain.factories.gitops_workflow_detail_factory import make_gitops_workflow_detail

from app.repository.monitoring_policy_defaults_ref_repository import MonitoringPolicyDefaultsRefRepository
from app.repository.monitoring_policy_overrides_mst_repository import MonitoringPolicyOverridesMstRepository
from app.repository.obs_vendor_accounts_mst_repository import ObsVendorAccountsMstRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.datadog_alert_query_ref_repository import DatadogAlertQueryRefRepository
from app.repository.service_dependency_map_repository import ServiceDependencyMapRepository
from app.repository.infrastructuretype_ref_repository import InfrastructureTypeRefRepository
from app.repository.alerttype_ref_repository import AlertTypeRefRepository

from app.integrations.datadog_integration import DatadogIntegration
from app.integrations.github_integration import GitHubIntegration
from app.services.terragrunt_mgmt_service import TerragruntMgmtService
from app.core.config import settings
from app.utils.github_app_token import get_token_for_org
from app.utils.tenant_config import get_tenant_config
import os

logger = logging.getLogger(__name__)

class ObsCreateAlertsService:

    def __init__(self, session: AsyncSession):
        self.session = session
        self.obs_alert_config = ObsAlertsRepository(session)
        self.monitoring_policy_defaults_ref = MonitoringPolicyDefaultsRefRepository(session)
        self.monitoring_policy_overrides_mst = MonitoringPolicyOverridesMstRepository(session)
        self.obs_vendor_accounts_mst_repository = ObsVendorAccountsMstRepository(session)
        self.services_mst_repository = ServicesMstRepository(session)
        self.datadog_query_ref_repository = DatadogAlertQueryRefRepository(session)
        self.service_dependency_map_repository = ServiceDependencyMapRepository(session)
        self.infrastructuretype_ref_repository = InfrastructureTypeRefRepository(session)
        self.alerttype_ref_repository = AlertTypeRefRepository(session)
        # GitOps workflow tracking
        self.gitops_workflow_repository = GitopsWorkflowDetailRepository(session)

    async def _get_existing_service_pr(
        self,
        service_code: str,
        tenant_code: str
    ) -> Optional[dict]:
        """
        Get existing open PR for any alert belonging to a service.

        Used to unify PRs across single and bulk alert endpoints - both endpoints
        will reuse the same PR for a given service instead of creating separate PRs.

        Args:
            service_code: Service code (services_mst.code)
            tenant_code: Tenant code for scoping

        Returns:
            Dict with PR info if found: {git_branch, pr_number, pr_url, workflow_id, commit_sha}
            None if no open PR exists for any alert of this service
        """
        existing_pr = await self.gitops_workflow_repository.get_open_pr_by_service_alerts(
            service_code=service_code,
            tenant_mst_code=tenant_code
        )

        if existing_pr:
            return {
                "git_branch": existing_pr.git_branch,
                "pr_number": existing_pr.pr_number,
                "pr_url": existing_pr.pr_url,
                "workflow_id": existing_pr.id,
                "commit_sha": existing_pr.git_commit_sha
            }
        return None

    async def _verify_pr_status_from_github(
        self,
        pr_number: int,
        git_repository: str = None
    ) -> dict:
        """
        Verify PR status from GitHub API and sync DB if status changed.

        This method checks the actual PR status on GitHub and updates the database
        if the status has changed (e.g., PR was merged or closed externally).

        Args:
            pr_number: GitHub pull request number
            git_repository: Repository in "owner/repo" format. Defaults to settings.datadog_terraform_repo

        Returns:
            Dict with:
                - is_open: True if PR is still open on GitHub
                - pr_was_closed: True if PR was marked as OPEN in DB but is now MERGED/CLOSED
                - github_status: Current status from GitHub ("open", "merged", "closed")
                - pr_status_enum: PRStatusEnum value
        """
        repository = git_repository

        try:
            # Parse owner/repo
            repo_parts = repository.split("/")
            if len(repo_parts) != 2:
                logger.error(f"Invalid repository format: {repository}")
                return {"is_open": True, "pr_was_closed": False, "github_status": "unknown", "pr_status_enum": PRStatusEnum.PR_OPEN}

            owner, repo = repo_parts

            # Get GitHub token via GitHub App installation
            github_token = await get_token_for_org(owner, self.session)

            # Call GitHub API to get PR details
            pr_details = await GitHubIntegration.get_pull_request(
                token=github_token,
                base_url=settings.github_base_url or "https://api.github.com",
                owner=owner,
                repo=repo,
                pr_number=pr_number
            )

            # Determine status from GitHub response
            if pr_details.get("merged"):
                github_status = "merged"
                pr_status_enum = PRStatusEnum.PR_MERGED
            elif pr_details.get("state") == "closed":
                github_status = "closed"
                pr_status_enum = PRStatusEnum.PR_CLOSED
            else:
                github_status = "open"
                pr_status_enum = PRStatusEnum.PR_OPEN

            is_open = (github_status == "open")
            pr_was_closed = not is_open  # If not open, it was closed/merged

            logger.info(f"GitHub PR #{pr_number} status: {github_status}")

            # If PR was closed/merged, update DB to sync status
            if pr_was_closed:
                updated_count = await self.gitops_workflow_repository.update_pr_status_by_pr_and_repository(
                    pr_number=pr_number,
                    git_repository=repository,
                    new_status=pr_status_enum
                )
                if updated_count > 0:
                    logger.info(f"Synced DB: Updated {updated_count} workflow entries for PR #{pr_number} to {github_status}")

            return {
                "is_open": is_open,
                "pr_was_closed": pr_was_closed,
                "github_status": github_status,
                "pr_status_enum": pr_status_enum
            }

        except Exception as e:
            logger.error(f"Failed to verify PR status from GitHub: {e}")
            # On error, assume PR is still open to avoid breaking the flow
            return {"is_open": True, "pr_was_closed": False, "github_status": "unknown", "pr_status_enum": PRStatusEnum.PR_OPEN}

    async def _generate_datadog_terragrunt_hcl(self, monitoring_policy, alert_data: CreateAlert, service, query_template: str) -> str:
        """
        Generate Terragrunt HCL content for Datadog monitor.

        Args:
            monitoring_policy: Monitoring policy from database
            alert_data: CreateAlert schema data
            service: Service model
            query_template: Query template string

        Returns:
            Rendered Terragrunt HCL content with all placeholders substituted
        """
        # Step 1: Render the Datadog query using existing logic
        query = DatadogIntegration.render_query(
            query_template=query_template,
            eval_window=alert_data.eval_window,
            service_name=service.name,  # Using service name for query
            operator=DatadogIntegration.COMPARATOR_MAP.get(alert_data.comparator.value, ">"),
            threshold=alert_data.threshold_value
        )

        # Step 2: Determine monitor type
        monitor_type = DatadogIntegration.SIGNAL_KIND_MAP.get(monitoring_policy.signal_kind.value, "metric alert")

        # Step 3: Build monitor name - combination of service name and monitoring policy name
        # Sanitize monitor name (replace consecutive spaces/hyphens with single hyphen)
        monitor_name = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')

        # Step 4: Build monitor message
        monitor_message = f"Alert triggered for {monitoring_policy.alerttype_ref_code}. Severity: {alert_data.severity.value}"

        # Step 4b: Generate identifier - combination of service name and monitoring policy name
        # Sanitize identifier to match monitor_name format (consistent formatting)
        identifier = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')

        # Step 5: Load Terragrunt template
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "templates", "terragrunt", "datadog", "monitor", "terragrunt.hcl"
        )

        with open(template_path, 'r') as f:
            template_content = f.read()

        # Step 6: Substitute all placeholders
        # Get environment from infrastructure relationship or default to "staging"
        environment = service.infrastructure.environments_enum.value if service.infrastructure else "staging"

        # Escape special characters in strings for HCL syntax
        def escape_hcl_string(s: str) -> str:
            """Escape special characters for HCL string values."""
            return (s.replace("\\", "\\\\")      # Backslash first
                     .replace("\"", "\\\"")      # Double quotes
                     .replace("\n", "\\n")       # Newlines
                     .replace("\r", "\\r")       # Carriage returns
                     .replace("\t", "\\t")       # Tabs
                     .replace("$", "$$"))        # Dollar signs (HCL interpolation)

        rendered = template_content.replace("${identifier}", identifier)
        rendered = rendered.replace("${monitor_name}", escape_hcl_string(monitor_name))
        rendered = rendered.replace("${monitor_type}", monitor_type)
        rendered = rendered.replace("${monitor_query}", escape_hcl_string(query))
        rendered = rendered.replace("${monitor_message}", escape_hcl_string(monitor_message))
        rendered = rendered.replace("${threshold_critical}", str(float(alert_data.threshold_value)))
        rendered = rendered.replace("${evaluation_delay}", str(alert_data.for_duration * 60))  # Convert minutes to seconds
        rendered = rendered.replace("${notify_audit}", "true")
        rendered = rendered.replace("${include_tags}", "true")
        rendered = rendered.replace("${resource_kind}", monitoring_policy.infrastructuretype_ref_code)
        rendered = rendered.replace("${severity}", alert_data.severity.value)
        rendered = rendered.replace("${source}", settings.app_name.lower())
        rendered = rendered.replace("${tenant}", service.tenants_mst_code)
        rendered = rendered.replace("${environment}", environment)

        logger.info(f"Generated Terragrunt HCL for monitor: {monitor_name}")
        return rendered 

    async def _generate_datadog_monitor_block(self, monitoring_policy, alert_data: CreateAlert, service, query_template: str) -> str:
        """
        Generate Terragrunt HCL monitor BLOCK (not full file) for Datadog monitor.
        This is used for list-based terragrunt structure where multiple monitors are in one file.

        Args:
            monitoring_policy: Monitoring policy from database
            alert_data: CreateAlert schema data
            service: Service model
            query_template: Query template string

        Returns:
            Rendered monitor block HCL content (just the monitor object, not full file)
        """
        # Step 1: Render the Datadog query using existing logic
        query = DatadogIntegration.render_query(
            query_template=query_template,
            eval_window=alert_data.eval_window,
            service_name=service.name,  # Using service name for query
            operator=DatadogIntegration.COMPARATOR_MAP.get(alert_data.comparator.value, ">"),
            threshold=alert_data.threshold_value
        )

        # Step 2: Determine monitor type
        monitor_type = DatadogIntegration.SIGNAL_KIND_MAP.get(monitoring_policy.signal_kind.value, "metric alert")

        # Step 3: Build monitor name - combination of service name and monitoring policy name
        # Sanitize monitor name (replace consecutive spaces/hyphens with single hyphen)
        # Same format as single alert endpoint for consistency
        monitor_name = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')

        # Step 4: Build monitor message
        monitor_message = f"Alert triggered for {monitoring_policy.alerttype_ref_code}. Severity: {alert_data.severity.value}"

        # Step 5: Generate identifier - combination of service name and monitoring policy name
        # Sanitize identifier to match monitor_name format (consistent formatting)
        # Same format as single alert endpoint for consistency
        identifier = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')

        # Step 6: Load monitor item template (block only, not full file)
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "templates", "terragrunt", "datadog", "monitor", "monitor_item.hcl"
        )

        with open(template_path, 'r') as f:
            template_content = f.read()

        # Step 7: Substitute all placeholders
        # Get environment from infrastructure relationship or default to "staging"
        environment = service.infrastructure.environments_enum.value if service.infrastructure else "staging"

        # Escape special characters in strings for HCL syntax
        def escape_hcl_string(s: str) -> str:
            """Escape special characters for HCL string values."""
            return (s.replace("\\", "\\\\")      # Backslash first
                     .replace("\"", "\\\"")      # Double quotes
                     .replace("\n", "\\n")       # Newlines
                     .replace("\r", "\\r")       # Carriage returns
                     .replace("\t", "\\t")       # Tabs
                     .replace("$", "$$"))        # Dollar signs (HCL interpolation)

        rendered = template_content.replace("${identifier}", identifier)
        rendered = rendered.replace("${monitor_name}", escape_hcl_string(monitor_name))
        rendered = rendered.replace("${monitor_type}", monitor_type)
        rendered = rendered.replace("${monitor_query}", escape_hcl_string(query))
        rendered = rendered.replace("${monitor_message}", escape_hcl_string(monitor_message))
        rendered = rendered.replace("${threshold_critical}", str(float(alert_data.threshold_value)))
        rendered = rendered.replace("${evaluation_delay}", str(alert_data.for_duration * 60))  # Convert minutes to seconds
        rendered = rendered.replace("${notify_audit}", "true")
        rendered = rendered.replace("${include_tags}", "true")
        rendered = rendered.replace("${resource_kind}", monitoring_policy.infrastructuretype_ref_code)
        rendered = rendered.replace("${severity}", alert_data.severity.value)
        rendered = rendered.replace("${source}", settings.app_name.lower())
        rendered = rendered.replace("${tenant}", service.tenants_mst_code)
        rendered = rendered.replace("${environment}", environment)

        logger.info(f"Generated monitor block HCL for: {monitor_name}")
        return rendered

    async def create_or_update_alert(
        self,
        data: CreateAlert,
        user_code: str = None,
        tenant_code: str = None
    ):
        """
        Create or update an alert configuration (upsert operation).
        Identifies existing alerts by services_code + monitoring_policy_code.
        Updates all configurable fields and syncs with vendor platform.

        Args:
            data: Alert creation/update data
            user_code: User code from JWT (optional, used for chat message insertion)
            tenant_code: Tenant code from JWT (optional, used for workflow tracking)

        Returns:
            Dict with alert details and operation type ("created" or "updated")
        """
        # Check if alert already exists
        existing_alert = await self.obs_alert_config.check_alert_exists(
            services_code=data.services_code,
            monitoring_policy_code=data.monitoring_policy_code
        )

        if existing_alert:
            # UPDATE path
            return await self._update_existing_alert(existing_alert, data, user_code)
        else:
            # CREATE path
            result = await self.create_alert(data, user_code)
            result["operation"] = "created"
            return result

    async def _update_existing_alert(self, existing_alert, data: CreateAlert, user_code: str = None):
        """
        Internal method to update an existing alert and sync with vendor.
        """
        try:
            logger.info(f"Updating existing alert - ID: {existing_alert.id}, Code: {existing_alert.code}")

            # Step 2: Validate new data
            logger.debug("Validating update data...")
            AlertConfigValidationError.validate_obs_create_alerts_rule(data)
            logger.debug("Validation passed")

            # Step 3: Get Service Details (for vendor sync payload)
            logger.debug(f"Getting service details for: {data.services_code}")
            service = await self.services_mst_repository.get_by_code(data.services_code)
            if not service:
                raise ValueError(f"Service not found: {data.services_code}")
            logger.debug(f"Service found - App: {service.applications_mst_code}, Tenant: {service.tenants_mst_code}")

            # Step 4: Get Vendor Account
            logger.debug("Getting vendor account...")
            vendor_account = await self.obs_vendor_accounts_mst_repository.get_by_service_hierarchy(
                service_code=data.services_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code
            )
            if not vendor_account:
                raise ValueError(f"No vendor account found for service/app/tenant hierarchy")
            logger.debug(f"Vendor account: {vendor_account.code} ({vendor_account.obs_vendor_enum.value})")

            # Step 5: Update database record
            logger.debug("Updating database record...")
            update_data = {
                "comparator": data.comparator,
                "threshold_value": data.threshold_value,
                "threshold_unit": data.threshold_unit,
                "eval_window": data.eval_window,
                "for_duration": data.for_duration,
                "severity": data.severity,
                "status": data.status,
                "no_data": data.no_data,
                "vendor_status": IntegrationStatusEnum.INITIATED,
                "vendor_last_sync_attempt": datetime.utcnow(),
            }
            updated_alert = await self.obs_alert_config.update(existing_alert, update_data)
            await self.session.commit()
            logger.debug("Database updated")

            # Step 6: Sync Monitor with Vendor (Create or Update)
            vendor_monitor_id = existing_alert.vendor_monitor_id
            vendor_status = IntegrationStatusEnum.INITIATED
            vendor_error = None

            if vendor_monitor_id:
                logger.debug(f"Updating monitor in {vendor_account.obs_vendor_enum.value}...")
            else:
                logger.debug(f"Creating monitor in {vendor_account.obs_vendor_enum.value} (vendor_monitor_id was missing)...")

            try:
                if vendor_account.obs_vendor_enum == ObsVendorEnum.datadog:
                    # Get monitoring policy for payload building
                    monitoring_policy = await self.monitoring_policy_defaults_ref.get_by(code=data.monitoring_policy_code)
                    if not monitoring_policy:
                        raise ValueError(f"Monitoring policy not found: {data.monitoring_policy_code}")

                    # Fetch query template from database
                    query_ref = await self.datadog_query_ref_repository.get_by_infra_alert_signal(
                        infrastructuretype_ref_code=monitoring_policy.infrastructuretype_ref_code,
                        alerttype_ref_code=monitoring_policy.alerttype_ref_code,
                        signal_kind=monitoring_policy.signal_kind
                    )
                    if not query_ref:
                        raise ValueError(
                            f"Query template not found for infrastructure: {monitoring_policy.infrastructuretype_ref_code}, "
                            f"alert: {monitoring_policy.alerttype_ref_code}, signal: {monitoring_policy.signal_kind}"
                        )

                    # Generate monitor vendor reference identifier - combination of service name and monitoring policy name
                    monitor_vendor_reference_identifier = f"{service.name} - {monitoring_policy.name}"
                    logger.debug(f"Monitor vendor reference identifier: {monitor_vendor_reference_identifier}")

                    # Check if Terragrunt mode is enabled
                    tenant_cfg = await get_tenant_config(service.tenants_mst_code, self.session)
                    infra_repo = tenant_cfg.github_infra_repository
                    if settings.datadog_use_terragrunt and infra_repo:
                        if vendor_monitor_id:
                            logger.info(f"Using Terragrunt workflow to UPDATE existing monitor: {vendor_monitor_id}")
                            print(f"\n📋 Updating existing monitor - ID: {vendor_monitor_id}\n")
                        else:
                            logger.info("Using Terragrunt workflow to CREATE monitor (no vendor_monitor_id in DB)")

                        # Generate Terragrunt HCL content (same as create - Terraform will update existing resource)
                        terragrunt_content = await self._generate_datadog_terragrunt_hcl(
                            monitoring_policy=monitoring_policy,
                            alert_data=data,
                            service=service,
                            query_template=query_ref.query_template
                        )

                        # Commit and apply via Terragrunt service
                        terragrunt_service = TerragruntMgmtService()
                        # Get environment from infrastructure relationship or default to "staging"
                        environment = service.infrastructure.environments_enum.value if service.infrastructure else "staging"
                        # Normalize environment for branch name (staging -> stage)
                        normalized_branch = terragrunt_service._normalize_environment_for_path(environment)

                        # Check for existing open PR for this service (unified PR strategy)
                        existing_pr = await self._get_existing_service_pr(
                            service_code=service.code,
                            tenant_code=service.tenants_mst_code
                        )
                        if existing_pr:
                            # Verify PR status from GitHub API before using
                            github_status = await self._verify_pr_status_from_github(
                                pr_number=existing_pr.get("pr_number"),
                                git_repository=infra_repo
                            )
                            if github_status.get("is_open"):
                                logger.info(f"Found existing open PR #{existing_pr.get('pr_number')} for service {service.code} (verified with GitHub)")
                            else:
                                # PR was closed/merged - DB already synced, don't use for diff check
                                logger.info(f"PR #{existing_pr.get('pr_number')} was {github_status.get('github_status')} on GitHub - will create new PR if needed")
                                existing_pr = None  # Clear so we diff check against base branch

                        # ========================================================================
                        # SINGLE ALERT ENDPOINT DISABLED - Use bulk endpoint instead
                        # The commit_terragrunt_file() method no longer supports Datadog monitors.
                        # Use the bulk-create-or-update-alerts endpoint for all alert operations.
                        # ========================================================================
                        raise ValueError(
                            "Single alert endpoint is disabled for Datadog monitors. "
                            "Please use the bulk-create-or-update-alerts endpoint instead."
                        )

                        # terragrunt_result = await terragrunt_service.commit_terragrunt_file(
                        #     terragrunt_content=terragrunt_content,
                        #     tenant=service.tenants_mst_code,
                        #     user_code=user_code,  # User code from JWT
                        #     environment=environment,
                        #     github_repository=settings.datadog_terraform_repo,
                        #     branch_name=normalized_branch,
                        #     resource_type="datadog/monitor",
                        #     service_name=service.name,  # Using service name for folder path
                        #     resource_type_ref=monitoring_policy.infrastructuretype_ref_code,
                        #     existing_monitor_id=vendor_monitor_id,  # Pass existing monitor ID from DB
                        #     product_name=service.application.name if service.application else None,  # Application name as product
                        #     region=None,  # Region handled by environment-based fallback in terragrunt_mgmt_service
                        #     existing_pr_info=existing_pr,  # Pass existing PR info for smart replacement
                        #     skip_if_no_changes=True  # Enable diff checking
                        # )
                        #
                        # # Handle no_changes response
                        # if terragrunt_result.get("status") == "no_changes":
                        #     logger.info("No changes detected - reusing existing PR")
                        #     vendor_status = IntegrationStatusEnum.INITIATED
                        #     if terragrunt_result.get("existing_pr_reused"):
                        #         pr_number = terragrunt_result.get("pr_number")
                        #         pr_url = terragrunt_result.get("pr_url")
                        #         vendor_error = f"No changes - existing PR #{pr_number} is up to date"
                        #     else:
                        #         vendor_error = "No changes detected - content matches base branch"
                        #
                        #     # Update alert with no-change status
                        #     updated_alert.vendor_status = vendor_status
                        #     updated_alert.vendor_error = vendor_error
                        #     await self.session.commit()
                        #
                        #     # Return early for no_changes
                        #     return {
                        #         "status": "success",
                        #         "message": vendor_error,
                        #         "operation": "updated",
                        #         "alert_code": existing_alert.code,
                        #         "pr_number": terragrunt_result.get("pr_number"),
                        #         "pr_url": terragrunt_result.get("pr_url"),
                        #         "existing_pr_reused": terragrunt_result.get("existing_pr_reused", False)
                        #     }
                        #
                        # # Extract monitor ID from Terraform outputs or use existing
                        # terraform_outputs = terragrunt_result.get("terraform_outputs", {})
                        # vendor_monitor_id = terraform_outputs.get("monitor_id") or vendor_monitor_id

                        # Check if PR workflow was used (indicated by pr_number in response)
                        pr_workflow_used = terragrunt_result.get("pr_number") is not None

                        if vendor_monitor_id:
                            vendor_status = IntegrationStatusEnum.SUCCESS
                            logger.info(f"Monitor synced via Terragrunt - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (Terragrunt): {vendor_monitor_id}\n")
                        elif pr_workflow_used:
                            # PR workflow: monitor will be created/updated after PR merge by GitOps
                            vendor_status = IntegrationStatusEnum.INITIATED
                            pr_number = terragrunt_result.get("pr_number")
                            pr_url = terragrunt_result.get("pr_url")
                            vendor_error = f"PR #{pr_number} created - Monitor will be synced after PR merge by GitOps"
                            logger.info(f"PR workflow: {vendor_error}")
                            print(f"\n🔀 PR #{pr_number} created - Monitor sync pending GitOps apply\n")
                            print(f"🔗 PR URL: {pr_url}\n")

                            # Create NEW GitOps workflow tracking record (for updates)
                            try:
                                workflow_data = make_gitops_workflow_detail(
                                    git_repository=infra_repo,
                                    git_branch=terragrunt_result.get("feature_branch"),
                                    git_commit_sha=terragrunt_result.get("commit_sha"),
                                    pr_number=pr_number,
                                    pr_url=pr_url,
                                    tenant_mst_code=service.tenants_mst_code,
                                    user_mst_code=user_code,
                                    workflow_name=f"Alert Update: {service.name} - {monitoring_policy.name}",
                                    transaction_code=existing_alert.code,
                                    table_name=WorkflowSourceTableEnum.ALERT_CONFIG
                                )
                                workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                # If old PR was replaced, update its workflow status to CLOSED
                                old_pr_closed = terragrunt_result.get("old_pr_to_close")
                                if old_pr_closed and existing_pr:
                                    old_workflow_id = existing_pr.get("workflow_id")
                                    if old_workflow_id:
                                        await self.gitops_workflow_repository.update_pr_status(
                                            workflow_id=old_workflow_id,
                                            new_status=PRStatusEnum.PR_CLOSED
                                        )
                                        logger.info(f"Marked old workflow {old_workflow_id} as PR_CLOSED (superseded by PR #{pr_number})")

                                # Replace old workflow link with new one and update status
                                await self.obs_alert_config.link_to_gitops_workflow(
                                    alert_ids=[existing_alert.id],
                                    workflow_id=workflow.id
                                )

                                await self.obs_alert_config.update_creation_status(
                                    alert_id=existing_alert.id,
                                    status=DeploymentStatusEnum.PR_CREATED
                                )

                                # Commit workflow tracking changes
                                await self.session.commit()

                                logger.info(f"GitOps workflow tracking created for update - Workflow ID: {workflow.id}, PR: {pr_number}")
                            except Exception as workflow_error:
                                logger.error(f"Failed to create workflow tracking: {workflow_error}")
                        else:
                            # Direct workflow but no monitor ID - this is an error
                            vendor_status = IntegrationStatusEnum.FAILED
                            vendor_error = "Monitor ID not found in Terraform outputs"
                            logger.error(vendor_error)
                    else:
                        logger.info("Using direct Datadog API workflow")

                        # Build payload
                        payload = DatadogIntegration.build_payload(
                            monitoring_policy=monitoring_policy,
                            alert_data=data,
                            service=service,
                            query_template=query_ref.query_template
                        )
                        logger.debug(f"Payload: {payload}")

                        # Call Datadog API (create or update based on vendor_monitor_id existence)
                        if vendor_monitor_id:
                            # UPDATE existing monitor
                            datadog_response = await asyncio.wait_for(
                                DatadogIntegration.update_monitor(
                                    auth_config=vendor_account.auth_config,
                                    monitor_id=vendor_monitor_id,
                                    payload=payload
                                ),
                                timeout=10.0
                            )
                            logger.info(f"Monitor updated - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (API Update): {vendor_monitor_id}\n")
                        else:
                            # CREATE new monitor (vendor_monitor_id was missing)
                            datadog_response = await asyncio.wait_for(
                                DatadogIntegration.create_monitor(
                                    auth_config=vendor_account.auth_config,
                                    payload=payload
                                ),
                                timeout=10.0
                            )
                            vendor_monitor_id = str(datadog_response.get('id'))
                            logger.info(f"Monitor created - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (API Create): {vendor_monitor_id}\n")

                        vendor_status = IntegrationStatusEnum.SUCCESS
                else:
                    raise ValueError(f"Vendor {vendor_account.obs_vendor_enum.value} not supported")
            except asyncio.TimeoutError:
                vendor_status = IntegrationStatusEnum.TIMEOUT
                vendor_error = "Vendor API timeout after 10 seconds"
                logger.warning(vendor_error)
            except Exception as e:
                vendor_status = IntegrationStatusEnum.FAILED
                vendor_error = str(e)[:500]
                logger.error(f"Vendor error - {vendor_error}")

            # Step 7: Update DB with vendor sync result
            logger.debug("Updating alert with vendor result...")
            updated_alert.vendor_monitor_id = vendor_monitor_id
            updated_alert.vendor_status = vendor_status
            updated_alert.vendor_error = vendor_error
            updated_alert.vendor_status_updated_at = datetime.utcnow()
            updated_alert.vendor_last_sync_attempt = datetime.utcnow()

            # Update monitor_vendor_reference_identifier if datadog
            if vendor_account.obs_vendor_enum == ObsVendorEnum.datadog and 'monitor_vendor_reference_identifier' in locals():
                updated_alert.monitor_vendor_reference_identifier = monitor_vendor_reference_identifier
                logger.debug(f"Updated monitor_vendor_reference_identifier: {monitor_vendor_reference_identifier}")

            await self.session.commit()
            logger.info(f"Updated - Status: {vendor_status.value}, Monitor ID: {vendor_monitor_id}")

            # Step 8: Return simplified response
            response_status = "success" if vendor_status == IntegrationStatusEnum.SUCCESS else "partial"

            # Build message based on vendor status
            if vendor_status == IntegrationStatusEnum.SUCCESS:
                response_message = "Alert updated successfully"
            elif vendor_status == IntegrationStatusEnum.TIMEOUT:
                response_message = "Alert updated, but vendor sync timed out (Datadog may be down)"
            elif vendor_status == IntegrationStatusEnum.FAILED:
                response_message = "Alert updated, but vendor sync failed (check Datadog connectivity)"
            else:
                response_message = "Successfully processed the alert:0 created, 1 updated, pr are is created"

            return {
                "status": response_status,
                "message": response_message,
                "operation": "updated",
                "alert_code": updated_alert.code,
                "alert_id": updated_alert.id,
            }

        except Exception as e:
            logger.error(f"Error in update_alert: {str(e)}", exc_info=True)
            await self.session.rollback()
            raise

    async def create_alert(self, data: CreateAlert, user_code: str = None):
        """
        Create an alert configuration and sync with vendor platform.
        Uses Database-First Pattern to prevent orphan vendor monitors.
        """
        saved_alert = None
        try:
            # Step 1: Validate
            logger.debug("Validating data...")
            AlertConfigValidationError.validate_obs_create_alerts_rule(data)
            logger.debug("Validation passed")

            # Step 2: Get Service Details
            logger.debug(f"Getting service details for: {data.services_code}")
            service = await self.services_mst_repository.get_by_code(data.services_code)
            if not service:
                raise ValueError(f"Service not found: {data.services_code}")
            logger.debug(f"Service found - App: {service.applications_mst_code}, Tenant: {service.tenants_mst_code}")

            # Step 3: Get Vendor Account (Hierarchical)
            logger.debug("Getting vendor account...")
            vendor_account = await self.obs_vendor_accounts_mst_repository.get_by_service_hierarchy(
                service_code=data.services_code,
                application_code=service.applications_mst_code,
                tenant_code=service.tenants_mst_code
            )
            if not vendor_account:
                raise ValueError(f"No vendor account found for service/app/tenant hierarchy")
            logger.debug(f"Vendor account: {vendor_account.code} ({vendor_account.obs_vendor_enum.value})")

            # Step 3.5: Check if alert config already exists in database
            logger.debug("Checking for existing alert config...")
            existing_alert = await self.obs_alert_config.check_alert_exists(
                services_code=data.services_code,
                monitoring_policy_code=data.monitoring_policy_code
            )

            existing_monitor_id = None
            if existing_alert:
                existing_monitor_id = existing_alert.vendor_monitor_id
                logger.info(f"Found existing alert config - ID: {existing_alert.id}, Monitor ID: {existing_monitor_id}")
                print(f"\n📋 Existing alert found - Monitor ID: {existing_monitor_id or 'None'}\n")
            else:
                logger.info("No existing alert config found - will create new")

            # Step 4: Save to DB FIRST with PENDING status (prevents orphan monitors)
            if existing_alert:
                logger.debug("Updating existing alert config...")
                saved_alert = existing_alert
                # Update status to INITIATED while we process
                saved_alert.vendor_status = IntegrationStatusEnum.INITIATED
                await self.session.commit()
            else:
                logger.debug("Saving new alert to DB with PENDING status...")

                # Generate monitor vendor reference identifier for new alerts
                # Get monitoring policy to build identifier
                monitoring_policy_temp = await self.monitoring_policy_defaults_ref.get_by(code=data.monitoring_policy_code)
                if monitoring_policy_temp:
                    temp_identifier = f"{service.code}-{monitoring_policy_temp.infrastructuretype_ref_code}-{monitoring_policy_temp.alerttype_ref_code}".lower().replace('_', '-')
                else:
                    temp_identifier = None

                alert_config = make_alert_configs(
                    data=data,
                    vendor_account_code=vendor_account.code,
                    monitoring_policy_code=data.monitoring_policy_code,
                    vendor_monitor_id=None,
                    monitor_vendor_reference_identifier=temp_identifier,
                    vendor_status=IntegrationStatusEnum.INITIATED,
                    vendor_error=None
                )
                saved_alert = await self.obs_alert_config.create(**alert_config)
                await self.session.commit()
            logger.info(f"Alert saved - ID: {saved_alert.id}, Code: {saved_alert.code}")

            # Step 5: Create or Update Monitor in Vendor (with error handling)
            vendor_monitor_id = existing_monitor_id  # Use existing if available
            vendor_status = IntegrationStatusEnum.INITIATED
            vendor_error = None

            logger.debug(f"Creating monitor in {vendor_account.obs_vendor_enum.value}...")
            try:
                if vendor_account.obs_vendor_enum == ObsVendorEnum.datadog:
                    # Get monitoring policy for payload building
                    monitoring_policy = await self.monitoring_policy_defaults_ref.get_by(code=data.monitoring_policy_code)
                    if not monitoring_policy:
                        raise ValueError(f"Monitoring policy not found: {data.monitoring_policy_code}")

                    # Fetch query template from database
                    query_ref = await self.datadog_query_ref_repository.get_by_infra_alert_signal(
                        infrastructuretype_ref_code=monitoring_policy.infrastructuretype_ref_code,
                        alerttype_ref_code=monitoring_policy.alerttype_ref_code,
                        signal_kind=monitoring_policy.signal_kind
                    )
                    if not query_ref:
                        raise ValueError(
                            f"Query template not found for infrastructure: {monitoring_policy.infrastructuretype_ref_code}, "
                            f"alert: {monitoring_policy.alerttype_ref_code}, signal: {monitoring_policy.signal_kind}"
                        )

                    # Generate monitor vendor reference identifier - combination of service name and monitoring policy name
                    monitor_vendor_reference_identifier = f"{service.name} - {monitoring_policy.name}"
                    logger.debug(f"Monitor vendor reference identifier: {monitor_vendor_reference_identifier}")

                    # Check if Terragrunt mode is enabled
                    tenant_cfg = await get_tenant_config(service.tenants_mst_code, self.session)
                    infra_repo = tenant_cfg.github_infra_repository
                    if settings.datadog_use_terragrunt and infra_repo:
                        if existing_monitor_id:
                            logger.info(f"Using Terragrunt workflow to UPDATE existing monitor: {existing_monitor_id}")
                        else:
                            logger.info("Using Terragrunt workflow to CREATE new monitor")

                        # Generate Terragrunt HCL content
                        terragrunt_content = await self._generate_datadog_terragrunt_hcl(
                            monitoring_policy=monitoring_policy,
                            alert_data=data,
                            service=service,
                            query_template=query_ref.query_template
                        )

                        # Commit and apply via Terragrunt service
                        terragrunt_service = TerragruntMgmtService()
                        # Get environment from infrastructure relationship or default to "staging"
                        environment = service.infrastructure.environments_enum.value if service.infrastructure else "staging"
                        # Normalize environment for branch name (staging -> stage)
                        normalized_branch = terragrunt_service._normalize_environment_for_path(environment)

                        # Check for existing open PR for this service (unified PR strategy)
                        existing_pr = await self._get_existing_service_pr(
                            service_code=service.code,
                            tenant_code=service.tenants_mst_code
                        )
                        if existing_pr:
                            # Verify PR status from GitHub API before using
                            github_status = await self._verify_pr_status_from_github(
                                pr_number=existing_pr.get("pr_number"),
                                git_repository=infra_repo
                            )
                            if github_status.get("is_open"):
                                logger.info(f"Found existing open PR #{existing_pr.get('pr_number')} for service {service.code} (verified with GitHub)")
                            else:
                                # PR was closed/merged - DB already synced, don't use for diff check
                                logger.info(f"PR #{existing_pr.get('pr_number')} was {github_status.get('github_status')} on GitHub - will create new PR if needed")
                                existing_pr = None  # Clear so we diff check against base branch

                        # ========================================================================
                        # SINGLE ALERT ENDPOINT DISABLED - Use bulk endpoint instead
                        # The commit_terragrunt_file() method no longer supports Datadog monitors.
                        # Use the bulk-create-or-update-alerts endpoint for all alert operations.
                        # ========================================================================
                        raise ValueError(
                            "Single alert endpoint is disabled for Datadog monitors. "
                            "Please use the bulk-create-or-update-alerts endpoint instead."
                        )

                        # terragrunt_result = await terragrunt_service.commit_terragrunt_file(
                        #     terragrunt_content=terragrunt_content,
                        #     tenant=service.tenants_mst_code,
                        #     user_code=user_code,  # User code from JWT
                        #     environment=environment,
                        #     github_repository=settings.datadog_terraform_repo,
                        #     branch_name=normalized_branch,
                        #     resource_type="datadog/monitor",
                        #     service_name=service.name,  # Using service name for folder path
                        #     resource_type_ref=monitoring_policy.infrastructuretype_ref_code,
                        #     existing_monitor_id=existing_monitor_id,  # Pass existing monitor ID for import
                        #     product_name=service.application.name if service.application else None,  # Application name as product
                        #     region=None,  # Region handled by environment-based fallback in terragrunt_mgmt_service
                        #     existing_pr_info=existing_pr,  # Pass existing PR info for smart replacement
                        #     skip_if_no_changes=True  # Enable diff checking
                        # )
                        #
                        # # Handle no_changes response
                        # if terragrunt_result.get("status") == "no_changes":
                        #     logger.info("No changes detected - reusing existing PR or skipping")
                        #     vendor_status = IntegrationStatusEnum.INITIATED
                        #     if terragrunt_result.get("existing_pr_reused"):
                        #         pr_number = terragrunt_result.get("pr_number")
                        #         pr_url = terragrunt_result.get("pr_url")
                        #         vendor_error = f"No changes - existing PR #{pr_number} is up to date"
                        #     else:
                        #         vendor_error = "No changes detected - content matches base branch"
                        #
                        #     # Update alert with no-change status
                        #     saved_alert.vendor_status = vendor_status
                        #     saved_alert.vendor_error = vendor_error
                        #     await self.session.commit()
                        #
                        #     # Return early for no_changes
                        #     return {
                        #         "status": "success",
                        #         "message": vendor_error,
                        #         "operation": "created" if not existing_alert else "updated",
                        #         "alert_code": saved_alert.code,
                        #         "pr_number": terragrunt_result.get("pr_number"),
                        #         "pr_url": terragrunt_result.get("pr_url"),
                        #         "existing_pr_reused": terragrunt_result.get("existing_pr_reused", False)
                        #     }
                        #
                        # # Extract monitor ID from Terraform outputs
                        # terraform_outputs = terragrunt_result.get("terraform_outputs", {})
                        # vendor_monitor_id = terraform_outputs.get("monitor_id") or existing_monitor_id

                        # Check if PR workflow was used (indicated by pr_number in response)
                        pr_workflow_used = terragrunt_result.get("pr_number") is not None

                        if vendor_monitor_id:
                            vendor_status = IntegrationStatusEnum.SUCCESS
                            action = "updated" if existing_monitor_id else "created"
                            logger.info(f"Monitor {action} via Terragrunt - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (Terragrunt {action.title()}): {vendor_monitor_id}\n")
                        elif pr_workflow_used:
                            # PR workflow: monitor will be created/updated after PR merge by GitOps
                            vendor_status = IntegrationStatusEnum.INITIATED
                            pr_number = terragrunt_result.get("pr_number")
                            pr_url = terragrunt_result.get("pr_url")
                            vendor_error = f"PR #{pr_number} created - Monitor will be synced after PR merge by GitOps"
                            logger.info(f"PR workflow: {vendor_error}")
                            print(f"\n🔀 PR #{pr_number} created - Monitor sync pending GitOps apply\n")
                            print(f"🔗 PR URL: {pr_url}\n")

                            # Create GitOps workflow tracking record
                            try:
                                workflow_data = make_gitops_workflow_detail(
                                    git_repository=infra_repo,
                                    git_branch=terragrunt_result.get("feature_branch"),
                                    git_commit_sha=terragrunt_result.get("commit_sha"),
                                    pr_number=pr_number,
                                    pr_url=pr_url,
                                    tenant_mst_code=service.tenants_mst_code,
                                    user_mst_code=user_code,
                                    workflow_name=f"Alert: {service.name} - {monitoring_policy.name}",
                                    transaction_code=saved_alert.code,
                                    table_name=WorkflowSourceTableEnum.ALERT_CONFIG
                                )
                                workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                # If old PR was replaced, update its workflow status to CLOSED
                                old_pr_closed = terragrunt_result.get("old_pr_to_close")
                                if old_pr_closed and existing_pr:
                                    old_workflow_id = existing_pr.get("workflow_id")
                                    if old_workflow_id:
                                        await self.gitops_workflow_repository.update_pr_status(
                                            workflow_id=old_workflow_id,
                                            new_status=PRStatusEnum.PR_CLOSED
                                        )
                                        logger.info(f"Marked old workflow {old_workflow_id} as PR_CLOSED (superseded by PR #{pr_number})")

                                # Link alert to workflow and update status
                                await self.obs_alert_config.link_to_gitops_workflow(
                                    alert_ids=[saved_alert.id],
                                    workflow_id=workflow.id
                                )

                                await self.obs_alert_config.update_creation_status(
                                    alert_id=saved_alert.id,
                                    status=DeploymentStatusEnum.PR_CREATED
                                )

                                # Commit workflow tracking changes
                                await self.session.commit()

                                logger.info(f"GitOps workflow tracking created - Workflow ID: {workflow.id}, PR: {pr_number}")
                            except Exception as workflow_error:
                                logger.error(f"Failed to create workflow tracking: {workflow_error}")
                        else:
                            vendor_status = IntegrationStatusEnum.FAILED
                            vendor_error = "Monitor ID not found in Terraform outputs"
                            logger.error(vendor_error)
                    else:
                        logger.info("Using direct Datadog API workflow")

                        # Build payload using query template
                        payload = DatadogIntegration.build_payload(
                            monitoring_policy=monitoring_policy,
                            alert_data=data,
                            service=service,
                            query_template=query_ref.query_template
                        )
                        logger.debug(f"Payload: {payload}")

                        # Check if monitor already exists and decide CREATE vs UPDATE
                        if existing_monitor_id:
                            # UPDATE existing monitor
                            logger.info(f"Updating existing monitor via API - ID: {existing_monitor_id}")
                            print(f"\n📋 Updating existing monitor (API) - ID: {existing_monitor_id}\n")
                            datadog_response = await asyncio.wait_for(
                                DatadogIntegration.update_monitor(
                                    auth_config=vendor_account.auth_config,
                                    monitor_id=existing_monitor_id,
                                    payload=payload
                                ),
                                timeout=10.0
                            )
                            vendor_monitor_id = existing_monitor_id
                            logger.info(f"Monitor updated via API - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (API Update): {vendor_monitor_id}\n")
                        else:
                            # CREATE new monitor
                            logger.info("Creating new monitor via API")
                            datadog_response = await asyncio.wait_for(
                                DatadogIntegration.create_monitor(
                                    auth_config=vendor_account.auth_config,
                                    payload=payload
                                ),
                                timeout=10.0
                            )
                            vendor_monitor_id = str(datadog_response.get('id'))
                            logger.info(f"Monitor created via API - ID: {vendor_monitor_id}")
                            print(f"\n🔔 Datadog Monitor ID (API Create): {vendor_monitor_id}\n")

                        vendor_status = IntegrationStatusEnum.SUCCESS
                else:
                    raise ValueError(f"Vendor {vendor_account.obs_vendor_enum.value} not supported")
            except asyncio.TimeoutError:
                vendor_status = IntegrationStatusEnum.TIMEOUT
                vendor_error = "Vendor API timeout after 10 seconds"
                logger.warning(vendor_error)
            except Exception as e:
                vendor_status = IntegrationStatusEnum.FAILED
                vendor_error = str(e)[:500]
                logger.error(f"Vendor error - {vendor_error}")

            # Step 6: Update DB with vendor result
            logger.debug("Updating alert with vendor result...")
            saved_alert.vendor_monitor_id = vendor_monitor_id
            saved_alert.vendor_status = vendor_status
            saved_alert.vendor_error = vendor_error
            saved_alert.vendor_status_updated_at = datetime.utcnow()
            saved_alert.vendor_last_sync_attempt = datetime.utcnow()

            # Update monitor_vendor_reference_identifier if datadog
            if vendor_account.obs_vendor_enum == ObsVendorEnum.datadog and 'monitor_vendor_reference_identifier' in locals():
                saved_alert.monitor_vendor_reference_identifier = monitor_vendor_reference_identifier
                logger.debug(f"Updated monitor_vendor_reference_identifier: {monitor_vendor_reference_identifier}")

            await self.session.commit()
            logger.info(f"Updated - Status: {vendor_status.value}")

            # Step 7: Return simplified response
            response_status = "success" if vendor_status == IntegrationStatusEnum.SUCCESS else "partial"

            # Build message based on vendor status
            if vendor_status == IntegrationStatusEnum.SUCCESS:
                response_message = "Alert created successfully"
            elif vendor_status == IntegrationStatusEnum.TIMEOUT:
                response_message = "Alert created, but vendor sync timed out (Datadog may be down)"
            elif vendor_status == IntegrationStatusEnum.FAILED:
                response_message = "Alert created, but vendor sync failed (check Datadog connectivity)"
            else:
                response_message = "Successfully processed the alert:1 created, 0 updated, pr are is created"

            return {
                "status": response_status,
                "message": response_message,
                "alert_code": saved_alert.code,
                "alert_id": saved_alert.id,
            }

        except Exception as e:
            logger.error(f"Error in create_alert: {str(e)}", exc_info=True)
            if saved_alert:
                await self.session.rollback()
            raise

    async def bulk_create_or_update_alerts(
        self,
        alerts: List[CreateAlert],
        user_code: str = None,
        tenant_code: str = None
    ):
        """
        Bulk create or update alerts (upsert operation).

        When Terragrunt PR workflow is enabled:
        - Processes all alerts
        - Collects terragrunt files
        - Creates ONE commit with all files
        - Creates ONE PR

        When Terragrunt PR workflow is disabled:
        - Falls back to individual processing per alert

        Args:
            alerts: List of CreateAlert objects
            user_code: User code from JWT (optional, used for workflow tracking)
            tenant_code: Tenant code from JWT (optional, used for workflow tracking)

        Returns:
            Dict with summary and detailed results for created/updated/failed alerts
            Includes PR information if Terragrunt PR workflow is used
        """
        logger.info(f"Starting bulk create/update for {len(alerts)} alerts")

        # Step 1: Get all unique service codes from the alert list
        logger.debug("Extracting unique service codes...")
        service_codes = set(alert.services_code for alert in alerts)
        logger.debug(f"Found {len(service_codes)} unique service(s)")

        # Step 2: Fetch ALL existing alerts for these services in one query
        logger.debug("Fetching existing alerts for all services...")
        configured_alerts_list = await self.obs_alert_config.get_multi(
            filters=[self.obs_alert_config.model.services_mst_code.in_(service_codes)]
        )

        # Step 3: Create a map: (service_code, policy_code) -> configured_alert
        configured_alerts_map = {
            (alert.services_mst_code, alert.monitoring_policy_defaults_ref_code): alert
            for alert in configured_alerts_list
        }
        logger.debug(f"Found {len(configured_alerts_map)} existing alerts")

        # Step 4: Check if Terragrunt PR batch workflow is enabled
        tenant_cfg = await get_tenant_config(tenant_code, self.session) if tenant_code else None
        infra_repo = tenant_cfg.github_infra_repository if tenant_cfg else ""
        use_terragrunt_batch_pr = (
            settings.datadog_use_terragrunt and
            settings.datadog_use_pr_workflow and
            infra_repo
        )

        if use_terragrunt_batch_pr:
            logger.info("Using Terragrunt BATCH PR workflow (one commit + one PR)")
            return await self._bulk_create_with_single_pr(
                alerts, configured_alerts_map, user_code=user_code, tenant_code=tenant_code
            )
        else:
            logger.info("Using individual workflow (per-alert commits or API calls)")
            return await self._bulk_create_individual(alerts, configured_alerts_map)

    async def _bulk_create_individual(self, alerts: List[CreateAlert], configured_alerts_map: dict):
        """
        Original bulk create logic - processes each alert independently.
        Used when Terragrunt batch PR mode is disabled.
        """
        created_alerts = []
        updated_alerts = []
        failed_alerts = []

        for idx, alert_data in enumerate(alerts):
            logger.info(f"[{idx + 1}/{len(alerts)}] Processing alert for service: {alert_data.services_code}, policy: {alert_data.monitoring_policy_code}")

            try:
                # Check if alert exists in our pre-fetched map
                alert_key = (alert_data.services_code, alert_data.monitoring_policy_code)
                existing_alert = configured_alerts_map.get(alert_key)

                if existing_alert:
                    # UPDATE path
                    result = await self._update_existing_alert(existing_alert, alert_data)
                else:
                    # CREATE path
                    result = await self.create_alert(alert_data)
                    result["operation"] = "created"

                # Categorize based on operation type
                alert_summary = {
                    "alert_id": result["alert_id"],
                    "alert_code": result["alert_code"],
                    "services_code": alert_data.services_code,
                    "monitoring_policy_code": alert_data.monitoring_policy_code,
                    "vendor_monitor_id": result.get("vendor_monitor_id"),
                    "vendor_status": result.get("vendor_status"),
                    "threshold_value": alert_data.threshold_value,
                    "severity": alert_data.severity.value,
                }

                if result["operation"] == "created":
                    created_alerts.append(alert_summary)
                    logger.info(f"Created alert: {result['alert_code']}")
                elif result["operation"] == "updated":
                    updated_alerts.append(alert_summary)
                    logger.info(f"Updated alert: {result['alert_code']}")

            except Exception as e:
                error_msg = str(e)[:200]
                logger.error(f"Failed: {error_msg}")
                failed_alerts.append({
                    "services_code": alert_data.services_code,
                    "monitoring_policy_code": alert_data.monitoring_policy_code,
                    "threshold_value": alert_data.threshold_value,
                    "severity": alert_data.severity.value,
                    "error": error_msg
                })
                # Continue processing remaining alerts
                continue

        # Determine overall status and create simplified response
        created_count = len(created_alerts)
        updated_count = len(updated_alerts)
        failed_count = len(failed_alerts)
        total_processed = created_count + updated_count + failed_count

        overall_status = "success" if not failed_alerts else "partial"

        # Build human-readable message
        if failed_count == 0:
            message = f"Successfully processed {total_processed} alert(s): {created_count} created, {updated_count} updated"
        else:
            message = f"Processed {total_processed} alert(s): {created_count} created, {updated_count} updated, {failed_count} failed"

        logger.info(f"Bulk Operation Complete - Total: {total_processed}, Created: {created_count}, Updated: {updated_count}, Failed: {failed_count}")

        return {
            "status": overall_status,
            "message": message,
            "alerts_created": created_count,
            "alerts_updated": updated_count,
            "alerts_failed": failed_count
        }

    async def _bulk_create_with_single_pr(
        self,
        alerts: List[CreateAlert],
        configured_alerts_map: dict,
        user_code: str = None,
        tenant_code: str = None
    ):
        """
        Bulk create logic with single PR workflow.
        Creates all alerts, collects terragrunt files, and creates ONE PR with all changes.

        Supports diff checking:
        - Looks up existing open PRs for the alerts being updated
        - Passes existing PR info to batch commit for smart PR replacement
        - Skips commits if content is unchanged
        """
        logger.info("Starting bulk create with single PR workflow")

        tenant_cfg = await get_tenant_config(tenant_code, self.session) if tenant_code else None
        infra_repo = tenant_cfg.github_infra_repository if tenant_cfg else ""

        alert_preparations = []
        failed_alerts = []

        # Phase 1: Prepare all alerts (validation, database, terragrunt generation)
        for idx, alert_data in enumerate(alerts):
            logger.info(f"[{idx + 1}/{len(alerts)}] Preparing alert for service: {alert_data.services_code}, policy: {alert_data.monitoring_policy_code}")

            try:
                # Validate
                AlertConfigValidationError.validate_obs_create_alerts_rule(alert_data)

                # Get service details
                service = await self.services_mst_repository.get_by_code(alert_data.services_code)
                if not service:
                    raise ValueError(f"Service not found: {alert_data.services_code}")

                # Get vendor account
                vendor_account = await self.obs_vendor_accounts_mst_repository.get_by_service_hierarchy(
                    service_code=alert_data.services_code,
                    application_code=service.applications_mst_code,
                    tenant_code=service.tenants_mst_code
                )
                if not vendor_account:
                    raise ValueError(f"No vendor account found for service {alert_data.services_code}")

                # Get monitoring policy and query template
                monitoring_policy = await self.monitoring_policy_defaults_ref.get_by(
                    code=alert_data.monitoring_policy_code
                )
                if not monitoring_policy:
                    raise ValueError(f"Monitoring policy not found: {alert_data.monitoring_policy_code}")

                query_ref = await self.datadog_query_ref_repository.get_by_infra_alert_signal(
                    infrastructuretype_ref_code=monitoring_policy.infrastructuretype_ref_code,
                    alerttype_ref_code=monitoring_policy.alerttype_ref_code,
                    signal_kind=monitoring_policy.signal_kind
                )
                if not query_ref:
                    raise ValueError(f"Query template not found for policy: {alert_data.monitoring_policy_code}")

                # Check if UPDATE or CREATE
                alert_key = (alert_data.services_code, alert_data.monitoring_policy_code)
                existing_alert = configured_alerts_map.get(alert_key)
                is_update = existing_alert is not None

                # Save to database FIRST (status=INITIATED)
                if existing_alert:
                    # UPDATE path
                    update_data = {
                        "comparator": alert_data.comparator,
                        "threshold_value": alert_data.threshold_value,
                        "threshold_unit": alert_data.threshold_unit,
                        "eval_window": alert_data.eval_window,
                        "for_duration": alert_data.for_duration,
                        "severity": alert_data.severity,
                        "status": alert_data.status,
                        "no_data": alert_data.no_data,
                        "vendor_status": IntegrationStatusEnum.INITIATED,
                        "vendor_last_sync_attempt": datetime.utcnow()
                    }
                    saved_alert = await self.obs_alert_config.update(existing_alert, update_data)
                else:
                    # CREATE path
                    # Use same identifier format as single alert endpoint for consistency
                    monitor_vendor_reference_identifier = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')
                    alert_config = make_alert_configs(
                        data=alert_data,
                        vendor_account_code=vendor_account.code,
                        monitoring_policy_code=alert_data.monitoring_policy_code,
                        vendor_monitor_id=None,
                        monitor_vendor_reference_identifier=monitor_vendor_reference_identifier,
                        vendor_status=IntegrationStatusEnum.INITIATED,
                        vendor_error=None
                    )
                    saved_alert = await self.obs_alert_config.create(**alert_config)

                await self.session.commit()
                logger.info(f"Saved alert to database: {saved_alert.code}")

                # Generate terragrunt HCL monitor block (for list-based structure)
                terragrunt_content = await self._generate_datadog_monitor_block(
                    monitoring_policy=monitoring_policy,
                    alert_data=alert_data,
                    service=service,
                    query_template=query_ref.query_template
                )

                # Generate identifier - use same format as single alert endpoint for consistency
                identifier = re.sub(r'[\s-]+', '-', f"{service.name} - {monitoring_policy.name}").strip('-')

                # Collect preparation data
                alert_preparations.append({
                    "alert": saved_alert,
                    "alert_data": alert_data,
                    "service": service,
                    "monitoring_policy": monitoring_policy,
                    "terragrunt_content": terragrunt_content,
                    "existing_monitor_id": saved_alert.vendor_monitor_id,
                    "identifier": identifier,
                    "is_update": is_update
                })

            except Exception as e:
                error_msg = str(e)[:200]
                logger.error(f"Failed to prepare alert: {error_msg}")
                failed_alerts.append({
                    "services_code": alert_data.services_code,
                    "monitoring_policy_code": alert_data.monitoring_policy_code,
                    "threshold_value": alert_data.threshold_value,
                    "severity": alert_data.severity.value,
                    "error": error_msg
                })
                continue

        # Phase 2: Batch commit with single PR (if any alerts prepared successfully)
        pr_number = None
        pr_url = None

        if alert_preparations:
            try:
                logger.info(f"Creating single PR for {len(alert_preparations)} alerts")

                # Check for existing open PR for this service (unified PR strategy)
                # Single query to find any open PR for any alert of this service
                existing_pr_info = None
                pr_was_closed = False  # Track if PR was externally closed/merged

                # Get service code from first alert (all alerts in batch should be for same service)
                first_service = alert_preparations[0]["service"]
                service_code = first_service.code

                existing_pr = await self._get_existing_service_pr(
                    service_code=service_code,
                    tenant_code=first_service.tenants_mst_code
                )

                if existing_pr:
                    pr_number_to_check = existing_pr.get("pr_number")

                    # Verify PR status from GitHub API
                    github_status = await self._verify_pr_status_from_github(
                        pr_number=pr_number_to_check,
                        git_repository=infra_repo
                    )

                    if github_status.get("is_open"):
                        # PR is still open on GitHub - safe to reuse for diff check
                        existing_pr_info = existing_pr
                        logger.info(
                            f"Found existing open PR #{pr_number_to_check} for service {service_code} (verified with GitHub) - will use for diff checking"
                        )
                    else:
                        # PR was closed/merged on GitHub - DB was already synced by _verify_pr_status_from_github
                        pr_was_closed = True
                        logger.info(
                            f"PR #{pr_number_to_check} was {github_status.get('github_status')} on GitHub - will diff check against base branch and create new PR if needed"
                        )

                # Prepare files_data for batch commit
                files_data = []
                for prep in alert_preparations:
                    files_data.append({
                        "terragrunt_content": prep["terragrunt_content"],
                        "service": prep["service"],  # Pass full service object for path construction
                        "service_code": prep["service"].code,
                        "service_name": prep["service"].name,  # Service display name
                        "resource_type_ref": prep["monitoring_policy"].infrastructuretype_ref_code,
                        "identifier": prep["identifier"],
                        "existing_monitor_id": prep["existing_monitor_id"]
                    })

                # Get environment and metadata from first service (assuming all alerts are for same tenant/env)
                first_service = alert_preparations[0]["service"]
                environment = first_service.infrastructure.environments_enum.value if first_service.infrastructure else "staging"
                tenant = first_service.tenants_mst_code
                product_name = first_service.application.name if first_service.application else None
                region = None  # Region handled by environment-based fallback in terragrunt_mgmt_service

                # Call batch terragrunt service with diff checking enabled
                # Note: Don't pass feature_branch_name - batch function will create new branch
                # and use existing_pr_info for close-old-create-new PR replacement strategy
                terragrunt_service = TerragruntMgmtService()
                batch_result = await terragrunt_service.commit_multiple_terragrunt_files_batch(
                    files_data=files_data,
                    github_repository=infra_repo,
                    environment=environment,
                    tenant=tenant,
                    product_name=product_name,  # Application name for path
                    region=region,  # Service region for path
                    existing_pr_info=existing_pr_info,  # For PR replacement strategy
                    skip_if_no_changes=True  # Enable diff checking
                )

                # Handle "no_changes" response (all files identical to existing PR or base branch)
                if batch_result.get("status") == "no_changes":
                    logger.info("No changes detected - skipping PR creation")
                    # Update alerts to reflect no PR needed
                    for prep in alert_preparations:
                        prep["alert"].vendor_status = IntegrationStatusEnum.INITIATED
                        if existing_pr_info:
                            prep["alert"].vendor_error = f"No changes - existing PR #{existing_pr_info.get('pr_number')} already has latest config"
                        else:
                            prep["alert"].vendor_error = "No changes - config already matches base branch"
                        prep["alert"].vendor_status_updated_at = datetime.utcnow()
                    await self.session.commit()

                    # Return success with counts
                    creates_count = sum(1 for p in alert_preparations if not p.get("is_update", False))
                    updates_count = sum(1 for p in alert_preparations if p.get("is_update", False))
                    return {
                        "status": "success",
                        "message": f"No changes detected for {len(alert_preparations)} alert(s) - config already up to date",
                        "alerts_created": creates_count,
                        "alerts_updated": updates_count,
                        "alerts_failed": len(failed_alerts)
                    }

                pr_number = batch_result.get("pr_number")
                pr_url = batch_result.get("pr_url")
                old_pr_closed = batch_result.get("old_pr_to_close")

                logger.info(f"Successfully created PR #{pr_number}: {pr_url}")

                # If old PR was replaced, update its workflow status to CLOSED
                if old_pr_closed and existing_pr_info:
                    old_workflow_id = existing_pr_info.get("workflow_id")
                    if old_workflow_id:
                        try:
                            await self.gitops_workflow_repository.update_pr_status(
                                workflow_id=old_workflow_id,
                                new_status=PRStatusEnum.PR_CLOSED
                            )
                            logger.info(f"Marked old workflow {old_workflow_id} as PR_CLOSED (superseded by PR #{pr_number})")
                        except Exception as e:
                            logger.warning(f"Failed to update old workflow status: {e}")

                # Create GitOps workflow tracking record for each alert (one entry per alert)
                # This ensures each alert has its own PR history entry
                try:
                    for prep in alert_preparations:
                        alert = prep["alert"]
                        service = prep["service"]
                        monitoring_policy = prep["monitoring_policy"]
                        is_update = prep.get("is_update", False)

                        # Build workflow name for this specific alert
                        operation = "Update" if is_update else "Create"
                        workflow_name = f"Alert {operation}: {service.name} - {monitoring_policy.name}"

                        workflow_data = make_gitops_workflow_detail(
                            git_repository=infra_repo,
                            git_branch=batch_result.get("feature_branch"),
                            git_commit_sha=batch_result.get("commit_sha"),
                            pr_number=pr_number,
                            pr_url=pr_url,
                            tenant_mst_code=tenant_code,
                            user_mst_code=user_code,
                            workflow_name=workflow_name,
                            transaction_code=alert.code,  # Each alert's code for proper PR history
                            table_name=WorkflowSourceTableEnum.ALERT_CONFIG
                        )
                        workflow = await self.gitops_workflow_repository.create(**workflow_data)

                        # Link this alert to its workflow
                        await self.obs_alert_config.link_to_gitops_workflow(
                            alert_ids=[alert.id],
                            workflow_id=workflow.id
                        )

                        # Update creation_status
                        await self.obs_alert_config.update_creation_status(
                            alert_id=alert.id,
                            status=DeploymentStatusEnum.PR_CREATED
                        )

                    logger.info(f"Created {len(alert_preparations)} GitOps workflow entries for bulk PR #{pr_number}")

                    # Commit workflow tracking changes
                    await self.session.commit()
                except Exception as workflow_error:
                    logger.error(f"Failed to create workflow tracking for bulk operation: {workflow_error}")

                # Phase 3: Update database with PR info
                for prep in alert_preparations:
                    prep["alert"].vendor_status = IntegrationStatusEnum.INITIATED
                    prep["alert"].vendor_error = f"PR #{pr_number} created - Monitor will be synced after PR merge by GitOps"
                    prep["alert"].vendor_status_updated_at = datetime.utcnow()

                await self.session.commit()
                logger.info(f"Updated {len(alert_preparations)} alerts with PR info")

            except Exception as e:
                error_msg = str(e)[:500]
                logger.error(f"Failed to create PR: {error_msg}")

                # Mark all prepared alerts as failed
                for prep in alert_preparations:
                    prep["alert"].vendor_status = IntegrationStatusEnum.FAILED
                    prep["alert"].vendor_error = f"PR creation failed: {error_msg}"
                    prep["alert"].vendor_status_updated_at = datetime.utcnow()

                await self.session.commit()

                # Move all to failed list
                for prep in alert_preparations:
                    failed_alerts.append({
                        "services_code": prep["alert_data"].services_code,
                        "monitoring_policy_code": prep["alert_data"].monitoring_policy_code,
                        "threshold_value": prep["alert_data"].threshold_value,
                        "severity": prep["alert_data"].severity.value,
                        "error": f"PR creation failed: {error_msg}"
                    })
                alert_preparations = []

        # Phase 4: Categorize and return results
        created_alerts = []
        updated_alerts = []

        for prep in alert_preparations:
            alert_summary = {
                "alert_id": prep["alert"].id,
                "alert_code": prep["alert"].code,
                "services_code": prep["alert_data"].services_code,
                "monitoring_policy_code": prep["alert_data"].monitoring_policy_code,
                "vendor_monitor_id": prep["alert"].vendor_monitor_id,
                "vendor_status": prep["alert"].vendor_status.value,
                "threshold_value": prep["alert_data"].threshold_value,
                "severity": prep["alert_data"].severity.value,
            }

            if prep["is_update"]:
                updated_alerts.append(alert_summary)
            else:
                created_alerts.append(alert_summary)

        created_count = len(created_alerts)
        updated_count = len(updated_alerts)
        failed_count = len(failed_alerts)
        total_processed = created_count + updated_count + failed_count

        overall_status = "success" if failed_count == 0 else "partial"

        # Build message with PR info
        if pr_number:
            if failed_count == 0:
                message = f"Successfully processed {total_processed} alert(s): {created_count} created, {updated_count} updated. PR #{pr_number} created."
            else:
                message = f"Processed {total_processed} alert(s): {created_count} created, {updated_count} updated, {failed_count} failed. PR #{pr_number} created for successful alerts."
        else:
            message = f"Processed {total_processed} alert(s): {failed_count} failed. No PR created."

        logger.info(f"Bulk Operation with Single PR Complete - Total: {total_processed}, Created: {created_count}, Updated: {updated_count}, Failed: {failed_count}, PR: #{pr_number}")

        result = {
            "status": overall_status,
            "message": message,
            "alerts_created": created_count,
            "alerts_updated": updated_count,
            "alerts_failed": failed_count
        }

        # Add PR info if available
        if pr_number:
            result["pr_number"] = pr_number
            result["pr_url"] = pr_url

        return result

    async def get_alerts_for_service(self, services_code: str, infrastructuretype_ref_code: str):
        """
        Get all alerts (configured and available) for a service filtered by infrastructure type.

        Returns comprehensive list showing:
        - Configured alerts with their current values and vendor status
        - Available alerts with default/override values

        Args:
            services_code: Service code to get alerts for
            infrastructuretype_ref_code: Infrastructure type to filter policies by

        Returns:
            Dict with service info and list of alerts
        """
        logger.info(f"Getting alerts for service: {services_code}, infrastructure type: {infrastructuretype_ref_code}")

        # Step 1: Get service details
        logger.debug("Getting service details...")
        service = await self.services_mst_repository.get_by_code(services_code)
        if not service:
            raise ValueError(f"Service not found: {services_code}")
        logger.debug(f"Service: {service.name}, Tenant: {service.tenants_mst_code}, App: {service.applications_mst_code}, RG: {service.resource_group_mst_code}")

        # # Step 2: Get infrastructure dependencies (COMMENTED OUT - using direct parameter now)
        # logger.debug("Getting infrastructure dependencies...")
        # dependencies = await self.service_dependency_map_repository.get_by_service_with_infrastructure(services_code)
        # if not dependencies:
        #     logger.debug("No infrastructure dependencies found")
        #     return {
        #         "service_code": services_code,
        #         "service_name": service.name,
        #         "total_alerts": 0,
        #         "configured_count": 0,
        #         "available_count": 0,
        #         "alerts": []
        #     }

        # # Extract unique infrastructure types
        # infra_types = set()
        # for dep in dependencies:
        #     infra_types.add(dep.infrastructure.infrastructuretype_ref_code)
        # logger.debug(f"Infrastructure types: {list(infra_types)}")

        # # Step 3: Get all monitoring policies for these infrastructure types
        # logger.debug("Getting monitoring policies...")
        # monitoring_policies = []
        # for infra_type in infra_types:
        #     policies = await self.monitoring_policy_defaults_ref.get_by_infrastructuretype_ref_code(infra_type)
        #     monitoring_policies.extend(policies)
        # logger.debug(f"Found {len(monitoring_policies)} monitoring policies")

        # Step 2: Get monitoring policies directly for the specified infrastructure type
        logger.debug(f"Getting monitoring policies for infrastructure type: {infrastructuretype_ref_code}...")
        monitoring_policies = await self.monitoring_policy_defaults_ref.get_by_infrastructuretype_ref_code(infrastructuretype_ref_code)
        logger.debug(f"Found {len(monitoring_policies)} monitoring policies")

        # Step 3: Fetch display names for infrastructure types and alert types
        logger.debug("Fetching infrastructure and alert type display names...")

        # Get unique infrastructure type codes and alert type codes from policies
        infra_type_codes = set(policy.infrastructuretype_ref_code for policy in monitoring_policies)
        alert_type_codes = set(policy.alerttype_ref_code for policy in monitoring_policies)

        # Fetch infrastructure types and alert types
        infra_types_map = {}
        alert_types_map = {}

        for infra_code in infra_type_codes:
            infra_type = await self.infrastructuretype_ref_repository.get_by_code(infra_code)
            if infra_type:
                infra_types_map[infra_code] = infra_type.name

        for alert_code in alert_type_codes:
            alert_type = await self.alerttype_ref_repository.get_by_code(alert_code)
            if alert_type:
                alert_types_map[alert_code] = alert_type.name

        logger.debug(f"Loaded {len(infra_types_map)} infrastructure type names and {len(alert_types_map)} alert type names")

        # Step 4: Get already configured alerts
        logger.debug("Getting configured alerts...")
        configured_alerts_list = await self.obs_alert_config.get_multi(
            filters=[self.obs_alert_config.model.services_mst_code == services_code]
        )

        # Create a map: policy_code -> configured_alert
        configured_alerts_map = {
            alert.monitoring_policy_defaults_ref_code: alert
            for alert in configured_alerts_list
        }
        logger.debug(f"Found {len(configured_alerts_map)} configured alerts")

        # Step 5: Build response for each policy
        logger.debug("Building response...")
        alerts_list = []

        for policy in monitoring_policies:
            policy_code = policy.code

            # Check if already configured
            if policy_code in configured_alerts_map:
                # Use configured values
                configured_alert = configured_alerts_map[policy_code]

                alert_info = {
                    "is_configured": True,
                    "alert_id": configured_alert.id,
                    "alert_code": configured_alert.code,
                    "vendor_status": configured_alert.vendor_status.value if configured_alert.vendor_status else None,
                    "vendor_monitor_id": configured_alert.vendor_monitor_id,

                    # Policy metadata
                    "monitoring_policy_code": policy.code,
                    "monitoring_policy_name": policy.name,
                    "infrastructuretype_ref_code": policy.infrastructuretype_ref_code,
                    "alerttype_ref_code": policy.alerttype_ref_code,

                    # Display names (NEW)
                    "infrastructure_type_name": infra_types_map.get(policy.infrastructuretype_ref_code, policy.infrastructuretype_ref_code),
                    "alert_type_name": alert_types_map.get(policy.alerttype_ref_code, policy.alerttype_ref_code),

                    # Configured values
                    "comparator": configured_alert.comparator.value,
                    "threshold_value": int(configured_alert.threshold_value),
                    "threshold_unit": configured_alert.threshold_unit.value,
                    "eval_window": configured_alert.eval_window,
                    "for_duration": configured_alert.for_duration,
                    "no_data": configured_alert.no_data,
                    "severity": configured_alert.severity.value,

                    "policy_source": "configured"
                }
            else:
                # Not configured - resolve with override hierarchy
                resolved_policy, policy_source = await self.resolve_monitoring_policy(
                    base_policy_code=policy.code,
                    resource_group_code=service.resource_group_mst_code,
                    application_code=service.applications_mst_code,
                    tenant_code=service.tenants_mst_code,
                    infrastructure_type_code=policy.infrastructuretype_ref_code
                )

                alert_info = {
                    "is_configured": False,
                    "alert_id": None,
                    "alert_code": None,
                    "vendor_status": None,
                    "vendor_monitor_id": None,

                    # Policy metadata
                    "monitoring_policy_code": policy.code,
                    "monitoring_policy_name": policy.name,
                    "infrastructuretype_ref_code": policy.infrastructuretype_ref_code,
                    "alerttype_ref_code": policy.alerttype_ref_code,

                    # Display names (NEW)
                    "infrastructure_type_name": infra_types_map.get(policy.infrastructuretype_ref_code, policy.infrastructuretype_ref_code),
                    "alert_type_name": alert_types_map.get(policy.alerttype_ref_code, policy.alerttype_ref_code),

                    # Resolved values (from override or default)
                    "comparator": resolved_policy.comparator.value,
                    "threshold_value": int(resolved_policy.threshold_value),
                    "threshold_unit": resolved_policy.threshold_unit.value,
                    "eval_window": resolved_policy.eval_window,
                    "for_duration": resolved_policy.for_duration,
                    "no_data": resolved_policy.no_data,
                    "severity": resolved_policy.severity.value,

                    "policy_source": policy_source
                }

            alerts_list.append(alert_info)

        # Build final response
        configured_count = len(configured_alerts_map)
        available_count = len(monitoring_policies) - configured_count

        logger.info(f"Summary - Total alerts: {len(alerts_list)}, Configured: {configured_count}, Available: {available_count}")

        return {
            "service_code": services_code,
            "service_name": service.name,
            "total_alerts": len(alerts_list),
            "configured_count": configured_count,
            "available_count": available_count,
            "alerts": alerts_list
        }

    async def resolve_monitoring_policy(
        self,
        base_policy_code: str,
        resource_group_code: str,
        application_code: str,
        tenant_code: str,
        infrastructure_type_code: str
    ):
        """
        Resolve monitoring policy using hierarchical override system.

        8-level cascading lookup (most specific → least specific):
        1. Resource Group + Infrastructure Type + Tenant
        2. Resource Group + Tenant
        3. Application + Infrastructure Type + Tenant
        4. Application + Tenant
        5. Infrastructure Type + Tenant
        6. Tenant only
        7. Infrastructure Type only (global)
        8. Default policy (fallback)

        Returns:
            Tuple of (policy_object, policy_source_description)
        """
        # Level 1
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            resource_group_mst_code=resource_group_code,
            infrastructuretype_ref_code=infrastructure_type_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: RG+InfraType+Tenant"

        # Level 2
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            resource_group_mst_code=resource_group_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: RG+Tenant"

        # Level 3
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            applications_mst_code=application_code,
            infrastructuretype_ref_code=infrastructure_type_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: App+InfraType+Tenant"

        # Level 4
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            applications_mst_code=application_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: App+Tenant"

        # Level 5
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            infrastructuretype_ref_code=infrastructure_type_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: InfraType+Tenant"

        # Level 6
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            tenants_mst_code=tenant_code
        )
        if override:
            return override, f"Override: Tenant"

        # Level 7
        override = await self.monitoring_policy_overrides_mst.get_override_by_scope(
            monitoring_policy_defaults_ref_code=base_policy_code,
            infrastructuretype_ref_code=infrastructure_type_code
        )
        if override:
            return override, f"Override: InfraType (global)"

        # Level 8: Default policy
        default_policy = await self.monitoring_policy_defaults_ref.get_by(code=base_policy_code)
        if not default_policy:
            raise ValueError(f"Default monitoring policy not found: {base_policy_code}")

        return default_policy, f"Default Policy"

    # TODO: This method is commented out during the refactoring of infrastructuretype_ref_code
    # from services_mst to service_config. This method was using service.infrastructuretype_ref_code
    # which has been moved to service_config. Re-enable when enable_alerts_for_service is needed
    # and update to use service_config.infrastructuretype_ref_code instead.
    #
    # async def enable_alerts_for_service(self, services_code: str):
    #     """
    #     Enable all applicable alerts for a service based on its infrastructure type.
    #
    #     Idempotent operation: Only creates alerts that don't already exist in alert_configs table.
    #
    #     Process:
    #     1. Get service details (including infrastructuretype_ref_code)
    #     2. Get monitoring policies for the service's infrastructure type
    #     3. Get already configured alerts
    #     4. For each policy not yet configured, resolve overrides and create
    #     5. Build and return summary response
    #
    #     Args:
    #         services_code: Service code to enable alerts for
    #
    #     Returns:
    #         Summary dict with status, message, alerts_created, and alerts_failed counts
    #     """
    #     logger.info(f"Enabling alerts for service: {services_code}")
    #
    #     # Step 1: Get service details
    #     logger.debug("Getting service details...")
    #     service = await self.services_mst_repository.get_by_code(services_code)
    #     if not service:
    #         raise ValueError(f"Service not found: {services_code}")
    #     logger.debug(f"Service: {service.name}, Tenant: {service.tenants_mst_code}, App: {service.applications_mst_code}, RG: {service.resource_group_mst_code}, InfraType: {service.infrastructuretype_ref_code}")
    #
    #     # Step 2: Get infrastructure type directly from service
    #     logger.debug(f"Using infrastructure type from service: {service.infrastructuretype_ref_code}")
    #     infrastructuretype_ref_code = service.infrastructuretype_ref_code
    #
    #     # Step 3: Get monitoring policies for this infrastructure type
    #     logger.debug("Getting monitoring policies...")
    #     monitoring_policies = await self.monitoring_policy_defaults_ref.get_by_infrastructuretype_ref_code(infrastructuretype_ref_code)
    #     logger.debug(f"Found {len(monitoring_policies)} monitoring policies for infrastructure type: {infrastructuretype_ref_code}")
    #
    #     # Step 4: Get already configured alerts
    #     logger.debug("Getting configured alerts...")
    #     configured_alerts_list = await self.obs_alert_config.get_multi(
    #         filters=[self.obs_alert_config.model.services_mst_code == services_code]
    #     )
    #
    #     # Create a map: policy_code -> configured_alert
    #     configured_alerts_map = {
    #         alert.monitoring_policy_defaults_ref_code: alert
    #         for alert in configured_alerts_list
    #     }
    #     logger.debug(f"Found {len(configured_alerts_map)} configured alerts")
    #
    #     # Step 5: Process each monitoring policy and create alerts
    #     logger.debug("Processing monitoring policies...")
    #     existing_alerts = []
    #     new_alerts = []
    #     failed_alerts = []
    #
    #     for base_policy in monitoring_policies:
    #         logger.debug(f"Checking policy: {base_policy.code} ({base_policy.name})")
    #
    #         # Check if alert already exists in our map
    #         if base_policy.code in configured_alerts_map:
    #             existing_alert = configured_alerts_map[base_policy.code]
    #             logger.debug(f"Alert already exists - skipping (ID: {existing_alert.id})")
    #             existing_alerts.append({
    #                 "monitoring_policy_code": base_policy.code,
    #                 "monitoring_policy_name": base_policy.name,
    #                 "alert_code": existing_alert.code,
    #                 "vendor_status": existing_alert.vendor_status.value
    #             })
    #             continue
    #
    #         # Alert doesn't exist - resolve policy overrides and create
    #         try:
    #             logger.debug("Resolving policy overrides...")
    #
    #             # Resolve policy using 8-level hierarchy
    #             resolved_policy, policy_source = await self.resolve_monitoring_policy(
    #                 base_policy_code=base_policy.code,
    #                 resource_group_code=service.resource_group_mst_code,
    #                 application_code=service.applications_mst_code,
    #                 tenant_code=service.tenants_mst_code,
    #                 infrastructure_type_code=base_policy.infrastructuretype_ref_code
    #             )
    #             logger.debug(f"Policy source: {policy_source}")
    #
    #             # Build alert creation payload using resolved policy
    #             from app.core.enum import StatusEnum
    #             alert_data = CreateAlert(
    #                 services_code=services_code,
    #                 monitoring_policy_code=base_policy.code,
    #                 comparator=resolved_policy.comparator,
    #                 threshold_value=resolved_policy.threshold_value,
    #                 threshold_unit=resolved_policy.threshold_unit,
    #                 eval_window=resolved_policy.eval_window,
    #                 for_duration=resolved_policy.for_duration,
    #                 severity=resolved_policy.severity,
    #                 status=StatusEnum.active,  # New alerts are active by default
    #                 no_data=resolved_policy.no_data
    #             )
    #
    #             # Create alert
    #             result = await self.create_alert(alert_data)
    #
    #             logger.info(f"Alert created (Code: {result['alert_code']})")
    #             new_alerts.append({
    #                 "monitoring_policy_code": base_policy.code,
    #                 "monitoring_policy_name": base_policy.name,
    #                 "alert_code": result["alert_code"],
    #                 "vendor_status": result.get("vendor_status")
    #             })
    #
    #         except Exception as e:
    #             error_msg = str(e)[:200]
    #             logger.error(f"Failed: {error_msg}")
    #             failed_alerts.append({
    #                 "monitoring_policy_code": base_policy.code,
    #                 "monitoring_policy_name": base_policy.name,
    #                 "error": error_msg
    #             })
    #
    #     # Step 6: Build and return summary response
    #     created_count = len(new_alerts)
    #     failed_count = len(failed_alerts)
    #     logger.info(f"Enable alerts complete - Created: {created_count}, Failed: {failed_count}")
    #
    #     if failed_count == 0:
    #         message = f"Successfully created {created_count} alert(s)"
    #         status = "success"
    #     else:
    #         message = f"Created {created_count} alert(s), {failed_count} failed"
    #         status = "partial"
    #
    #     return {
    #         "status": status,
    #         "message": message,
    #         "alerts_created": created_count,
    #         "alerts_failed": failed_count
    #     }
