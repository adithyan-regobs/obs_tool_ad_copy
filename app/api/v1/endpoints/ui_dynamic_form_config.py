"""
UI Dynamic Form Config Endpoint

Endpoint for providing UI form configuration data like database lists.
Also handles fetching database users from GitHub terragrunt files.
"""

from typing import Tuple, Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.services.ui_dynamic_form_config_service import UIDynamicFormConfigService
from app.schemas.db_users_schemas import (
    GetDatabaseUsersRequest,
    GetDatabaseUsersResponse,
    GetServersListRequest,
    GetServersListResponse,
    GetDatabaseListFromTerragruntRequest,
    GetDatabaseListFromTerragruntResponse
)
from app.api.dependencies import get_current_user_and_tenant, get_db
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.domain.validators.github_rules import GitHubValidationError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/databases")
async def get_database_list(
    type: Optional[str] = Query(
        None,
        description="Filter by database type: mysql_database or postgresql_database"
    ),
    product: Optional[str] = Query(
        None,
        description="Application code to filter databases (e.g., APP001)"
    ),
    session: AsyncSession = Depends(get_db)
):
    """
    Get list of MySQL and PostgreSQL databases with their tables for UI form configuration.

    Optional Query Params:
        type = mysql_database | postgresql_database
        product = application code (e.g., APP001) - will be resolved to application name

    Example:
        /databases?type=mysql_database&product=APP001
        /databases?type=postgresql_database&product=APP002
    """
    try:
        # Resolve application code to application name
        # product_name = None
        # if product:        
        #     stmt = select(ApplicationsMstModel).where(
        #         ApplicationsMstModel.code == product,
        #         ApplicationsMstModel.is_deleted == False
        #     )
        #     result = await session.execute(stmt)
        #     application = result.scalar_one_or_none()
        #     if application:
        #         product_name = application.name
        #         logger.info(f"Resolved application code '{product}' to name '{product_name}'")
        #     else:
        #         logger.warning(f"Application code '{product}' not found, returning empty result")

        service = UIDynamicFormConfigService(session)
        result = await service.get_database_list(type_filter=type, product=product)
        return result

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.error(f"Error fetching database list: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch database configuration")


