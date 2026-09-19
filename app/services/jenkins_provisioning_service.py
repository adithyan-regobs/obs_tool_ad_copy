"""
Jenkins Pipeline Provisioning Service

Orchestrates the lifecycle of Jenkins pipeline jobs for EKS services.
Creates/updates Jenkins jobs via REST API, tracks pipelines in DB,
and triggers builds.

Flow:
  1. Fetch Jenkins connection config from pipeline_vendor_mst
  2. Generate Jenkinsfile script via DefaultEksJenkinsGenComponent
  3. Create/update Jenkins Pipeline job via JenkinsIntegration API
  4. Create pipeline_mst + pipeline_run_track DB records
  5. Optionally trigger first build
"""

import logging
import os
import uuid
from typing import Dict, Any, Optional, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enum import PipelineAgentEnum, PipelineRunStatusEnum
from app.integrations.jenkins_integration import JenkinsIntegration
from app.integrations.github_auth import GitHubAppAuth
from app.plugin.default.default_eks_jenkins_gen_component import DefaultEksJenkinsGenComponent
from app.repository.github_app_installation_mst_repository import GitHubAppInstallationMstRepository
from app.repository.pipeline_mst_repository import PipelineMstRepository
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
from app.repository.pipeline_vendor_mst_repository import PipelineVendorMstRepository
from app.repository.service_config_repository import ServiceConfigRepository

logger = logging.getLogger(__name__)


def _generate_job_name(service_name: str, environment: str, tenant_code: str = "") -> str:
    """Generate a consistent Jenkins job name from tenant + service + environment."""
    if tenant_code:
        return f"{tenant_code}-{service_name}-{environment}-pipeline".lower().replace(" ", "-")
    return f"{service_name}-{environment}-pipeline".lower().replace(" ", "-")


def _extract_github_org(repo_url: str) -> str:
    """Extract GitHub org/owner from a repo URL like 'https://github.com/owner/repo' or 'owner/repo'."""
    url = repo_url
    if url.startswith("https://github.com/"):
        url = url[len("https://github.com/"):]
    if url.startswith("http://github.com/"):
        url = url[len("http://github.com/"):]
    url = url.rstrip("/").removesuffix(".git")
    return url.split("/")[0] if "/" in url else ""


