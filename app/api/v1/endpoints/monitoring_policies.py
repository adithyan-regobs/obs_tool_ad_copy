"""
API endpoints for Monitoring Policies management.

This module provides endpoints to retrieve alert policies (both defaults and overrides)
for tenants with filtering capabilities.
"""

import logging
from typing import Tuple
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.monitoring_policy_service import MonitoringPolicyService
from app.schemas.monitoring_policy_schemas import (
    GetAllAlertPoliciesRequest,
    GetAllAlertPoliciesResponse
)
from app.schemas.monitoring_policy_override_schemas import (
    CreateMonitoringPolicyOverrideRequest,
    CreateMonitoringPolicyOverrideResponse,
    UpdateMonitoringPolicyOverrideRequest,
    UpdateMonitoringPolicyOverrideResponse
)
from app.domain.validators.monitoring_policy_override_validator import MonitoringPolicyOverrideValidationError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/get-all-alert-policies",
    response_model=GetAllAlertPoliciesResponse,
    status_code=status.HTTP_200_OK,
    summary="Get all alert policies for a tenant",
    description="""
    Retrieve all alert policies (both defaults and overrides) for a specific tenant.

    Returns both MonitoringPolicyDefaultsRefModel and MonitoringPolicyOverridesMstModel records.

    **Filters:**
    - `tenant_code` (required): Tenant isolation
    - `application_code` (optional): Filter by application
    - `resource_group_code` (optional): Filter by resource group
    - `infrastructuretype_code` (optional): Filter by infrastructure type (ec2, rds, vm, etc.)
    - `alerttype_code` (optional): Filter by alert type (cpu_util, memory_usage, etc.)

    **Sorting:**
    Results are sorted by: Application → Resource Group → Infrastructure Type → Alert Type

    **Response Format:**
    Each policy includes:
    - `is_override`: Boolean indicating if it's from the overrides table
    - `override_source`: Description of override scope (e.g., "Tenant + Application")
    - All alert configuration fields (comparator, threshold_value, severity, etc.)
    - Scope information (tenants_mst_code, applications_mst_code, etc.)

    **Example Request:**
    ```json
    {
        "tenant_code": "acme_corp",
        "infrastructuretype_code": "ec2",
        "skip": 0,
        "limit": 100
    }
    ```

    **Example Response:**
    Returns both default policies (with null scope fields) and tenant-specific overrides.
    """,
    tags=["Monitoring Policies"]
)
async def get_all_alert_policies(
    request: GetAllAlertPoliciesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all alert policies for a tenant with optional filters.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: only returns policies for authenticated user's tenant
        - tenant_code automatically injected from JWT token

    Returns both default policies and tenant-specific overrides as separate records.
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log tenant isolation
        logger.info("="*80)
        logger.info("🔍 GET ALL ALERT POLICIES - TENANT ISOLATION CHECK")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   User Email: {user.email_id}")
        logger.info(f"   Tenant Code: {tenant.code}")
        logger.info(f"   Tenant Name: {tenant.name}")
        logger.info(f"   Application Filter: {request.application_code}")
        logger.info(f"   Infrastructure Type Filter: {request.infrastructuretype_code}")
        logger.info("="*80)

        # Initialize service
        policy_service = MonitoringPolicyService(db)

        # Get policies with tenant_code from JWT
        policies, total = await policy_service.get_all_alert_policies(
            tenant_code=tenant.code,
            application_code=request.application_code,
            resource_group_code=request.resource_group_code,
            infrastructuretype_code=request.infrastructuretype_code,
            alerttype_code=request.alerttype_code,
            skip=request.skip,
            limit=request.limit
        )

        logger.info(
            f"Successfully retrieved {len(policies)} policies "
            f"(total: {total}) for tenant: {tenant.code}"
        )

        # Build response
        return GetAllAlertPoliciesResponse(
            status="success",
            message=f"Retrieved {len(policies)} alert policies",
            total=total,
            skip=request.skip,
            limit=request.limit,
            policies=policies
        )

    except ValueError as ve:
        logger.error(f"Validation error in get_all_alert_policies: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error in get_all_alert_policies: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve alert policies: {str(e)}"
        )


@router.post(
    "/create-policy-override",
    response_model=CreateMonitoringPolicyOverrideResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new monitoring policy override",
    description="""
    Create a new monitoring policy override for custom alert thresholds.

    **How it works:**
    1. UI sends `infrastructuretype_ref_code` (e.g., "ec2") and `alerttype_ref_code` (e.g., "cpu_util")
    2. Backend automatically finds the matching default policy
    3. Creates override with custom threshold values and scope

    **Scope Hierarchy:**
    - **Global**: No scope fields specified (applies to all tenants)
    - **Tenant**: Only `tenants_mst_code` specified
    - **Application**: `tenants_mst_code` + `applications_mst_code`
    - **Resource Group**: `tenants_mst_code` + `applications_mst_code` + `resource_group_mst_code`

    **Specificity Scoring:**
    - Resource Group = 8 points
    - Infrastructure Type = 4 points
    - Application = 2 points
    - Tenant = 1 point

    **Validation Rules:**
    - Name required (max 255 characters)
    - If `resource_group_mst_code` specified, `applications_mst_code` is required
    - If `applications_mst_code` specified, `tenants_mst_code` is required
    - `threshold_value` must be between 0 and 10000
    - `eval_window` and `for_duration` must be greater than 0
    - No duplicate overrides with same scope allowed

    **Example Request:**
    ```json
    {
        "name": "Strict CPU Policy",
        "description": "Lower CPU threshold for high-availability services",
        "infrastructuretype_ref_code": "ec2",
        "alerttype_ref_code": "cpu_util",
        "applications_mst_code": null,
        "resource_group_mst_code": null,
        "comparator": "gt",
        "threshold_value": 70.0,
        "threshold_unit": "percent",
        "eval_window": 5,
        "for_duration": 10,
        "no_data": true,
        "severity": "P1",
        "is_active": true
    }
    ```
    """,
    tags=["Monitoring Policies"]
)
async def create_policy_override(
    request: CreateMonitoringPolicyOverrideRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new monitoring policy override.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: overrides created for authenticated user's tenant only
        - tenants_mst_code automatically injected from JWT token

    UI sends infrastructure type and alert type - backend finds the default policy automatically.
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log tenant isolation
        logger.info("="*80)
        logger.info("🔍 CREATE POLICY OVERRIDE - TENANT ISOLATION CHECK")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   User Email: {user.email_id}")
        logger.info(f"   Tenant Code: {tenant.code}")
        logger.info(f"   Tenant Name: {tenant.name}")
        logger.info(f"   Override Name: {request.name}")
        logger.info(f"   Infrastructure Type: {request.infrastructuretype_ref_code}")
        logger.info(f"   Alert Type: {request.alerttype_ref_code}")
        logger.info("="*80)

        # Initialize service
        policy_service = MonitoringPolicyService(db)

        # Create override with tenant_code from JWT (tenant_code first, then request)
        override = await policy_service.create_policy_override(tenant_code=tenant.code, request=request)

        logger.info(f"Successfully created policy override: {override.code}")

        # Build response
        return CreateMonitoringPolicyOverrideResponse(
            status="success",
            message=f"Policy override '{override.name}' created successfully",
            override_code=override.code,
            override=override
        )

    except MonitoringPolicyOverrideValidationError as ve:
        logger.error(f"Validation error in create_policy_override: {ve.errors}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Validation failed",
                "errors": ve.errors
            }
        )
    except ValueError as ve:
        logger.error(f"Value error in create_policy_override: {str(ve)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as e:
        logger.error(f"Error in create_policy_override: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create policy override: {str(e)}"
        )


@router.put(
    "/update-policy-override/{override_code}",
    response_model=UpdateMonitoringPolicyOverrideResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Monitoring Policy Override",
    description="Update an existing monitoring policy override. Supports partial updates - only provided fields will be updated."
)
async def update_policy_override(
    override_code: str,
    request: UpdateMonitoringPolicyOverrideRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> UpdateMonitoringPolicyOverrideResponse:
    """
    Update an existing monitoring policy override.

    **Partial Updates Supported:**
    - Only fields provided in the request will be updated
    - All fields are optional in the update request
    - At least one field must be provided

    **Updatable Fields:**
    - name: Override display name
    - description: Override description
    - comparator: Comparison operator (gt, lt, eq, gte, lte)
    - threshold_value: Alert threshold value (0-10000)
    - threshold_unit: Unit of measurement
    - eval_window: Lookback window in minutes (> 0)
    - for_duration: Duration threshold must be breached in minutes (> 0)
    - no_data: Whether to alert on no data (boolean)
    - severity: Alert severity (P0-P4)
    - is_active: Enable/disable override (boolean)

    **Immutable Fields (Cannot Be Updated):**
    - code: Unique identifier
    - monitoring_policy_defaults_ref_code: Base policy reference
    - infrastructuretype_ref_code: Infrastructure type
    - alerttype_ref_code: Alert type
    - tenants_mst_code: Tenant scope
    - applications_mst_code: Application scope
    - resource_group_mst_code: Resource group scope

    **Rationale for Immutable Fields:**
    These fields define the override's scope and identity. Changing them would
    create a different override. To change scope, delete this override and create
    a new one with the desired scope.

    **Security:**
    - JWT authentication required
    - Tenant isolation enforced (override must belong to authenticated tenant)

    **Example Request:**
    ```json
    PUT /api/v1/monitoring-policies/update-policy-override/POL_OVR_ABC123
    {
      "name": "Updated CPU Policy",
      "threshold_value": 65.0,
      "eval_window": 10,
      "severity": "P2"
    }
    ```

    **Example Response:**
    ```json
    {
      "status": "success",
      "message": "Policy override 'Updated CPU Policy' updated successfully",
      "override": {
        "code": "POL_OVR_ABC123",
        "name": "Updated CPU Policy",
        ...
        "specificity_score": 7,
        "override_source": "Resource Group: rg-prod | Application: webapp"
      }
    }
    ```

    **Error Responses:**
    - 400 Bad Request: Validation errors, no fields provided, or override deleted
    - 404 Not Found: Override with specified code does not exist
    - 500 Internal Server Error: Database or unexpected errors
    """
    try:
        # Extract tenant from JWT
        user, tenant = user_and_tenant

        logger.info(f"Updating policy override: {override_code} for tenant: {tenant.code}")

        # Initialize service
        service = MonitoringPolicyService(db)

        # Call service method to update override (UPSERT: update or create)
        response = await service.update_policy_override(
            tenant_code=tenant.code,
            override_code=override_code,
            request=request
        )

        logger.info(f"Successfully updated policy override: {override_code}")

        return response

    except MonitoringPolicyOverrideValidationError as ve:
        logger.error(f"Validation error in update_policy_override: {ve.errors}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Validation failed",
                "errors": ve.errors
            }
        )
    except ValueError as ve:
        error_message = str(ve)
        logger.error(f"Value error in update_policy_override: {error_message}")

        # Determine if it's a 404 (not found) or 400 (bad request)
        if "not found" in error_message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_message
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_message
            )
    except Exception as e:
        logger.error(f"Error in update_policy_override: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update policy override: {str(e)}"
        )