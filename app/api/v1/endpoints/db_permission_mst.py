"""
Database Permission API Endpoints

Endpoints for fetching and managing users and their permission hierarchy on a database server.
"""
from typing import Tuple, List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.db_permission_service import DbPermissionService

router = APIRouter()


# ── Pydantic schemas ─────────────────────────────────────────────────────────

class PermissionGrant(BaseModel):
    permission_id: Optional[int] = None  # existing db_permission_mst.id → update
    db_object_mst_id: Optional[int] = None  # for new grants only
    permissions: List[str] = []


class UpsertUserPermissionsRequest(BaseModel):
    infrastructure_mst_code: str
    username: str
    password: Optional[str] = None
    environment: str
    grants: List[PermissionGrant] = []


class UpsertUserPermissionsResponse(BaseModel):
    username: str
    infrastructure_mst_code: str
    created: int
    updated: int
    total_grants: int


@router.get("/users/search", summary="Search Usernames from Sibling Servers")
async def search_usernames(
    infrastructure_mst_code: str = Query(..., description="Current server to exclude from results"),
    applications_mst_code: str = Query(..., description="Application code to scope the search"),
    username: str = Query("", description="Username prefix to filter by"),
    environment: Optional[str] = Query(None, description="Optional environment filter"),
    geo_loc_mst_code: Optional[str] = Query(None, description="Optional geo location filter"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Search for usernames across sibling servers under the same application,
    excluding the current server. Useful for reusing credentials from another server.

    Returns a list of `{ username, password, server_name, infrastructure_mst_code, db_type }`.
    """
    try:
        user, tenant = user_and_tenant

        # Workspace access guard
        from app.services.workspace_service import WorkspaceService
        workspace_svc = WorkspaceService(db)
        if not await workspace_svc.verify_app_workspace_access(user.code, tenant.code, applications_mst_code):
            return {"users": []}

        service = DbPermissionService(db)
        results = await service.search_usernames(
            tenant_code=tenant.code,
            applications_mst_code=applications_mst_code,
            exclude_infrastructure_mst_code=infrastructure_mst_code,
            username_prefix=username,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )
        return {"users": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users", summary="List Users and Permissions for a Database Server")
async def list_users(
    infrastructure_mst_code: str = Query(..., description="Infrastructure code of the database server"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all users on a database server with their grouped permission hierarchy.

    **PostgreSQL** (nested: user → database → schema → table):
    ```json
    {
      "db_type": "postgresql",
      "users": [
        {
          "username": "postgres",
          "is_server_admin": true,
          "databases": [
            {
              "serverName": "INFRA_K8S_PG_...",
              "databaseName": "devlift_db",
              "permissions": ["ALL PRIVILEGES"],
              "schemas": []
            }
          ]
        }
      ]
    }
    ```

    **MySQL** (flat: user → database with table privileges):
    ```json
    {
      "db_type": "mysql",
      "users": [
        {
          "username": "app_user",
          "databases": [
            {
              "serverName": "INFRA_K8S_MYSQL_...",
              "databaseName": "app_db",
              "privileges": ["SELECT", "INSERT", "UPDATE"]
            }
          ]
        }
      ]
    }
    ```
    """
    try:
        user, tenant = user_and_tenant
        service = DbPermissionService(db)
        result = await service.list_users(
            infrastructure_mst_code=infrastructure_mst_code,
            tenant_code=tenant.code,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/db-user/upsert",
    summary="Create or Update User Permissions",
    response_model=UpsertUserPermissionsResponse,
)
async def upsert_user_permissions(
    request: UpsertUserPermissionsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create or update permission rows for a user on a database server.

    For each grant in the request, checks if a permission row already exists
    for the (infrastructure_mst_code, db_object_mst_id, username) composite key.
    If it exists, the permissions array is replaced. If not, a new row is created.
    """
    try:
        _, tenant = user_and_tenant
        service = DbPermissionService(db)
        result = await service.upsert_user_permissions(
            infrastructure_mst_code=request.infrastructure_mst_code,
            username=request.username,
            grants=[g.model_dump() for g in request.grants],
            tenant_code=tenant.code,
            environment=request.environment,
            password=request.password,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
