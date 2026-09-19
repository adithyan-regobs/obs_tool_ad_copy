import logging
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from uuid import uuid4

from app.repository.monitoring_policy_defaults_ref_repository import MonitoringPolicyDefaultsRefRepository
from app.repository.monitoring_policy_overrides_mst_repository import MonitoringPolicyOverridesMstRepository
from app.schemas.monitoring_policy_schemas import AlertPolicyResponse
from app.schemas.monitoring_policy_override_schemas import (
    CreateMonitoringPolicyOverrideRequest,
    MonitoringPolicyOverrideResponse,
    UpdateMonitoringPolicyOverrideRequest,
    UpdateMonitoringPolicyOverrideResponse
)
from app.domain.validators.monitoring_policy_override_validator import MonitoringPolicyOverrideValidator, MonitoringPolicyOverrideValidationError
from app.domain.factories.monitoring_policy_override_factory import make_monitoring_policy_override
from app.db.models.monitoring_policy_defaults_ref_model import MonitoringPolicyDefaultsRefModel
from app.db.models.monitoring_policy_overrides_mst_model import MonitoringPolicyOverridesMstModel

logger = logging.getLogger(__name__)


class MonitoringPolicyService:
    """Service for managing monitoring policies - returns both defaults and overrides"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.defaults_repository = MonitoringPolicyDefaultsRefRepository(session)
        self.overrides_repository = MonitoringPolicyOverridesMstRepository(session)

    async def get_all_alert_policies(
        self,
        tenant_code: str,
        application_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
        infrastructuretype_code: Optional[str] = None,
        alerttype_code: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[AlertPolicyResponse], int]:
        """
        Get all alert policies for a tenant - returns BOTH defaults and overrides as separate records.

        This method returns:
        1. All default policies (filtered by infrastructure type and alert type if specified)
        2. All override policies for the tenant (filtered by scope parameters)

        Args:
            tenant_code: Tenant code for isolation
            application_code: Optional application filter
            resource_group_code: Optional resource group filter
            infrastructuretype_code: Optional infrastructure type filter
            alerttype_code: Optional alert type filter
            skip: Pagination skip
            limit: Pagination limit

        Returns:
            Tuple of (list of policies from both tables, total count)
        """
        logger.info(f"Fetching alert policies for tenant: {tenant_code}")

        all_policies = []

        # Step 1: Fetch default policies with filters
        defaults, total_defaults = await self.defaults_repository.get_all_defaults_with_filters(
            infrastructuretype_code=infrastructuretype_code,
            alerttype_code=alerttype_code,
            skip=0,  # Get all defaults first (before pagination)
            limit=10000
        )

        logger.info(f"Found {len(defaults)} default policies")

        # Convert defaults to response format
        for default in defaults:
            policy = {
                "policy_code": default["code"],
                "policy_name": default["name"],
                "description":default["description"],
                "monitoring_policy_defaults_ref_code": default["code"],
                "tenants_mst_code": None,
                "applications_mst_code": None,
                "resource_group_mst_code": None,
                "infrastructuretype_ref_code": default["infrastructuretype_ref_code"],
                "alerttype_ref_code": default["alerttype_ref_code"],
                "comparator": default["comparator"],
                "threshold_value": default["threshold_value"],
                "threshold_unit": default["threshold_unit"],
                "eval_window": default["eval_window"],
                "for_duration": default["for_duration"],
                "no_data": default["no_data"],
                "severity": default["severity"],
                "is_override": False,
                "override_source": None,
                "is_active": default["is_active"],
                "created_at": None  # Defaults don't have created_at for sorting
            }
            all_policies.append(policy)

        # Step 2: Fetch applicable overrides for tenant
        overrides = await self.overrides_repository.get_overrides_for_tenant(
            tenant_code=tenant_code,
            application_code=application_code,
            resource_group_code=resource_group_code,
            infrastructuretype_code=infrastructuretype_code
        )

        logger.info(f"Found {len(overrides)} override policies for tenant {tenant_code}")

        # Convert overrides to response format
        for override in overrides:
            # Filter by alerttype_code if specified
            if alerttype_code and override.get("alerttype_ref_code") != alerttype_code:
                continue

            policy = {
                "policy_code": override["code"],
                "policy_name": override["name"],
                "description": override["description"],
                "monitoring_policy_defaults_ref_code": override["monitoring_policy_defaults_ref_code"],
                "tenants_mst_code": override["tenants_mst_code"],
                "applications_mst_code": override["applications_mst_code"],
                "resource_group_mst_code": override["resource_group_mst_code"],
                "infrastructuretype_ref_code": override["infrastructuretype_ref_code"],
                "alerttype_ref_code": override["alerttype_ref_code"],
                "comparator": override["comparator"],
                "threshold_value": override["threshold_value"],
                "threshold_unit": override["threshold_unit"],
                "eval_window": override["eval_window"],
                "for_duration": override["for_duration"],
                "no_data": override["no_data"],
                "severity": override["severity"],
                "is_override": True,
                "override_source": override["override_source"],
                "is_active": override["is_active"],
                "created_at": override["created_at"]  # Add created_at for sorting
            }
            all_policies.append(policy)

        # Step 3: Sort policies - overrides first, then by alert type, application, resource group, infrastructure type
        # Sort key:
        # 1. is_override (False=0, True=1) - reversed so True comes first
        # 2. created_at (None for defaults, datetime for overrides) - reversed for most recent first
        # 3. alert type - primary grouping (groups similar alerts together)
        # 4. application (None/"" sorted last)
        # 5. resource_group (None/"" sorted last)
        # 6. infrastructure type
        all_policies.sort(key=lambda p: (
            not p["is_override"],  # False (0) for overrides, True (1) for defaults
            p["created_at"] if p["created_at"] is None else -p["created_at"].timestamp(),  # Most recent first
            p["alerttype_ref_code"] or "",
            p["applications_mst_code"] or "",
            p["resource_group_mst_code"] or "",
            p["infrastructuretype_ref_code"] or ""
        ))

        # Step 4: Apply pagination
        total_count = len(all_policies)
        paginated_policies = all_policies[skip:skip + limit]

        # Step 5: Convert to Pydantic response models
        response_policies = [
            AlertPolicyResponse(**policy)
            for policy in paginated_policies
        ]

        logger.info(
            f"Returning {len(response_policies)} policies "
            f"(total: {total_count}) - {len(defaults)} defaults + {len(overrides)} overrides"
        )

        return response_policies, total_count

    async def create_policy_override(
        self,
        tenant_code: str,
        request: CreateMonitoringPolicyOverrideRequest
    ) -> MonitoringPolicyOverrideResponse:
        """
        Create a new monitoring policy override.

        Steps:
        1. Find the default policy by infrastructure type + alert type (via repository)
        2. Use factory to create override data dictionary
        3. Validate the override data
        4. Check for duplicate overrides with same scope (via repository)
        5. Create and save the override model (via repository)
        6. Return response with specificity score

        Args:
            request: Create override request from API
            tenant_code: Tenant code from JWT authentication (not from request)

        Returns:
            MonitoringPolicyOverrideResponse with created override details

        Raises:
            ValueError: If duplicate exists
            MonitoringPolicyOverrideValidationError: If validation fails
        """
        logger.info(
            f"Creating policy override: {request.name} for "
            f"{request.infrastructuretype_ref_code}/{request.alerttype_ref_code}"
        )

        # Step 1: Find default policy by infra type + alert type using repository
        default_policy = await self.defaults_repository.get_by_infra_and_alert_type(
            infrastructuretype_ref_code=request.infrastructuretype_ref_code,
            alerttype_ref_code=request.alerttype_ref_code
        )

        # Support null default policy
        monitoring_policy_defaults_ref_code = default_policy.code if default_policy else None
        logger.info(f"Default policy code: {monitoring_policy_defaults_ref_code}")

        # Step 2: Use factory to create override data dictionary with tenant_code from JWT
        override_data = make_monitoring_policy_override(
            request=request,
            monitoring_policy_defaults_ref_code=monitoring_policy_defaults_ref_code,
            tenant_code=tenant_code
        )

        # Step 3: Validate the override data using the same factory output
        MonitoringPolicyOverrideValidator.validate_override_data(override_data)

        logger.info("Override data validation passed")

        # Step 4: Check for duplicate override with same scope using repository
        # Duplicate check: same infra type + alert type + tenant + application + resource group
        #TODO Need to check this all test cases.
        existing_override = await self.overrides_repository.get_override_by_scope(
            monitoring_policy_defaults_ref_code=monitoring_policy_defaults_ref_code,
            tenants_mst_code=tenant_code,  # From JWT authentication
            applications_mst_code=request.applications_mst_code,
            resource_group_mst_code=request.resource_group_mst_code,
            infrastructuretype_ref_code=request.infrastructuretype_ref_code,
            alerttype_ref_code=request.alerttype_ref_code
        )
       
        if existing_override:
            raise ValueError(
                f"Override already exists for this scope. Existing override code: {existing_override.code}"
            )

        logger.info("No duplicate override found, proceeding with creation")

        # Step 5: Create override using base repository with override_data kwargs
        created_override = await self.overrides_repository.create(**override_data)

        logger.info(f"Created override with code: {created_override.code}")

        # Step 6: Calculate specificity score for response
        specificity_score = 0
        override_source_parts = []

        if created_override.resource_group_mst_code:
            specificity_score += 8
            override_source_parts.append(f"Resource Group: {created_override.resource_group_mst_code}")
        if created_override.infrastructuretype_ref_code:
            specificity_score += 4
            override_source_parts.append(f"Infrastructure: {created_override.infrastructuretype_ref_code}")
        if created_override.applications_mst_code:
            specificity_score += 2
            override_source_parts.append(f"Application: {created_override.applications_mst_code}")
        if created_override.tenants_mst_code:
            specificity_score += 1
            override_source_parts.append(f"Tenant: {created_override.tenants_mst_code}")

        override_source = " | ".join(override_source_parts) if override_source_parts else "Global"

        # Step 7: Build and return response
        response = MonitoringPolicyOverrideResponse(
            code=created_override.code,
            name=created_override.name,
            description=created_override.description,
            monitoring_policy_defaults_ref_code=created_override.monitoring_policy_defaults_ref_code,
            tenants_mst_code=created_override.tenants_mst_code,
            applications_mst_code=created_override.applications_mst_code,
            resource_group_mst_code=created_override.resource_group_mst_code,
            infrastructuretype_ref_code=created_override.infrastructuretype_ref_code,
            alerttype_ref_code=created_override.alerttype_ref_code,
            comparator=created_override.comparator,
            threshold_value=created_override.threshold_value,
            threshold_unit=created_override.threshold_unit,
            eval_window=created_override.eval_window,
            for_duration=created_override.for_duration,
            no_data=created_override.no_data,
            severity=created_override.severity,
            is_active=created_override.is_active,
            specificity_score=specificity_score,
            override_source=override_source
        )

        logger.info(f"Successfully created override: {created_override.code}")

        return response

    async def update_policy_override(
        self,
        tenant_code: str,
        override_code: str,
        request: UpdateMonitoringPolicyOverrideRequest
    ) -> UpdateMonitoringPolicyOverrideResponse:
        """
        Update or create a monitoring policy override (UPSERT operation).

        If override with given code exists → Update it
        If override doesn't exist → Create new one with provided code

        Only provided fields will be updated (partial updates supported).
        Immutable fields (code, scope fields) cannot be updated.

        Args:
            tenant_code: Tenant code from JWT authentication
            override_code: Unique code of the override to update/create
            request: Update request with optional fields

        Returns:
            UpdateMonitoringPolicyOverrideResponse with override details

        Raises:
            ValueError: If validation fails or duplicate scope detected
            MonitoringPolicyOverrideValidationError: If field validation fails
        """
        logger.info(f"UPSERT policy override: {override_code} for tenant: {tenant_code}")

        # Step 1: Try to get existing override by code
        existing_override = await self.overrides_repository.get_by_code(override_code)

        # Step 2: If NOT found → CREATE new override
        if not existing_override:
            logger.info(f"Override {override_code} not found - creating new override by copying from default policy")

            # 2a. Fetch default policy by code (override_code matches default policy code)
            # Example: override_code "alb_unhealthy_hosts_default" → fetch default with same code
            logger.debug(f"Fetching default policy by code: {override_code}")

            default_policy = await self.defaults_repository.get_by_code(override_code)

            # If no default policy found, raise error
            if not default_policy:
                raise ValueError(
                    f"Cannot create override '{override_code}': No default monitoring policy found with this code. "
                    f"Please verify that a default policy exists with code '{override_code}'."
                )

            # 2b. Build override data by copying ALL fields from default policy
            # Then override with provided update fields from request
            logger.debug(f"Copying all fields from default policy: {default_policy.code}")

            override_data = {
                'code': override_code,  # Use provided code
                'name': request.name if request.name is not None else default_policy.name,
                'description': request.description if request.description is not None else default_policy.description,
                'monitoring_policy_defaults_ref_code': default_policy.code,
                'tenants_mst_code': tenant_code,  # From JWT authentication
                'applications_mst_code': None,  # Override-specific scope fields default to None
                'resource_group_mst_code': None,
                'infrastructuretype_ref_code': default_policy.infrastructuretype_ref_code,
                'alerttype_ref_code': default_policy.alerttype_ref_code,
                'comparator': request.comparator if request.comparator is not None else default_policy.comparator,
                'threshold_value': request.threshold_value if request.threshold_value is not None else default_policy.threshold_value,
                'threshold_unit': request.threshold_unit if request.threshold_unit is not None else default_policy.threshold_unit,
                'eval_window': request.eval_window if request.eval_window is not None else default_policy.eval_window,
                'for_duration': request.for_duration if request.for_duration is not None else default_policy.for_duration,
                'no_data': request.no_data if request.no_data is not None else default_policy.no_data,
                'severity': request.severity if request.severity is not None else default_policy.severity,
                'is_active': request.is_active if request.is_active is not None else True,
                'is_deleted': False
            }

            logger.debug(f"Override data built - updated fields: {[k for k, v in request.model_dump().items() if v is not None]}")

            # 2c. Validate override data
            MonitoringPolicyOverrideValidator.validate_override_data(override_data)
            logger.debug("Override data validation passed")

            # 2d. Check for duplicate scope (same infra + alert + tenant + app + rg)
            duplicate = await self.overrides_repository.get_override_by_scope(
                monitoring_policy_defaults_ref_code=default_policy.code,
                tenants_mst_code=tenant_code,
                applications_mst_code=None,  # Default scope
                resource_group_mst_code=None,
                infrastructuretype_ref_code=default_policy.infrastructuretype_ref_code,
                alerttype_ref_code=default_policy.alerttype_ref_code
            )

            # If duplicate exists with different code → ERROR
            if duplicate and duplicate.code != override_code:
                raise ValueError(
                    f"Override already exists for this scope with code '{duplicate.code}'. "
                    f"Cannot create new override with code '{override_code}'."
                )

            # 2e. Create new override
            created_override = await self.overrides_repository.create(**override_data)
            await self.session.commit()

            logger.info(f"Successfully created new override: {created_override.code} (copied from {default_policy.code})")

            # 2f. Build and return response
            return self._build_override_response(created_override, operation="created")

        # Step 3: If found → UPDATE existing override
        logger.info(f"Override {override_code} found - updating existing override")

        # Step 2: Check if deleted
        if existing_override.is_deleted:
            raise ValueError(f"Policy override with code '{override_code}' has been deleted and cannot be updated")

        # Step 3: Build updates dictionary (only provided fields)
        updates = {}
        if request.name is not None:
            updates['name'] = request.name
        if request.description is not None:
            updates['description'] = request.description
        if request.comparator is not None:
            updates['comparator'] = request.comparator
        if request.threshold_value is not None:
            updates['threshold_value'] = request.threshold_value
        if request.threshold_unit is not None:
            updates['threshold_unit'] = request.threshold_unit
        if request.eval_window is not None:
            updates['eval_window'] = request.eval_window
        if request.for_duration is not None:
            updates['for_duration'] = request.for_duration
        if request.no_data is not None:
            updates['no_data'] = request.no_data
        if request.severity is not None:
            updates['severity'] = request.severity
        if request.is_active is not None:
            updates['is_active'] = request.is_active

        # Step 4: Validate at least one field provided
        if not updates:
            raise ValueError("No fields provided for update. Please provide at least one field to update.")

        logger.debug(f"Fields to update: {list(updates.keys())}")

        # Step 5: Validate updated values (basic validation)
        # Name validation
        if 'name' in updates and not updates['name'].strip():
            raise MonitoringPolicyOverrideValidationError(["Name cannot be empty or whitespace"])

        # Threshold validation
        if 'threshold_value' in updates and (updates['threshold_value'] < 0 or updates['threshold_value'] > 10000):
            raise MonitoringPolicyOverrideValidationError(["Threshold value must be between 0 and 10000"])

        # Window validations
        if 'eval_window' in updates and updates['eval_window'] <= 0:
            raise MonitoringPolicyOverrideValidationError(["Evaluation window must be greater than 0"])

        if 'for_duration' in updates and updates['for_duration'] <= 0:
            raise MonitoringPolicyOverrideValidationError(["Duration must be greater than 0"])

        # Step 6: Update in database
        updated_override = await self.overrides_repository.update_override(existing_override, updates)
        await self.session.commit()

        logger.info(f"Successfully updated override: {override_code} with fields: {list(updates.keys())}")

        # Step 7: Build and return response using helper method
        return self._build_override_response(updated_override, operation="updated")

    def _build_override_response(
        self,
        override: MonitoringPolicyOverridesMstModel,
        operation: str = "updated"
    ) -> UpdateMonitoringPolicyOverrideResponse:
        """
        Build response object for override create/update operations.

        Args:
            override: The override model object
            operation: "created" or "updated" for response message

        Returns:
            UpdateMonitoringPolicyOverrideResponse with complete override details
        """
        # Calculate specificity score
        specificity_score = 0
        override_source_parts = []

        if override.resource_group_mst_code:
            specificity_score += 8
            override_source_parts.append(f"Resource Group: {override.resource_group_mst_code}")

        if override.infrastructuretype_ref_code:
            specificity_score += 4
            override_source_parts.append(f"Infrastructure: {override.infrastructuretype_ref_code}")

        if override.applications_mst_code:
            specificity_score += 2
            override_source_parts.append(f"Application: {override.applications_mst_code}")

        if override.tenants_mst_code:
            specificity_score += 1
            override_source_parts.append(f"Tenant: {override.tenants_mst_code}")

        override_source = " | ".join(override_source_parts) if override_source_parts else "Global"

        # Build override response model
        override_response = MonitoringPolicyOverrideResponse(
            code=override.code,
            name=override.name,
            description=override.description,
            monitoring_policy_defaults_ref_code=override.monitoring_policy_defaults_ref_code,
            tenants_mst_code=override.tenants_mst_code,
            applications_mst_code=override.applications_mst_code,
            resource_group_mst_code=override.resource_group_mst_code,
            infrastructuretype_ref_code=override.infrastructuretype_ref_code,
            alerttype_ref_code=override.alerttype_ref_code,
            comparator=override.comparator,
            threshold_value=override.threshold_value,
            threshold_unit=override.threshold_unit,
            eval_window=override.eval_window,
            for_duration=override.for_duration,
            no_data=override.no_data,
            severity=override.severity,
            is_active=override.is_active,
            specificity_score=specificity_score,
            override_source=override_source
        )

        return UpdateMonitoringPolicyOverrideResponse(
            status="success",
            message=f"Policy override '{override.name}' {operation} successfully",
            override=override_response
        )