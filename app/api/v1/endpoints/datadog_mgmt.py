from typing import Tuple
import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.datadog_mgmt_service import DatadogMgmtService
from app.schemas.datadog_schemas import (
    CreateDatadogAlertQuery,
    UpdateDatadogAlertQuery,
    GetAllDatadogAlertQueriesRequest,
    GetAllDatadogAlertQueriesResponse
)
from app.domain.validators.datadog_mgmt_rules import DatadogValidationError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/create-datadog-alert-query", summary="Create Datadog Alert Query Template")
async def create_datadog_alert_query(
    data: CreateDatadogAlertQuery,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new Datadog alert query template.

    Security:
        - JWT authentication required

    This endpoint creates a query template that will be used to generate
    Datadog monitors dynamically based on infrastructure type, alert type,
    and signal kind.

    **Request Body:**
    - `code`: Unique query code (e.g., 'ec2_cpu_metric')
    - `name`: Human-readable name (e.g., 'EC2 CPU Utilization Query')
    - `infrastructuretype_ref_code`: Infrastructure type code (e.g., 'ec2', 'rds')
    - `alerttype_ref_code`: Alert type code (e.g., 'cpu_util', 'memory_util')
    - `signal_kind`: Signal kind enum ('metric', 'log', 'trace', 'event')
    - `query_template`: Datadog query with placeholders (e.g., `avg(last_{{window}}m):avg:aws.ec2.cpuutilization{service:{{service_name}}} {{operator}} {{threshold}}`)
    - `description`: Optional description
    - `is_active`: Whether template is active (default: true)

    **Placeholders supported in query_template:**
    - `{{window}}`: Evaluation window in minutes
    - `{{service_name}}`: Service name/tag
    - `{{operator}}`: Comparison operator (>, <, >=, <=, ==)
    - `{{threshold}}`: Alert threshold value

    **Response:**
    ```json
    {
        "status": "success",
        "message": "Datadog alert query template created successfully",
        "query": {
            "id": 1,
            "code": "ec2_cpu_metric",
            "name": "EC2 CPU Utilization Query",
            ...
        }
    }
    ```

    **Raises:**
    - `400`: Code already exists or invalid combination
    - `404`: Infrastructure type or alert type not found
    - `422`: Validation error
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "CREATE DATADOG ALERT QUERY - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "query_code": data.code,
                "infrastructuretype_ref_code": data.infrastructuretype_ref_code,
                "alerttype_ref_code": data.alerttype_ref_code,
                "endpoint": "/create-datadog-alert-query"
            }
        )
        logger.debug(
            f"Query details: name={data.name}, signal_kind={data.signal_kind}, is_active={data.is_active}"
        )

        service = DatadogMgmtService(db)
        result = await service.create_datadog_alert_query(data)

        logger.info(f"Successfully created Datadog alert query '{data.code}'")
        return result
    except HTTPException:
        raise
    except DatadogValidationError as e:
        logger.warning(f"Validation error in create_datadog_alert_query: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # Check if it's a "not found" error
        if "not found" in str(e).lower():
            logger.warning(f"Resource not found in create_datadog_alert_query: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        logger.error(f"Value error in create_datadog_alert_query: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in create_datadog_alert_query: {str(e)}",
            exc_info=True,
            extra={"query_code": data.code if data else None}
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/get-all-datadog-alert-queries",
    response_model=GetAllDatadogAlertQueriesResponse,
    summary="Get All Datadog Alert Query Templates"
)
async def get_all_datadog_alert_queries(
    data: GetAllDatadogAlertQueriesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> GetAllDatadogAlertQueriesResponse:
    """
    Get all Datadog alert query templates with optional filtering and pagination.

    Security:
        - JWT authentication required

    This endpoint retrieves all query templates used to generate Datadog monitors.
    Supports filtering by infrastructure type, alert type, signal kind, and active status.

    **Request Body:**
    - `infrastructuretype_ref_code`: Filter by infrastructure type (e.g., 'ec2', 'rds', 'lambda')
    - `alerttype_ref_code`: Filter by alert type (e.g., 'cpu_util', 'http_4xx_rate')
    - `signal_kind`: Filter by signal kind ('metric', 'log', 'trace', 'event')
    - `is_active`: Filter by active status (true/false)
    - `skip`: Pagination offset (default: 0)
    - `limit`: Page size (default: 100, max: 500)

    **Response:**
    ```json
    {
        "total": 25,
        "skip": 0,
        "limit": 100,
        "queries": [
            {
                "id": 1,
                "code": "ec2_cpu_metric",
                "name": "EC2 CPU Utilization Query",
                "infrastructuretype_ref_code": "ec2",
                "alerttype_ref_code": "cpu_util",
                "signal_kind": "metric",
                "query_template": "avg(last_{{window}}m):avg:aws.ec2.cpuutilization{service:{{service_name}}} {{operator}} {{threshold}}",
                "is_active": true,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": null
            }
        ]
    }
    ```

    **Examples:**
    ```
    # Get all queries
    POST /api/v1/datadog-mgmt/get-all-datadog-alert-queries
    Body: {}

    # Filter by EC2
    POST /api/v1/datadog-mgmt/get-all-datadog-alert-queries
    Body: {"infrastructuretype_ref_code": "ec2"}

    # Get only metrics
    POST /api/v1/datadog-mgmt/get-all-datadog-alert-queries
    Body: {"signal_kind": "metric"}

    # With pagination
    POST /api/v1/datadog-mgmt/get-all-datadog-alert-queries
    Body: {"skip": 0, "limit": 50}
    ```

    **Raises:**
    - `400`: Invalid signal_kind value
    - `422`: Validation error
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "GET ALL DATADOG ALERT QUERIES - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "infrastructuretype_filter": data.infrastructuretype_ref_code or 'All',
                "alerttype_filter": data.alerttype_ref_code or 'All',
                "endpoint": "/get-all-datadog-alert-queries"
            }
        )
        logger.debug(
            f"Query filters: signal_kind={data.signal_kind}, is_active={data.is_active}, skip={data.skip}, limit={data.limit}"
        )

        service = DatadogMgmtService(db)
        result = await service.get_all_datadog_alert_queries(
            infrastructuretype_ref_code=data.infrastructuretype_ref_code,
            alerttype_ref_code=data.alerttype_ref_code,
            signal_kind=data.signal_kind,
            is_active=data.is_active,
            skip=data.skip,
            limit=data.limit
        )

        logger.info(f"Successfully retrieved {result['total']} Datadog alert queries")
        return GetAllDatadogAlertQueriesResponse(**result)
    except HTTPException:
        raise
    except DatadogValidationError as e:
        logger.warning(f"Validation error in get_all_datadog_alert_queries: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in get_all_datadog_alert_queries: {str(e)}",
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update-datadog-alert-query/{query_code}", summary="Update Datadog Alert Query Template")
async def update_datadog_alert_query(
    query_code: str,
    data: UpdateDatadogAlertQuery,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Update an existing Datadog alert query template.

    Security:
        - JWT authentication required

    This endpoint allows partial updates to a query template. Only fields
    provided in the request body will be updated. Immutable fields like
    code, infrastructuretype_ref_code, alerttype_ref_code, and signal_kind
    cannot be updated.

    **Path Parameters:**
    - `query_code`: Unique code of the query to update (e.g., 'ec2_cpu_metric')

    **Request Body (all optional):**
    - `name`: Updated human-readable name
    - `description`: Updated description (or null to clear)
    - `query_template`: Updated Datadog query with placeholders
    - `is_active`: Updated active status (true/false)

    **Response:**
    ```json
    {
        "status": "success",
        "message": "Datadog alert query template updated successfully",
        "query": {
            "id": 1,
            "code": "ec2_cpu_metric",
            "name": "Updated EC2 CPU Utilization Query",
            "description": "Updated description",
            "infrastructuretype_ref_code": "ec2",
            "alerttype_ref_code": "cpu_util",
            "signal_kind": "metric",
            "query_template": "avg(last_{{window}}m):avg:aws.ec2.cpuutilization{service:{{service_name}}} {{operator}} {{threshold}}",
            "is_active": false,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T10:30:00Z"
        }
    }
    ```

    **Examples:**
    ```
    # Update only the name
    PUT /api/v1/datadog-mgmt/update-datadog-alert-query/ec2_cpu_metric
    Body: {"name": "New Name"}

    # Deactivate a query
    PUT /api/v1/datadog-mgmt/update-datadog-alert-query/ec2_cpu_metric
    Body: {"is_active": false}

    # Update multiple fields
    PUT /api/v1/datadog-mgmt/update-datadog-alert-query/ec2_cpu_metric
    Body: {
        "name": "New Name",
        "description": "New description",
        "is_active": false
    }
    ```

    **Raises:**
    - `400`: No fields provided for update
    - `404`: Query with given code not found or has been deleted
    - `422`: Validation error (empty strings not allowed)
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log authenticated access
        logger.info(
            "UPDATE DATADOG ALERT QUERY - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "query_code": query_code,
                "endpoint": "/update-datadog-alert-query"
            }
        )
        logger.debug(f"Update data: {data.model_dump(exclude_unset=True)}")

        service = DatadogMgmtService(db)
        result = await service.update_datadog_alert_query(query_code, data)

        logger.info(f"Successfully updated Datadog alert query '{query_code}'")
        return result
    except HTTPException:
        raise
    except DatadogValidationError as e:
        logger.warning(f"Validation error in update_datadog_alert_query: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # Check if it's a "not found" error
        if "not found" in str(e).lower():
            logger.warning(f"Resource not found in update_datadog_alert_query: {str(e)}")
            raise HTTPException(status_code=404, detail=str(e))
        logger.error(f"Value error in update_datadog_alert_query: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            f"Unexpected error in update_datadog_alert_query: {str(e)}",
            exc_info=True,
            extra={"query_code": query_code}
        )
        raise HTTPException(status_code=500, detail=str(e))