@router.post(
    "/database-users",
    summary="Get Database Users from Terragrunt Files",
    response_model=GetDatabaseUsersResponse,
    response_model_exclude_none=True
)
async def get_database_users(
    data: GetDatabaseUsersRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    session: AsyncSession = Depends(get_db)
) -> GetDatabaseUsersResponse:
    """
    Fetch all database users from terragrunt.hcl files in the GitHub repository.

    **Endpoint:** `POST /api/v1/ui-dynamic-form-config/database-users`

    **Security:**
        - JWT authentication required

    This endpoint performs the following operations:
    1. Builds path: environment/{product}-{env}-{version}/{region}/database/
    2. Lists all subdirectories in the database folder
    3. Fetches terragrunt.hcl from each subdirectory
    4. Parses mysql_users or psql_users arrays from each file
    5. Returns aggregated user information

    **Request Body:**
    - product_name: Product name (e.g., 'genorim') (required)
    - environment: Environment name - dev, staging, prod (required)
    - github_repository: Repository in 'owner/repo' format (required)
    - branch_name: Target branch name (required)
    - region: AWS region (optional, auto-detected from environment if not provided)
    - tenant: Tenant identifier (optional, for tenant-specific path logic e.g., 'vance', 'aspora')
    - geo_loc: Geographic location code (optional, for region mapping e.g., 'mumbai', 'london')

    **Returns:**
    - success: Boolean indicating success
    - users: Simple list of usernames with password and database type
    - full_details: Full user details with passwords and grants
    - metadata: directories_scanned, files_parsed, base_path, etc.
    - message: Result message

    **Example Request:**
    ```json
    {
      "product_name": "genorim",
      "environment": "dev",
      "github_repository": "myorg/infrastructure",
      "branch_name": "main"
    }
    ```

    **Example Response:**
    ```json
    {
      "success": true,
      "users": [
        {"username": "appuser", "password": "SecurePass123!", "database_type": "mysql"},
        {"username": "pguser", "password": "PgSecurePass!", "database_type": "postgresql"}
      ],
      "full_details": [
        {
          "username": "appuser",
          "password": "SecurePass123!",
          "database_type": "mysql",
          "source_directory": "common-mysql",
          "grants": [
            {
              "database": "mydb",
              "table": "*",
              "privileges": ["SELECT", "INSERT", "UPDATE"]
            }
          ]
        },
        {
          "username": "pguser",
          "password": "PgSecurePass!",
          "database_type": "postgresql",
          "source_directory": "common-pg",
          "grants": [
            {
              "database": "analytics",
              "schema": "public",
              "object_type": "table",
              "privileges": ["SELECT", "INSERT"]
            }
          ]
        }
      ],
      "metadata": {
        "directories_scanned": 2, 
        "files_parsed": 2,
        "base_path": "environment/genorim-dev-01/ap-south-1/database",
        "product": "genorim",
        "environment": "dev",
        "region": "ap-south-1"
      },
      "message": "Found 2 database user(s) across 2 file(s)"
    }
    ```

    **Grant Structure:**

    MySQL grants include:
    - database: Database name
    - table: Table name (use "*" for all tables)
    - privileges: Array of privileges (SELECT, INSERT, UPDATE, DELETE, etc.)

    PostgreSQL grants include:
    - database: Database name
    - schema: Schema name (e.g., "public")
    - object_type: Object type (database, schema, table)
    - privileges: Array of privileges (SELECT, INSERT, UPDATE, DELETE, CREATE, USAGE, etc.)
    """
    try:
        # Extract authenticated user from JWT (tenant not used for path, but JWT still required)
        user, _ = user_and_tenant

        logger.info("=" * 80)
        logger.info("GET DATABASE USERS")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   Product: {data.product_name}")
        logger.info(f"   Environment: {data.environment}")
        logger.info(f"   GitHub Repo: {data.github_repository}")
        logger.info(f"   Region: {data.region or 'auto-detect'}")
        logger.info(f"   Tenant: {data.tenant or 'default'}")
        logger.info(f"   Geo Loc: {data.geo_loc or 'not specified'}")
        logger.info("=" * 80)

        # Initialize service
        service = UIDynamicFormConfigService(session)

        # Call service with product_name and tenant/geo_loc for path logic
        result = await service.get_database_users(
            product_name=data.product_name,
            environment=data.environment,
            github_repository=data.github_repository,
            branch_name=data.branch_name,
            region=data.region,
            tenant=data.tenant,
            geo_loc=data.geo_loc
        )

        return GetDatabaseUsersResponse(**result)

    except GitHubValidationError as e:
        logger.error(f"Validation error in get_database_users: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        error_message = str(e)

        if "authentication failed" in error_message.lower() or "invalid or expired token" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            logger.error(f"Unexpected error in get_database_users: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch database users: {error_message}")


@router.post(
    "/servers-list",
    summary="Get Database Server Directories",
    response_model=GetServersListResponse,
    response_model_exclude_none=True
)
async def get_servers_list(
    data: GetServersListRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    session: AsyncSession = Depends(get_db)
) -> GetServersListResponse:
    """
    Get list of database server directories from the GitHub repository.

    **Endpoint:** `POST /api/v1/ui-dynamic-form-config/servers-list`

    **Security:**
        - JWT authentication required

    This endpoint lists all subdirectories in the database folder path.

    **Request Body:**
    - product_name: Product name (e.g., 'core') (required)
    - environment: Environment name - dev, staging, prod (required)
    - github_repository: Repository in 'owner/repo' format (required)
    - branch_name: Target branch name (required)
    - region: AWS region (optional, auto-detected from environment if not provided)
    - tenant: Tenant identifier (optional)
    - geo_loc: Geographic location code (optional)

    **Example Response:**
    ```json
    {
      "success": true,
      "servers": ["common-mysql", "common-pg"],
      "metadata": {
        "base_path": "environment/core-stage-01/ap-south-1/database",
        "server_count": 2
      },
      "message": "Found 2 database server(s)"
    }
    ```
    """
    try:
        user, _ = user_and_tenant

        logger.info("=" * 80)
        logger.info("GET SERVERS LIST")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   Product: {data.product_name}")
        logger.info(f"   Environment: {data.environment}")
        logger.info(f"   GitHub Repo: {data.github_repository}")
        logger.info(f"   Region: {data.region or 'auto-detect'}")
        logger.info("=" * 80)

        service = UIDynamicFormConfigService(session)

        result = await service.get_servers_list(
            product_name=data.product_name,
            environment=data.environment,
            github_repository=data.github_repository,
            branch_name=data.branch_name,
            region=data.region,
            tenant=data.tenant,
            geo_loc=data.geo_loc
        )

        return GetServersListResponse(**result)

    except GitHubValidationError as e:
        logger.error(f"Validation error in get_servers_list: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        error_message = str(e)

        if "authentication failed" in error_message.lower() or "invalid or expired token" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            logger.error(f"Unexpected error in get_servers_list: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch servers list: {error_message}")


@router.post(
    "/database-list-terragrunt",
    summary="Get Database List from Terragrunt",
    response_model=GetDatabaseListFromTerragruntResponse,
    response_model_exclude_none=True
)
async def get_database_list_from_terragrunt(
    data: GetDatabaseListFromTerragruntRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    session: AsyncSession = Depends(get_db)
) -> GetDatabaseListFromTerragruntResponse:
    """
    Get list of databases from terragrunt.hcl for a specific server.

    **Endpoint:** `POST /api/v1/ui-dynamic-form-config/database-list-terragrunt`

    **Security:**
        - JWT authentication required

    This endpoint fetches and parses the terragrunt.hcl file from the specified
    server subdirectory to extract the list of databases.

    **Request Body:**
    - product_name: Product name (e.g., 'core') (required)
    - environment: Environment name - dev, staging, prod (required)
    - github_repository: Repository in 'owner/repo' format (required)
    - branch_name: Target branch name (required)
    - server_name: Server subdirectory name (e.g., 'common-mysql') (required)
    - region: AWS region (optional, auto-detected from environment if not provided)
    - tenant: Tenant identifier (optional)
    - geo_loc: Geographic location code (optional)

    **Example Response:**
    ```json
    {
      "success": true,
      "databases": ["ybl_fulfillment", "appserver_noref", "accounts"],
      "database_type": "mysql",
      "server_name": "common-mysql",
      "metadata": {
        "file_path": "environment/core-stage-01/ap-south-1/database/common-mysql/terragrunt.hcl"
      },
      "message": "Found 3 database(s) in common-mysql"
    }
    ```
    """
    try:
        user, _ = user_and_tenant

        logger.info("=" * 80)
        logger.info("GET DATABASE LIST FROM TERRAGRUNT")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   Product: {data.product_name}")
        logger.info(f"   Environment: {data.environment}")
        logger.info(f"   Server Name: {data.server_name}")
        logger.info(f"   GitHub Repo: {data.github_repository}")
        logger.info(f"   Region: {data.region or 'auto-detect'}")
        logger.info("=" * 80)

        service = UIDynamicFormConfigService(session)

        result = await service.get_database_list_from_terragrunt(
            product_name=data.product_name,
            environment=data.environment,
            github_repository=data.github_repository,
            branch_name=data.branch_name,
            server_name=data.server_name,
            region=data.region,
            tenant=data.tenant,
            geo_loc=data.geo_loc
        )

        return GetDatabaseListFromTerragruntResponse(**result)

    except GitHubValidationError as e:
        logger.error(f"Validation error in get_database_list_from_terragrunt: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        error_message = str(e)

        if "authentication failed" in error_message.lower() or "invalid or expired token" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            logger.error(f"Unexpected error in get_database_list_from_terragrunt: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch database list: {error_message}")