class JenkinsProvisioningService:
    """Creates and manages Jenkins pipeline jobs for EKS services."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.jenkins_gen = DefaultEksJenkinsGenComponent()
        self._model_serving_gen: Optional["DefaultModelServingJenkinsGenComponent"] = None

    def _get_generator(self, config_snapshot: Dict):
        """Return the appropriate Jenkinsfile generator based on service type."""
        if config_snapshot.get("service_type") == "MODEL_SERVING":
            if self._model_serving_gen is None:
                from app.plugin.default.default_model_serving_jenkins_gen_component import DefaultModelServingJenkinsGenComponent
                self._model_serving_gen = DefaultModelServingJenkinsGenComponent()
            return self._model_serving_gen
        return self.jenkins_gen

    async def _generate_github_token(self, repo_url: str) -> str:
        """Generate a short-lived GitHub App installation token for the repo's org."""
        github_org = _extract_github_org(repo_url)
        if not github_org:
            raise ValueError(f"Cannot extract GitHub org from repo URL: {repo_url}")

        install_repo = GitHubAppInstallationMstRepository(self.db)
        installation = await install_repo.get_by_github_org(github_org)
        if not installation:
            raise ValueError(f"No GitHub App installation found for org '{github_org}'")

        auth = GitHubAppAuth(
            app_id=settings.github_app_id,
            private_key=settings.github_app_private_key,
        )
        return await auth.get_installation_token(installation.installation_id)

    async def _get_jenkins_client(
        self,
        tenant_code: str,
        environment: str,
        service_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
    ) -> tuple:
        """
        Build JenkinsIntegration client from env vars and fetch vendor record from DB.

        Jenkins credentials come from environment variables (single shared instance).
        The vendor record is still looked up for DB tracking (pipeline_mst FK).
        """
        # Jenkins auth from env vars
        jenkins_url = settings.jenkins_url
        jenkins_user = settings.jenkins_user
        jenkins_api_token = settings.jenkins_api_token

        if not all([jenkins_url, jenkins_user, jenkins_api_token]):
            raise ValueError(
                "Jenkins env vars not configured. "
                "Required: JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN"
            )

        # Vendor record from DB (for pipeline_mst FK)
        vendor_repo = PipelineVendorMstRepository(self.db)
        vendor = await vendor_repo.get_by_service_hierarchy(
            service_code=service_code or "",
            resource_group_code=resource_group_code or "",
            application_code=application_code or "",
            tenant_code=tenant_code,
            environment=environment,
        )
        if not vendor:
            raise ValueError(
                f"No Jenkins pipeline_vendor_mst found for tenant={tenant_code}, env={environment}. "
                "Please configure a pipeline vendor with pipeline_agent_enum='jenkins'."
            )

        client = JenkinsIntegration(jenkins_url, jenkins_user, jenkins_api_token)
        return client, vendor

    async def provision_pipeline(
        self,
        service_name: str,
        config_snapshot: Dict[str, Any],
        tenant_code: str,
        transaction_code: Optional[str] = None,
        table_name: str = "SERVICE_CONFIG",
        service_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
        trigger_first_build: bool = False,
        queue_codes: Optional[List[str]] = None,
    ) -> Dict:
        """
        Provision a Jenkins pipeline job for an EKS service.

        1. Generate Jenkinsfile script from template
        2. Create/update Jenkins Pipeline job via API
        3. Create pipeline_mst DB record
        4. Optionally trigger first build

        Returns:
            {status, job_name, job_url, pipeline_code}
        """
        environment = config_snapshot.get("environment") or "stage"

        # Resolve transaction_code (service_config.code) from parameter or config_snapshot
        if not transaction_code:
            transaction_code = config_snapshot.get("service_config_code") or config_snapshot.get("code")
        if not transaction_code:
            raise ValueError(
                "transaction_code is required to create a pipeline_mst record. "
                "Pass it explicitly or include 'service_config_code' in config_snapshot."
            )

        # Resolve service_code for vendor lookup (still needed for _get_jenkins_client)
        if not service_code:
            service_code = config_snapshot.get("services_mst_code") or config_snapshot.get("service_mst_code")

        # Get Jenkins client
        client, vendor = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
            service_code=service_code,
            resource_group_code=resource_group_code,
            application_code=application_code,
        )

        # Generate Jenkinsfile (model-serving uses a different generator — no build stage)
        generator = self._get_generator(config_snapshot)
        jenkinsfile_content = await generator.generate_jenkinsfile(
            config_snapshot=config_snapshot,
            db=self.db,
            tenant_code=tenant_code,
        )

        # Create/update Jenkins job
        job_name = _generate_job_name(service_name, environment, tenant_code)
        description = f"EKS deployment for {service_name} ({environment}) - Managed by ObsTool"

        result = await client.create_or_update_pipeline_job(
            name=job_name,
            script=jenkinsfile_content,
            description=description,
        )

        if result.get("status") != "success":
            logger.error("Failed to provision Jenkins job %s: %s", job_name, result)
            return result

        job_url = result.get("job_url", f"{client.jenkins_url}/job/{job_name}")

        # Create or reuse pipeline_mst record (avoid duplicates on repeated saves)
        pipeline_repo = PipelineMstRepository(self.db)
        repo_url = config_snapshot.get("repository") or ""
        repo_branch = (config_snapshot.get("branches") or ["main"])[0] if isinstance(
            config_snapshot.get("branches"), list
        ) else config_snapshot.get("branches") or "main"

        existing = await pipeline_repo.check_pipeline_exists(
            transaction_code=transaction_code,
            repo_url=repo_url,
            branch=repo_branch,
            table_name=table_name,
        )

        if existing:
            pipeline_code = existing.code
            existing.deployment_config = {
                **(existing.deployment_config or {}),
                "jenkins_job_name": job_name,
                "jenkins_job_url": job_url,
                "ci_provider": "jenkins",
                "queue_codes": queue_codes or [],
            }
            logger.info("Reusing existing pipeline_mst %s for job %s", pipeline_code, job_name)
        else:
            pipeline_code = f"pipeline-{uuid.uuid4().hex[:12]}"
            await pipeline_repo.create(
                code=pipeline_code,
                name=job_name,
                description=description,
                transaction_code=transaction_code,
                table_name=table_name,
                tenant_code=tenant_code,
                pipeline_vendor_mst_code=vendor.code,
                repo_url=repo_url,
                repo_branch=repo_branch,
                language_ref_code=config_snapshot.get("language_ref_code"),
                deployment_config={
                    "jenkins_job_name": job_name,
                    "jenkins_job_url": job_url,
                    "ci_provider": "jenkins",
                    "queue_codes": queue_codes or [],
                },
            )

        # Pre-calculate and store URL for MODEL_SERVING before Jenkins starts.
        # Format: https://{tenant}-{service}-predictor.models.devlift.ai
        # Knative domain-template is "{{.Name}}.{{.Domain}}" (no namespace).
        # Wildcard CNAME *.models.devlift.ai → Kourier NLB (internet-facing).
        # TLS terminated at NLB via ACM cert + AWS Load Balancer Controller.
        if config_snapshot.get("service_type") == "MODEL_SERVING" and transaction_code:
            try:
                import re as _re
                _svc = service_name
                if _svc.lower().endswith("-service"):
                    _svc = _svc[:-8]
                safe_name = _re.sub(r"[^a-z0-9-]", "-", _svc.lower()).strip("-")
                k8s_name = f"{tenant_code}-{safe_name}"
                model_serving_url = f"https://{k8s_name}-predictor.models.devlift.ai"

                svc_config_repo = ServiceConfigRepository(self.db)
                await svc_config_repo.update_alb_url_by_codes([transaction_code], model_serving_url)
                logger.info("Pre-calculated MODEL_SERVING URL: %s", model_serving_url)
            except Exception as exc:
                logger.warning("Failed to pre-calculate model serving URL: %s", exc)

        # Optionally trigger first build (with fresh GitHub token if repo exists)
        run_code = None
        build_result = None
        if trigger_first_build:
            # Generate run_code BEFORE triggering so Jenkins can forward it in webhooks
            run_track_repo = PipelineRunTrackRepository(self.db)
            run_code = f"run-{uuid.uuid4().hex[:12]}"
            await run_track_repo.create(
                code=run_code,
                pipeline_mst_code=pipeline_code,
                status=PipelineRunStatusEnum.PENDING.value,
                log_url=job_url,
                transaction_queue_code=queue_codes or [],
            )

            build_params = {"OBS_TOOL_RUN_CODE": run_code}
            build_params["SERVICE_TYPE"] = "MODEL_SERVING" if config_snapshot.get("service_type") == "MODEL_SERVING" else "REGULAR"

            # Generate separate GitHub tokens for infra repo and source repo.
            # They may belong to different GitHub orgs/installations.
            from app.utils.tenant_config import get_tenant_config
            tenant_cfg = await get_tenant_config(tenant_code, self.db)
            infra_repo = tenant_cfg.github_infra_repository
            if infra_repo:
                build_params["GITHUB_TOKEN"] = await self._generate_github_token(
                    f"https://github.com/{infra_repo}"
                )
            if repo_url:
                build_params["SOURCE_GITHUB_TOKEN"] = await self._generate_github_token(repo_url)
            build_result = await client.trigger_build(
                job_name, parameters=build_params
            )

        await self.db.flush()

        logger.info(
            "Provisioned Jenkins pipeline: job=%s, pipeline_code=%s", job_name, pipeline_code
        )

        return {
            "status": "success",
            "job_name": job_name,
            "job_url": job_url,
            "pipeline_code": pipeline_code,
            "first_build_triggered": bool(build_result and build_result.get("status") == "success"),
            "run_code": run_code,
        }

    async def trigger_build_for_pipeline(
        self,
        job_name: str,
        pipeline_code: str,
        config_snapshot: Dict[str, Any],
        tenant_code: str,
        repo_url: str = "",
        queue_codes: Optional[List[str]] = None,
    ) -> Dict:
        """
        Trigger a Jenkins build for an already-provisioned pipeline.

        Called after infra repo PRs are merged so manifests exist when
        Jenkins fetches them.
        """
        environment = config_snapshot.get("environment") or "stage"
        service_code = config_snapshot.get("services_mst_code") or config_snapshot.get("service_mst_code")
        resource_group_code = config_snapshot.get("resource_group_mst_code")
        application_code = config_snapshot.get("applications_mst_code")

        client, _ = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
            service_code=service_code,
            resource_group_code=resource_group_code,
            application_code=application_code,
        )

        job_url = f"{client.jenkins_url}/job/{job_name}"

        # Pre-calculate and store URL for MODEL_SERVING (idempotent — safe on re-deploys)
        if config_snapshot.get("service_type") == "MODEL_SERVING":
            try:
                from app.repository.pipeline_mst_repository import PipelineMstRepository
                pipeline_repo = PipelineMstRepository(self.db)
                pipeline = await pipeline_repo.get_by_code(pipeline_code)
                if pipeline and pipeline.transaction_code:
                    import re as _re
                    _svc = config_snapshot.get("service_name", "")
                    if _svc.lower().endswith("-service"):
                        _svc = _svc[:-8]
                    safe_name = _re.sub(r"[^a-z0-9-]", "-", _svc.lower()).strip("-")
                    k8s_name = f"{tenant_code}-{safe_name}"
                    model_serving_url = f"https://{k8s_name}-predictor.models.devlift.ai"
                    svc_config_repo = ServiceConfigRepository(self.db)
                    await svc_config_repo.update_alb_url_by_codes([pipeline.transaction_code], model_serving_url)
                    logger.info("Re-stored MODEL_SERVING URL: %s", model_serving_url)
            except Exception as exc:
                logger.warning("Failed to store model serving URL on re-deploy: %s", exc)

        # Create run track record
        run_track_repo = PipelineRunTrackRepository(self.db)
        run_code = f"run-{uuid.uuid4().hex[:12]}"
        await run_track_repo.create(
            code=run_code,
            pipeline_mst_code=pipeline_code,
            status=PipelineRunStatusEnum.PENDING.value,
            log_url=job_url,
            transaction_queue_code=queue_codes or [],
        )

        build_params: Dict[str, str] = {"OBS_TOOL_RUN_CODE": run_code}
        build_params["SERVICE_TYPE"] = "MODEL_SERVING" if config_snapshot.get("service_type") == "MODEL_SERVING" else "REGULAR"

        # Generate separate GitHub tokens for infra repo and source repo
        from app.utils.tenant_config import get_tenant_config
        tenant_cfg = await get_tenant_config(tenant_code, self.db)
        infra_repo = tenant_cfg.github_infra_repository
        if infra_repo:
            build_params["GITHUB_TOKEN"] = await self._generate_github_token(
                f"https://github.com/{infra_repo}"
            )
        if repo_url:
            build_params["SOURCE_GITHUB_TOKEN"] = await self._generate_github_token(repo_url)

        build_result = await client.trigger_build(job_name, parameters=build_params)
        await self.db.flush()

        logger.info("Triggered Jenkins build post-merge: job=%s, run_code=%s", job_name, run_code)

        return {**build_result, "run_code": run_code}

    async def provision_pipeline_job(
        self,
        job_name: str,
        jenkinsfile_content: str,
        tenant_code: str,
        environment: str,
        transaction_code: Optional[str] = None,
        table_name: str = "INFRASTRUCTURE",
        infrastructure_mst_code: Optional[str] = None,
        trigger_build: bool = False,
        queue_codes: Optional[List[str]] = None,
        build_params: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """
        Generic Jenkins pipeline job provisioner.

        Creates (or updates) a Jenkins Pipeline job from a pre-generated
        Jenkinsfile, records it in the database for webhook tracking, and
        optionally triggers the first build.

        This is the reusable entry point for any operation that needs a
        Jenkins pipeline — the caller controls what the Jenkinsfile does.

        Current consumers:
          - Helm chart deployments  (postgres, mysql, mongodb, kafka, etc.)
          - EKS service operations  (stop, restart, delete)

        Unlike provision_pipeline() (EKS service deploy), this method:
          - Accepts a pre-generated Jenkinsfile (no internal generation)
          - Does not require service_code (not tied to a specific service)
          - Does not inject a GitHub token (no source code checkout needed)

        Flow:
          1. Get Jenkins client for the tenant/environment
          2. Create or update the Jenkins Pipeline job via REST API
          3. Create or reuse a pipeline_mst DB record (for webhook tracking)
          4. Trigger the build (if trigger_build=True)
          5. Create a pipeline_run_track record (so webhook can update status)

        Args:
            job_name:              Short name for the job (used to build full job name)
            jenkinsfile_content:   Pre-generated Jenkinsfile (Groovy pipeline script)
            tenant_code:           Tenant code
            environment:           Target environment (stage, prod, etc.)
            transaction_code:      FK to source table for pipeline_mst linkage
            table_name:            Source table name for pipeline_mst linkage
            infrastructure_mst_code: FK to infrastructure_mst (optional)
            trigger_build:         Whether to trigger a build immediately
            queue_codes:           Queue codes for webhook status tracking

        Returns:
            {status, job_name, job_url, pipeline_code, run_code, first_build_triggered}
        """
        client, vendor = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
        )

        full_job_name = f"{tenant_code}-{job_name}-{environment}-pipeline".lower().replace(" ", "-")
        description = f"Pipeline for {job_name} ({environment}) - Managed by ObsTool"

        result = await client.create_or_update_pipeline_job(
            name=full_job_name,
            script=jenkinsfile_content,
            description=description,
        )

        if result.get("status") != "success":
            logger.error("Failed to provision Jenkins job %s: %s", full_job_name, result)
            return result

        job_url = result.get("job_url", f"{client.jenkins_url}/job/{full_job_name}")

        # Create or reuse pipeline_mst record for webhook tracking
        pipeline_code = None
        if transaction_code:
            pipeline_repo = PipelineMstRepository(self.db)
            existing = await pipeline_repo.check_pipeline_exists(
                transaction_code=transaction_code,
                repo_url="",
                branch="main",
                table_name=table_name,
            )

            if existing:
                pipeline_code = existing.code
                existing.deployment_config = {
                    **(existing.deployment_config or {}),
                    "jenkins_job_name": full_job_name,
                    "jenkins_job_url": job_url,
                    "ci_provider": "jenkins",
                    "queue_codes": queue_codes or [],
                }
                logger.info("Reusing existing pipeline_mst %s for job %s", pipeline_code, full_job_name)
            else:
                pipeline_code = f"pipeline-{uuid.uuid4().hex[:12]}"
                await pipeline_repo.create(
                    code=pipeline_code,
                    name=full_job_name,
                    description=description,
                    transaction_code=transaction_code,
                    table_name=table_name,
                    tenant_code=tenant_code,
                    pipeline_vendor_mst_code=vendor.code,
                    repo_url="",
                    repo_branch="main",
                    language_ref_code=None,
                    deployment_config={
                        "jenkins_job_name": full_job_name,
                        "jenkins_job_url": job_url,
                        "ci_provider": "jenkins",
                        "queue_codes": queue_codes or [],
                    },
                )
                logger.info("Created pipeline_mst %s for job %s", pipeline_code, full_job_name)

        # Trigger build and create run_track for webhook status syncing
        run_code = None
        if trigger_build:
            # Create run_track BEFORE triggering so webhook can find it
            if pipeline_code:
                run_track_repo = PipelineRunTrackRepository(self.db)
                run_code = f"run-{uuid.uuid4().hex[:12]}"
                await run_track_repo.create(
                    code=run_code,
                    pipeline_mst_code=pipeline_code,
                    status=PipelineRunStatusEnum.PENDING.value,
                    log_url=job_url,
                    transaction_queue_code=queue_codes or [],
                )

            merged_params: Dict[str, str] = {**(build_params or {})}
            if run_code:
                merged_params["OBS_TOOL_RUN_CODE"] = run_code
            build_result = await client.trigger_build(full_job_name, parameters=merged_params or None)
            build_triggered = build_result.get("status") == "success"
            logger.info(
                "Triggered Jenkins build: job=%s triggered=%s",
                full_job_name, build_triggered,
            )

        await self.db.flush()

        logger.info("Provisioned Jenkins pipeline job: job=%s, pipeline_code=%s", full_job_name, pipeline_code)

        return {
            "status": "success",
            "job_name": full_job_name,
            "job_url": job_url,
            "pipeline_code": pipeline_code,
            "run_code": run_code,
            "first_build_triggered": run_code is not None,
        }

    async def trigger_build(
        self,
        service_name: str,
        environment: str,
        tenant_code: str,
        repo_url: str = "",
        commit_sha: Optional[str] = None,
        transaction_code: Optional[str] = None,
        service_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
        queue_codes: Optional[List[str]] = None,
    ) -> Dict:
        """Trigger a Jenkins build for an existing pipeline job."""
        client, vendor = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
            service_code=service_code,
            resource_group_code=resource_group_code,
            application_code=application_code,
        )

        job_name = _generate_job_name(service_name, environment, tenant_code)

        # Find pipeline_mst and create run_track BEFORE triggering build
        pipeline_repo = PipelineMstRepository(self.db)
        pipeline = await pipeline_repo.get_by(
            transaction_code=transaction_code,
            table_name="SERVICE_CONFIG",
        ) if transaction_code else None

        run_code = None
        if pipeline:
            effective_queue_codes = queue_codes or []
            if not effective_queue_codes:
                deployment_config = pipeline.deployment_config or {}
                effective_queue_codes = deployment_config.get("queue_codes", [])

            run_track_repo = PipelineRunTrackRepository(self.db)
            run_code = f"run-{uuid.uuid4().hex[:12]}"
            await run_track_repo.create(
                code=run_code,
                pipeline_mst_code=pipeline.code,
                status=PipelineRunStatusEnum.RUNNING.value,
                commit_sha=commit_sha,
                log_url=f"{client.jenkins_url}/job/{job_name}",
                transaction_queue_code=effective_queue_codes,
            )
            await self.db.flush()

        # Build params: GitHub token + run tracking code
        build_params = {}
        if run_code:
            build_params["OBS_TOOL_RUN_CODE"] = run_code
        if repo_url:
            try:
                github_token = await self._generate_github_token(repo_url)
                build_params["GITHUB_TOKEN"] = github_token
            except Exception as e:
                logger.warning("Failed to generate GitHub token for build: %s", e)

        result = await client.trigger_build(job_name, parameters=build_params or None)

        return result

    async def get_build_status(
        self,
        service_name: str,
        environment: str,
        tenant_code: str,
        service_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
    ) -> Dict:
        """Get the latest build status from Jenkins."""
        client, _ = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
            service_code=service_code,
            resource_group_code=resource_group_code,
            application_code=application_code,
        )
        job_name = _generate_job_name(service_name, environment, tenant_code)
        return await client.get_last_build_status(job_name)

    async def deprovision_pipeline(
        self,
        service_name: str,
        environment: str,
        tenant_code: str,
        service_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        application_code: Optional[str] = None,
    ) -> Dict:
        """Delete a Jenkins job and clean up DB records."""
        client, _ = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
            service_code=service_code,
            resource_group_code=resource_group_code,
            application_code=application_code,
        )
        job_name = _generate_job_name(service_name, environment, tenant_code)
        return await client.delete_job(job_name)

    # ------------------------------------------------------------------
    # Infrastructure-apply pipeline (S3 / SQS / DynamoDB / IAM)
    # ------------------------------------------------------------------

    _INFRA_APPLY_TEMPLATE_PATH = os.path.join(
        os.path.dirname(__file__),
        "..", "..", "templates", "terragrunt", "jenkins", "Jenkinsfile-infra-apply",
    )

    # (name, default, description) tuples mirrored from the Jenkinsfile's parameters{} block.
    # Must be declared on the job XML — Jenkins drops any /buildWithParameters value
    # whose name isn't pre-registered there.
    _INFRA_APPLY_JOB_PARAMETERS = [
        ("OBS_TOOL_RUN_CODE",     "",     "ObsTool run code for webhook correlation"),
        ("RESOURCE_TYPE",         "",     "s3 | sqs | dynamodb | iam"),
        ("RESOURCE_CODE",         "",     "INFRA_<TYPE>_<id>; for iam: IAM_<tenant>_<env>"),
        ("TENANT",                "",     "Tenant code"),
        ("ENVIRONMENT",           "",     "Target environment"),
        ("TERRAGRUNT_PATH",       "",     "Path to resource directory inside the tenant infra repo"),
        ("INFRA_REPO",            "",     "owner/repo of the tenant infra repo"),
        ("INFRA_BRANCH",          "main", "Branch of the tenant infra repo"),
        ("BASE_INFRA_ORG",        "",     "Org of the base infra repo (layers source)"),
        ("BASE_INFRA_REPO",       "",     "Repo of the base infra repo (layers source)"),
        ("BASE_INFRA_BRANCH",     "main", "Branch of the base infra repo"),
        ("ROLE_ARN",              "",     "Tenant default-role ARN (resource pipelines only)"),
        ("EXPECTED_ACTIONS",      "",     "Comma-separated IAM actions to verify"),
        ("EXPECTED_RESOURCE_ARNS", "",    "Comma-separated resource ARNs to verify"),
    ]

    def _load_infra_apply_jenkinsfile(self) -> str:
        with open(self._INFRA_APPLY_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        webhook_url = f"{settings.devlift_backend_url}/api/v1/webhooks/jenkins-webhook"
        content = content.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        content = content.replace("{{WEBHOOK_SECRET}}", settings.pipeline_webhook_secret or "")
        return content

    @staticmethod
    def _infra_apply_job_name(tenant_code: str, environment: str) -> str:
        return _generate_job_name("infra-apply", environment, tenant_code)

    async def ensure_infra_apply_pipeline(
        self,
        tenant_code: str,
        environment: str,
    ) -> Dict[str, str]:
        """Idempotently create the per-tenant infra-apply Jenkins job and pipeline_mst row.

        Returns:
            {job_name, job_url, pipeline_code}
        """
        client, vendor = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
        )

        job_name = self._infra_apply_job_name(tenant_code, environment)
        description = (
            f"Infrastructure apply pipeline for {tenant_code} ({environment}) - Managed by ObsTool"
        )
        jenkinsfile = self._load_infra_apply_jenkinsfile()

        result = await client.create_or_update_pipeline_job(
            name=job_name,
            script=jenkinsfile,
            description=description,
            extra_parameters=self._INFRA_APPLY_JOB_PARAMETERS,
        )
        if result.get("status") != "success":
            logger.error("Failed to provision infra-apply Jenkins job %s: %s", job_name, result)
            return result

        job_url = result.get("job_url", f"{client.jenkins_url}/job/{job_name}")

        pipeline_repo = PipelineMstRepository(self.db)
        existing = await pipeline_repo.check_pipeline_exists(
            transaction_code=f"INFRA_APPLY_{tenant_code}_{environment}",
            repo_url="",
            branch="main",
            table_name="INFRASTRUCTURE",
        )

        if existing:
            pipeline_code = existing.code
            existing.deployment_config = {
                **(existing.deployment_config or {}),
                "jenkins_job_name": job_name,
                "jenkins_job_url": job_url,
                "ci_provider": "jenkins",
            }
        else:
            pipeline_code = f"pipeline-{uuid.uuid4().hex[:12]}"
            await pipeline_repo.create(
                code=pipeline_code,
                name=job_name,
                description=description,
                transaction_code=f"INFRA_APPLY_{tenant_code}_{environment}",
                table_name="INFRASTRUCTURE",
                tenant_code=tenant_code,
                pipeline_vendor_mst_code=vendor.code,
                repo_url="",
                repo_branch="main",
                language_ref_code=None,
                deployment_config={
                    "jenkins_job_name": job_name,
                    "jenkins_job_url": job_url,
                    "ci_provider": "jenkins",
                },
            )

        await self.db.flush()
        return {"job_name": job_name, "job_url": job_url, "pipeline_code": pipeline_code}

    async def trigger_infra_apply_build(
        self,
        tenant_code: str,
        environment: str,
        resource_type: str,
        resource_code: str,
        terragrunt_path: str,
        infra_repo: str,
        infra_branch: str = "main",
        role_arn: str = "",
        expected_actions: str = "",
        expected_resource_arns: str = "",
        queue_codes: Optional[List[str]] = None,
        ensured: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """Trigger one infra-apply Jenkins build and create its run_track row.

        ``resource_type`` is one of ``s3 | sqs | dynamodb | iam``.
        For ``iam`` runs, ``role_arn`` / ``expected_actions`` / ``expected_resource_arns``
        should be empty — verification is skipped.

        ``ensured`` lets callers hoist the one-time pipeline provisioning out
        of a parallel fan-out (avoids racing N concurrent
        ``create_or_update_pipeline_job`` requests against Jenkins for the same
        job). If ``None``, this method calls :meth:`ensure_infra_apply_pipeline`
        itself — fine for single-shot use, but DO NOT do this concurrently.
        """
        if ensured is None:
            ensured = await self.ensure_infra_apply_pipeline(tenant_code, environment)
        job_name = ensured.get("job_name")
        job_url = ensured.get("job_url")
        pipeline_code = ensured.get("pipeline_code")
        if not (job_name and pipeline_code):
            raise RuntimeError(
                f"ensure_infra_apply_pipeline did not return a job_name/pipeline_code "
                f"for tenant={tenant_code} env={environment}: {ensured}"
            )

        client, _ = await self._get_jenkins_client(
            tenant_code=tenant_code,
            environment=environment,
        )

        run_track_repo = PipelineRunTrackRepository(self.db)
        run_code = f"run-{uuid.uuid4().hex[:12]}"
        await run_track_repo.create(
            code=run_code,
            pipeline_mst_code=pipeline_code,
            status=PipelineRunStatusEnum.PENDING.value,
            log_url=job_url,
            transaction_queue_code=queue_codes or [],
        )

        github_token = await self._generate_github_token(f"https://github.com/{infra_repo}")

        build_params = {
            "OBS_TOOL_RUN_CODE": run_code,
            "RESOURCE_TYPE": resource_type,
            "RESOURCE_CODE": resource_code,
            "TENANT": tenant_code,
            "ENVIRONMENT": environment,
            "TERRAGRUNT_PATH": terragrunt_path,
            "INFRA_REPO": infra_repo,
            "INFRA_BRANCH": infra_branch,
            "BASE_INFRA_ORG": settings.onboarding_source_org,
            "BASE_INFRA_REPO": settings.onboarding_source_repo,
            "BASE_INFRA_BRANCH": settings.onboarding_source_branch,
            "GITHUB_TOKEN": github_token,
            "ROLE_ARN": role_arn,
            "EXPECTED_ACTIONS": expected_actions,
            "EXPECTED_RESOURCE_ARNS": expected_resource_arns,
        }

        build_result = await client.trigger_build(job_name, parameters=build_params)
        await self.db.flush()

        logger.info(
            "Triggered infra-apply build: job=%s run_code=%s resource=%s/%s",
            job_name, run_code, resource_type, resource_code,
        )
        return {**build_result, "run_code": run_code, "job_name": job_name}
