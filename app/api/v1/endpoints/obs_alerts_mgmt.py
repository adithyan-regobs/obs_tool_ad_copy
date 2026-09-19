from typing import Tuple
import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.alert_schemas import (
    CreateAlert,
    EnableAlertsForService,
    EnableAlertsResponse,
    BulkCreateOrUpdateAlerts,
    GetAlertsForService,
    CreateOrUpdateAlertResponse,
    BulkCreateOrUpdateAlertsResponse
)
from app.services.obs_alerts_service import ObsCreateAlertsService

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/defaultroute")
def say_hello():
    return{"message":"hello"}

#Endpoint Legacy, for testing single alert create
@router.post("/create-alert", summary="Create Alerts")
async def create(
    data: CreateAlert,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "CREATE ALERT - Authenticated access (Legacy endpoint)",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "service_code": data.services_code,
                "endpoint": "/create-alert"
            }
        )
        logger.debug(
            f"Alert details: services_code={data.services_code}, monitoring_policy={data.monitoring_policy_code}, severity={data.severity}"
        )

        # Initialize service with database session
        service = ObsCreateAlertsService(db)
        # Call the create_alert method with the data
        result = await service.create_alert(data)

        logger.info(f"Successfully created alert for service {data.services_code}")
        return result
    except ValueError as e:
        # Handle not found and validation errors
        error_msg = str(e).lower()
        if "not found" in error_msg:
            logger.warning(f"Resource not found in create_alert: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        else:
            logger.warning(f"Validation error in create_alert: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in create_alert: {str(e)}",
            exc_info=True,
            extra={"service_code": data.services_code if data else None}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/create-or-update-alert",
    summary="Create or Update Alert (Upsert)",
    response_model=CreateOrUpdateAlertResponse,
    response_model_exclude_none=True
)
async def create_or_update(
    data: CreateAlert,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> CreateOrUpdateAlertResponse:
    """
    Create or update an alert configuration (upsert operation).

    Security:
        - JWT authentication required

    Identifies existing alerts by services_code + monitoring_policy_code.
    - If exists: Updates the alert configuration and syncs with vendor
    - If not exists: Creates a new alert configuration

    **Request:**
    - All fields from CreateAlert schema

    **Response:**
    - `status`: "success" or "partial" (if vendor sync pending/failed)
    - `message`: Human-readable operation description
      - Success: "Alert created/updated successfully"
      - Timeout: "Alert created/updated, but vendor sync timed out (Datadog may be down)"
      - Failed: "Alert created/updated, but vendor sync failed (check Datadog connectivity)"
    - `operation`: "created" or "updated"
    - `alert_code`: Unique alert code

    **Example Success Response:**
    ```json
    {
        "status": "success",
        "message": "Alert created successfully",
        "operation": "created",
        "alert_code": "AC_ABC12345"
    }
    ```

    **Example Partial Response (Datadog Down):**
    ```json
    {
        "status": "partial",
        "message": "Alert created, but vendor sync timed out (Datadog may be down)",
        "operation": "created",
        "alert_code": "AC_ABC12345"
    }
    ```
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "CREATE OR UPDATE ALERT - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "service_code": data.services_code,
                "endpoint": "/create-or-update-alert"
            }
        )
        logger.debug(
            f"Alert details: services_code={data.services_code}, monitoring_policy={data.monitoring_policy_code}, severity={data.severity}"
        )

        service = ObsCreateAlertsService(db)
        result = await service.create_or_update_alert(data, user_code=user.code, tenant_code=tenant.code)

        logger.info(
            f"Successfully {result['operation']} alert for service {data.services_code}",
            extra={"alert_code": result.get("alert_code"), "status": result.get("status")}
        )
        return result
    except ValueError as e:
        # Handle not found and validation errors
        error_msg = str(e).lower()
        if "not found" in error_msg:
            logger.warning(f"Resource not found in create_or_update_alert: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        else:
            logger.warning(f"Validation error in create_or_update_alert: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in create_or_update_alert: {str(e)}",
            exc_info=True,
            extra={"service_code": data.services_code if data else None}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/bulk-create-or-update-alerts",
    summary="Bulk Create or Update Alerts",
    response_model=BulkCreateOrUpdateAlertsResponse,
    response_model_exclude_none=True
)
async def bulk_create_or_update(
    data: BulkCreateOrUpdateAlerts,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> BulkCreateOrUpdateAlertsResponse:
    """
    Bulk create or update alerts (upsert operation).

    Security:
        - JWT authentication required

    Processes each alert independently. Continues on individual failures.
    For each alert, identifies by services_code + monitoring_policy_code:
    - If exists: Updates the alert
    - If not exists: Creates new alert

    **Request:**
    - `alerts`: List of CreateAlert objects (max 100 per request)

    **Response:**
    - `status`: "success" (all succeeded) or "partial" (some failed)
    - `message`: Human-readable summary of the operation
    - `alerts_created`: Number of alerts successfully created
    - `alerts_updated`: Number of alerts successfully updated
    - `alerts_failed`: Number of alerts that failed

    **Example Response:**
    ```json
    {
        "status": "success",
        "message": "Successfully processed 10 alert(s): 5 created, 5 updated",
        "alerts_created": 5,
        "alerts_updated": 5,
        "alerts_failed": 0
    }
    ```
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "BULK CREATE OR UPDATE ALERTS - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "alert_count": len(data.alerts),
                "endpoint": "/bulk-create-or-update-alerts"
            }
        )
        logger.debug(f"Processing {len(data.alerts)} alerts for bulk operation")

        service = ObsCreateAlertsService(db)
        result = await service.bulk_create_or_update_alerts(data.alerts, user_code=user.code, tenant_code=tenant.code)

        logger.info(
            f"Bulk operation completed: {result['alerts_created']} created, {result['alerts_updated']} updated, {result['alerts_failed']} failed",
            extra={
                "alerts_created_count": result['alerts_created'],
                "alerts_updated_count": result['alerts_updated'],
                "alerts_failed_count": result['alerts_failed'],
                "status": result['status']
            }
        )
        return result
    except ValueError as e:
        # Handle not found and validation errors
        error_msg = str(e).lower()
        if "not found" in error_msg:
            logger.warning(f"Resource not found in bulk_create_or_update_alerts: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        else:
            logger.warning(f"Validation error in bulk_create_or_update_alerts: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in bulk_create_or_update_alerts: {str(e)}",
            exc_info=True,
            extra={"alert_count": len(data.alerts) if data else 0}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/get-alerts-for-service", summary="Get All Alerts for Service")
async def get_alerts_for_service(
    data: GetAlertsForService,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all alerts (configured and available) for a service.

    Security:
        - JWT authentication required

    Returns comprehensive list combining:
    - Configured alerts from alert_configs (with vendor status)
    - Available alerts from monitoring_policy_defaults_ref (with override resolution)

    Request:
        - services_code: Service code to get alerts for
        - infrastructuretype_ref_code: Infrastructure type to filter alerts by

    Response:
        - service_code: Service code
        - service_name: Service name
        - total_alerts: Total number of alerts (configured + available)
        - configured_count: Number of already configured alerts
        - available_count: Number of unconfigured but available alerts
        - alerts: List of AlertInfo objects with:
            - is_configured: Whether alert is already configured
            - alert_id, alert_code: If configured
            - vendor_status, vendor_monitor_id: Vendor sync status
            - monitoring_policy_code, infrastructuretype_ref_code, alerttype_ref_code
            - comparator, threshold_value, threshold_unit, eval_window, for_duration, no_data, severity
            - policy_source: "configured", "Override: X", or "Default Policy"
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "GET ALERTS FOR SERVICE - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "service_code": data.services_code,
                "infrastructuretype_ref_code": data.infrastructuretype_ref_code,
                "endpoint": "/get-alerts-for-service"
            }
        )

        service = ObsCreateAlertsService(db)
        result = await service.get_alerts_for_service(
            services_code=data.services_code,
            infrastructuretype_ref_code=data.infrastructuretype_ref_code
        )

        logger.info(
            f"Retrieved {result['total_alerts']} alerts for service {data.services_code} ({result['configured_count']} configured, {result['available_count']} available)"
        )
        return result
    except ValueError as e:
        # Handle not found and validation errors
        error_msg = str(e).lower()
        if "not found" in error_msg:
            logger.warning(f"Resource not found in get_alerts_for_service: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        else:
            logger.warning(f"Validation error in get_alerts_for_service: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_alerts_for_service: {str(e)}",
            exc_info=True,
            extra={"service_code": data.services_code if data else None}
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post(
    "/enable-alerts-for-service",
    summary="Enable All Alerts for Service",
    response_model=EnableAlertsResponse,
    response_model_exclude_none=True
)
async def enable_alerts_for_service(
    data: EnableAlertsForService,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> EnableAlertsResponse:
    """
    Enable all applicable alerts for a service based on its infrastructure type.

    Security:
        - JWT authentication required

    This endpoint automatically creates all relevant monitoring alerts based on the
    service's infrastructure type (defined in services_mst.infrastructuretype_ref_code).

    **Process:**
    1. Retrieves service details (including infrastructure type)
    2. Fetches monitoring policies for that infrastructure type (e.g., EC2, RDS, Lambda)
    3. Gets already configured alerts
    4. Resolves policy overrides (8-level hierarchy: RG+Tenant+App, RG+Tenant, etc.)
    5. Creates alerts that don't already exist
    6. Syncs new alerts with vendor platform (e.g., Datadog)

    **Idempotent:** Running this multiple times is safe. Existing alerts are skipped.

    **Request Body:**
    - `services_code`: Service code to enable alerts for (e.g., "SVC001")

    **Response:**
    - `status`: "success" (all created) or "partial" (some failed)
    - `message`: Human-readable summary message
    - `alerts_created`: Number of alerts successfully created
    - `alerts_failed`: Number of alerts that failed to create

    **Example Response:**
    ```json
    {
        "status": "success",
        "message": "Successfully created 7 alert(s)",
        "alerts_created": 7,
        "alerts_failed": 0
    }
    ```

    **Raises:**
    - `404`: Service not found
    - `400`: Validation errors
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "ENABLE ALERTS FOR SERVICE - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "service_code": data.services_code,
                "endpoint": "/enable-alerts-for-service"
            }
        )

        service = ObsCreateAlertsService(db)
        result = await service.enable_alerts_for_service(data.services_code)

        logger.info(
            f"Enabled alerts for service {data.services_code}: {result['alerts_created']} created, {result['alerts_failed']} failed",
            extra={
                "created": result['alerts_created'],
                "failed": result['alerts_failed'],
                "status": result['status']
            }
        )
        return result
    except ValueError as e:
        # Handle not found and validation errors
        error_msg = str(e).lower()
        if "not found" in error_msg:
            logger.warning(f"Resource not found in enable_alerts_for_service: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        else:
            logger.warning(f"Validation error in enable_alerts_for_service: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in enable_alerts_for_service: {str(e)}",
            exc_info=True,
            extra={"service_code": data.services_code if data else None}
        )
        raise HTTPException(status_code=500, detail=str(e))
