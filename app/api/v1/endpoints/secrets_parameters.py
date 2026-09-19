"""
Secrets and Parameters Management API Endpoints

Generic REST API endpoints for managing secrets and parameters across cloud vendors.
Works with AWS, GCP, Azure - vendor determined from service configuration.

Endpoints:
    GET  /service/{service_code} - List all secrets/parameters for a service
    POST /secret/link-existing - Link existing secret from vendor
    POST /secret/create - Create new secret in vendor
    GET  /secret/{secret_id}/values - Get secret key-value pairs
    PUT  /secret/{secret_id}/update - Update secret values
    POST /parameter/link-existing - Link existing parameter from vendor
    POST /parameter/create - Create new parameter in vendor
    GET  /parameter/{parameter_id}/value - Get parameter value
    PUT  /parameter/{parameter_id}/update - Update parameter value
"""

from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.secrets_mgmt_service import SecretsMgmtService
from app.schemas.secrets_parameters_schemas import (
    LinkExistingSecretRequest,
    CreateSecretRequest,
    UpdateSecretRequest,
    LinkExistingParameterRequest,
    CreateParameterRequest,
    UpdateParameterRequest,
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

router = APIRouter()


# ==================== SECRET ENDPOINTS ====================

@router.get(
    "/service/{service_code}",
    summary="Get all secrets and parameters for a service"
)
async def get_service_secrets_parameters(
    service_code: str,
    environment: str = Query("prod", description="Environment (dev, staging, prod)"),
    resource_type: str = Query(None, description="Filter by type (secret or parameter)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all configured secrets and parameters for a service.

    **Works with any vendor** - AWS, GCP, Azure automatically determined from service configuration.

    **Response includes:**
    - Service information (vendor, region, environment)
    - List of secrets with metadata (no values)
    - List of parameters with metadata (no values)
    - Configuration status

    **Use this endpoint to:**
    - Initial load when user navigates to secrets management page
    - Show "No secrets configured" message if empty
    - Display list of configured secrets/parameters

    **Status Codes:**
    - 200: Success
    - 404: Service not found
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.get_service_secrets_and_parameters(
            service_code=service_code,
            environment=environment
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/secret/link-existing",
    summary="Link existing secret from cloud vendor"
)
async def link_existing_secret(
    data: LinkExistingSecretRequest,  # now uses resource_code instead of service_code
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Link an existing secret from cloud vendor (does not create new secret).

    **Vendor-specific identifiers:**
    - **AWS**: Provide secret ARN (e.g., `arn:aws:secretsmanager:us-east-1:123456789012:secret:...`)
    - **GCP**: Provide resource name (e.g., `projects/my-project/secrets/my-secret`)
    - **Azure**: Provide Key Vault URI (e.g., `https://myvault.vault.azure.net/secrets/my-secret`)

    **Process:**
    1. Validates secret exists in cloud vendor
    2. Extracts metadata (name, version, etc.)
    3. Saves reference to database
    4. Returns metadata (no secret values for security)

    **After linking:**
    - User can click "View Values" to see actual secret data
    - Secret appears in service's secret list

    **Status Codes:**
    - 200: Successfully linked
    - 400: Invalid identifier format or vendor not supported
    - 404: Secret not found in cloud vendor or service not found
    - 409: Secret already linked to this service
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.link_existing_secret(
            resource_code=data.resource_code,  # was service_code
            secret_name=data.secret_name,
            environment=data.environment
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        elif "already linked" in error_msg or "already exists" in error_msg:
            raise HTTPException(status_code=409, detail=str(e))
        elif "not yet implemented" in error_msg or "not supported" in error_msg:
            raise HTTPException(status_code=400, detail=str(e))
        else:
            raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/secret/create",
    summary="Create new secret in cloud vendor"
)
async def create_new_secret(
    data: CreateSecretRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new secret in cloud vendor (AWS Secrets Manager, GCP Secret Manager, etc.).

    **Path construction:**
    - **With resource_name**: `{tenant}/{environment}/{region}/{resource_name}`
    - **Without resource_name**: `{tenant}/{environment}/{region}/{service_name}`

    **Secret value format:**
    - Key-value pairs: `{"api_key": "value1", "api_secret": "value2"}`
    - Single string: `"simple-string-value"`

    **Tags (optional):**
    - AWS format: `[{"Key": "Environment", "Value": "prod"}]`
    - Applied to secret in cloud vendor for organization

    **Process:**
    1. Validates service exists and vendor supported
    2. Builds resource path based on naming convention
    3. Creates secret in cloud vendor
    4. Saves reference to database
    5. Returns created secret metadata

    **Status Codes:**
    - 200: Successfully created
    - 400: Invalid input, vendor not supported, or secret scheduled for deletion
    - 404: Service not found or no cloud account configured
    - 409: Secret with this name already exists
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.create_new_secret(
            resource_code=data.resource_code,  # was service_code
            secret_name=data.secret_name,
            secret_value=data.secret_value,
            tags=data.tags,
            environment=data.environment
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "already exists" in error_msg:
            raise HTTPException(status_code=409, detail=str(e))
        elif "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        elif "not yet implemented" in error_msg or "not supported" in error_msg:
            raise HTTPException(status_code=400, detail=str(e))
        else:
            raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        error_msg = str(e).lower()
        if "scheduled for deletion" in error_msg:
            raise HTTPException(
                status_code=400,
                detail=f"{str(e)}\n\nTo resolve, restore and force-delete the secret in cloud vendor first."
            )
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/secret/{secret_id}/values",
    summary="Get secret values (key-value pairs)"
)
async def get_secret_values(
    secret_id: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get secret values as key-value pairs for display/edit.

    **Use this endpoint when:**
    - User clicks "View Values" button on a secret
    - Showing secret data in edit form

    **Security note:**
    - This endpoint returns actual secret values (decrypted)
    - Ensure proper authentication and authorization
    - Consider masking in UI initially

    **Response format:**
    - For JSON secrets: `{"values": {"key1": "value1", "key2": "value2"}}`
    - For string secrets: `{"values": "simple-string-value"}`

    **Status Codes:**
    - 200: Success
    - 404: Secret not found in database
    - 410: Secret deleted in cloud vendor (marked in DB)
    - 500: Internal server error or cloud vendor API error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.get_secret_values(secret_id)
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "deleted" in error_msg and "no longer accessible" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))  # 410 Gone
        elif "marked as deleted" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))
        else:
            raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/secret/{secret_id}/update",
    summary="Update secret values"
)
async def update_secret_values(
    secret_id: str,
    data: UpdateSecretRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Update secret values in cloud vendor.

    **Use this endpoint when:**
    - User edits secret values and clicks "Save"
    - Rotating credentials
    - Adding/removing keys from secret

    **Update behavior:**
    - Completely replaces secret value in cloud vendor
    - To remove a key: exclude it from the request
    - To add a key: include it in the request
    - Cloud vendor creates new version

    **Version tracking:**
    - AWS: Returns new VersionId
    - GCP: Returns new version number
    - Azure: Returns new version identifier

    **Status Codes:**
    - 200: Successfully updated
    - 400: Invalid input or secret scheduled for deletion in cloud
    - 404: Secret not found
    - 410: Secret deleted in cloud vendor
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.update_secret_values(
            secret_id=secret_id,
            secret_value=data.secret_value,
            description=None  # Description is not supported in update
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "deleted" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))
        else:
            raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        error_msg = str(e).lower()
        if "scheduled for deletion" in error_msg:
            raise HTTPException(
                status_code=400,
                detail=f"{str(e)}\n\nTo update, restore the secret in cloud vendor first."
            )
        raise HTTPException(status_code=500, detail=str(e))


# ==================== PARAMETER ENDPOINTS ====================

@router.post(
    "/parameter/link-existing",
    summary="Link existing parameter from cloud vendor"
)
async def link_existing_parameter(
    data: LinkExistingParameterRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Link an existing parameter/configuration from cloud vendor.

    **Vendor-specific parameter paths:**
    - **AWS SSM**: `/tenant/environment/region/name` (e.g., `/acme-corp/prod/us-east-1/max-connections`)
    - **GCP Runtime Config**: `projects/my-project/configs/my-config/variables/my-var`
    - **Azure App Config**: `https://mystore.azconfig.io/kv/my-key`

    **Process:**
    1. Validates parameter exists in cloud vendor
    2. Retrieves parameter metadata (type, version, etc.)
    3. Saves reference to database
    4. Returns metadata (parameter value NOT included for security)

    **After linking:**
    - User can click "View Value" to see actual parameter data
    - Parameter appears in service's parameter list

    **Status Codes:**
    - 200: Successfully linked
    - 400: Invalid parameter path or vendor not supported
    - 404: Parameter not found in cloud vendor
    - 409: Parameter already linked
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.link_existing_parameter(
            resource_code=data.resource_code,  # was service_code
            parameter_name=data.parameter_name,
            environment=data.environment
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        elif "already linked" in error_msg or "already exists" in error_msg:
            raise HTTPException(status_code=409, detail=str(e))
        elif "not yet implemented" in error_msg or "not supported" in error_msg:
            raise HTTPException(status_code=400, detail=str(e))
        else:
            raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/parameter/create",
    summary="Create new parameter in cloud vendor"
)
async def create_new_parameter(
    data: CreateParameterRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new parameter/configuration value in cloud vendor.

    **Parameter types:**
    - **AWS SSM**:
      - `String`: Plain text value
      - `StringList`: Comma-separated values
      - `SecureString`: Encrypted value (KMS)
    - **GCP**: String only
    - **Azure**: String only

    **Path construction:**
    - **AWS SSM**: `/{tenant}/{environment}/{region}/{name}` (leading `/` added automatically)
    - **GCP**: `projects/{project}/configs/{config}/variables/{name}`
    - **Azure**: Determined by App Configuration store

    **Use cases:**
    - Configuration values (timeout, retry count, feature flags)
    - Non-sensitive settings
    - Environment-specific config

    **Status Codes:**
    - 200: Successfully created
    - 400: Invalid parameter type or vendor not supported
    - 404: Service not found or no cloud account configured
    - 409: Parameter already exists
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.create_new_parameter(
            resource_code=data.resource_code,  # was service_code
            parameter_name=data.parameter_name,
            parameter_value=data.parameter_value,
            parameter_type=data.parameter_type,
            tags=data.tags,
            environment=data.environment
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "already exists" in error_msg:
            raise HTTPException(status_code=409, detail=str(e))
        elif "not found" in error_msg:
            raise HTTPException(status_code=404, detail=str(e))
        elif "not yet implemented" in error_msg or "not supported" in error_msg:
            raise HTTPException(status_code=400, detail=str(e))
        else:
            raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/parameter/{parameter_id}/value",
    summary="Get parameter value"
)
async def get_parameter_value(
    parameter_id: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get parameter value from cloud vendor.

    **Use this endpoint when:**
    - User clicks "View Value" on a parameter
    - Displaying parameter in edit form

    **Response includes:**
    - Parameter value (string)
    - Parameter type (String, StringList, SecureString for AWS)
    - Version number
    - Last modified timestamp

    **SecureString handling:**
    - AWS: Automatically decrypted if permissions allow
    - Returned as plain text in response

    **Status Codes:**
    - 200: Success
    - 404: Parameter not found
    - 410: Parameter deleted in cloud vendor
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.get_parameter_value(parameter_id)
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "deleted" in error_msg and "no longer accessible" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))
        elif "marked as deleted" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))
        else:
            raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/parameter/{parameter_id}/update",
    summary="Update parameter value"
)
async def update_parameter_value(
    parameter_id: str,
    data: UpdateParameterRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Update parameter value in cloud vendor.

    **Use this endpoint when:**
    - User edits parameter value and clicks "Save"
    - Updating configuration value
    - Changing feature flag state

    **Update behavior:**
    - Overwrites current parameter value in cloud vendor
    - Parameter type remains the same (cannot change String to SecureString)
    - Cloud vendor increments version number

    **Version tracking:**
    - AWS SSM: Returns incremented version
    - Each update creates new version
    - Old versions retained by cloud vendor

    **Status Codes:**
    - 200: Successfully updated
    - 400: Invalid value format
    - 404: Parameter not found
    - 410: Parameter deleted in cloud vendor
    - 500: Internal server error
    """
    try:
        service = SecretsMgmtService(db)
        result = await service.update_parameter_value(
            parameter_id=parameter_id,
            parameter_value=data.parameter_value,
            description=data.description
        )
        return result
    except ValueError as e:
        error_msg = str(e).lower()
        if "deleted" in error_msg:
            raise HTTPException(status_code=410, detail=str(e))
        else:
            raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
