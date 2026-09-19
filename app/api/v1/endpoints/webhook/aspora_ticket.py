"""
Aspora Webhook Endpoints

Authenticated via pre-shared API key in X-Api-Key header (programmatic/M2M auth).

Endpoints:
  GET  /webhooks/aspora-servers      — list DB server infrastructure_mst entries for tenant
  GET  /webhooks/aspora-permissions  — list existing users/permissions for a DB server
  POST /webhooks/aspora-ticket       — create db_object_mst entry + transaction queue item
  POST /webhooks/aspora-user-management  — upsert DB user permissions + transaction queue item
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.enum import WorkflowSourceTableEnum
from sqlalchemy import and_, select
from sqlalchemy.orm import joinedload

from app.core.enum import EnvironmentEnum
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.repository.db_object_mst_repository import DbObjectMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.services.db_object_service import DbObjectService
from app.services.db_permission_service import DbPermissionService
from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.services.ticket_service import TicketService
from app.services.transaction_queue_service import TransactionQueueService

router = APIRouter()
logger = logging.getLogger(__name__)

_CASE_REF_CODE = "database_creation"

# All aspora webhook calls are scoped to this tenant
_TENANT_CODE = "aspora"

# Hardcoded system user per tenant for M2M calls (no user JWT in this flow)
_TENANT_USER_CODE: dict[str, str] = {
    "aspora": "4ed6e7c8-1b0e-4e20-b3f0-0547b31ca6bb",
    "vance": "3868423c-3dab-40e6-810c-13fcce65a0b7",
}

# Infrastructure type codes that represent database servers
_DB_SERVER_TYPES = [
    "aurora_mysql_infrastructuretype_ref",
    "aurora_postgres_infrastructuretype_ref",
    "devlift_k8s_postgres_infrastructuretype_ref",
    "rds_postgres_infrastructuretype_ref",
]


def _verify_api_key(x_api_key: str = Header(None, alias="X-Api-Key")) -> None:
    if not x_api_key or x_api_key != settings.aspora_api_key:
        logger.warning("Aspora webhook: invalid or missing API key")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


# ── GET /aspora-servers ──────────────────────────────────────────────────────

class AsporaServerItem(BaseModel):
    code: str
    name: str
    infrastructuretype_ref_code: str
    environment: str
    geo_loc_mst_code: str
    product_name: Optional[str]


class AsporaServersResponse(BaseModel):
    servers: List[AsporaServerItem]
    total: int


@router.get(
    "/aspora-servers",
    response_model=AsporaServersResponse,
    summary="Aspora — List DB Servers",
    dependencies=[Depends(_verify_api_key)],
)
async def aspora_servers(
    environment: Optional[str] = Query(default=None, description="Filter by environment: dev | stage | qa | prod"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all active database server infrastructure_mst entries for the aspora tenant.
    Aspora uses this to let the user pick which server the new database goes on.
    """
    filters = [
        InfrastructureMstModel.tenants_mst_code == _TENANT_CODE,
        InfrastructureMstModel.is_deleted == False,
        InfrastructureMstModel.is_active == True,
        InfrastructureMstModel.infrastructuretype_ref_code.in_(_DB_SERVER_TYPES),
    ]
    if environment:
        try:
            filters.append(InfrastructureMstModel.environments_enum == EnvironmentEnum(environment))
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid environment: {environment}")

    stmt = (
        select(InfrastructureMstModel, ApplicationsMstModel.name.label("product_name"))
        .outerjoin(ApplicationsMstModel, InfrastructureMstModel.applications_mst_code == ApplicationsMstModel.code)
        .where(and_(*filters))
    )
    result = await db.execute(stmt)
    rows = result.all()

    servers = [
        AsporaServerItem(
            code=row.InfrastructureMstModel.code,
            name=row.InfrastructureMstModel.name,
            infrastructuretype_ref_code=row.InfrastructureMstModel.infrastructuretype_ref_code,
            environment=row.InfrastructureMstModel.environments_enum.value if hasattr(row.InfrastructureMstModel.environments_enum, "value") else str(row.InfrastructureMstModel.environments_enum),
            geo_loc_mst_code=row.InfrastructureMstModel.geo_loc_mst_code or "",
            product_name=row.product_name,
        )
        for row in rows
    ]
    return AsporaServersResponse(servers=servers, total=len(servers))


# ── POST /aspora-ticket ──────────────────────────────────────────────────────

_DEFAULT_USER_CODE = "309f818b-6c63-49b3-b701-ae8372ea3eb7"

_MYSQL_INFRA_TYPES = {"aurora_mysql_infrastructuretype_ref"}


def _db_type(infrastructuretype_ref_code: str) -> str:
    return "mysql" if infrastructuretype_ref_code in _MYSQL_INFRA_TYPES else "postgresql"


# ── GET /aspora-databases ────────────────────────────────────────────────────

@router.get(
    "/aspora-databases",
    summary="Aspora — List Databases with Full Schema/Table Tree",
    dependencies=[Depends(_verify_api_key)],
)
async def aspora_databases(
    infrastructure_mst_code: str = Query(..., description="Server code from /aspora-servers"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all databases on a server with their full schema/table tree embedded.
    Also includes db_type so the caller knows which grant levels to use.
    Use the id values from this response as db_object_mst_id in /aspora-user-management.
    """
    service = DbObjectService(db)
    return await service.list_databases_with_tree(
        infrastructure_mst_code=infrastructure_mst_code,
        tenant_code=_TENANT_CODE,
    )


# ── GET /aspora-permissions ──────────────────────────────────────────────────

@router.get(
    "/aspora-permissions",
    summary="Aspora — List Users and Permissions for a DB Server",
    dependencies=[Depends(_verify_api_key)],
)
async def aspora_permissions(
    infrastructure_mst_code: str = Query(..., description="Infrastructure code of the DB server"),
    username: Optional[str] = Query(default=None, description="Optional regex to filter by username (case-insensitive, PostgreSQL ~*)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns existing users and their permission hierarchy for a DB server.
    Aspora uses this to show current state before submitting a user management ticket.
    Pass `username` as a regex pattern to narrow results to matching users.
    """
    service = DbPermissionService(db)
    return await service.list_users_filtered(
        infrastructure_mst_code=infrastructure_mst_code,
        tenant_code=_TENANT_CODE,
        username_pattern=username,
    )


# ── POST /aspora-user-management ─────────────────────────────────────────────────

class AsporaPermissionGrant(BaseModel):
    permission_id: Optional[int] = None
    db_object_mst_id: Optional[int] = None
    permissions: List[str] = []


class AsporaUserTicketRequest(BaseModel):
    infrastructure_mst_code: str = Field(..., description="Code of the target DB server")
    username: str = Field(..., min_length=1, max_length=100, description="Database username")
    password: Optional[str] = Field(default=None, description="Database password")
    environment: str = Field(..., description="Target environment: dev | stage | qa | prod")
    grants: List[AsporaPermissionGrant] = Field(default_factory=list, description="Permission grants (db_object_mst_id + permissions[])")


class AsporaUserTicketResponse(BaseModel):
    upsert_result: Dict[str, Any]
    queue_code: str
    queue_id: int
    ticket_code: str
    ticket_number: str
    pr_result: Dict[str, Any]
    message: str


@router.post(
    "/aspora-user-management",
    response_model=AsporaUserTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Aspora User Ticket — Upsert DB User Permissions + Queue Item",
    dependencies=[Depends(_verify_api_key)],
)
async def aspora_user_ticket(
    request: AsporaUserTicketRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Upserts DB user permissions and creates a transaction queue item for user management.

    Authenticated via X-Api-Key (M2M — no user JWT required).

    Internally:
    - case_ref_code is hardcoded as "user_management"
    - Resolves db_object names to build the pgsql_servers/mysql_servers config_snapshot
    - Ticket, queue item, and PR are created as in the database creation flow
    """
    user_code = _TENANT_USER_CODE.get(_TENANT_CODE, _DEFAULT_USER_CODE)

    # ── Step 0: resolve infra record ────────────────────────────────────────
    infra_repo = InfrastructureMstRepository(db)
    infra = await infra_repo.get_by_code(request.infrastructure_mst_code)
    if not infra:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Infrastructure server not found: {request.infrastructure_mst_code}",
        )

    db_type = _db_type(infra.infrastructuretype_ref_code)
    is_mysql = db_type == "mysql"
    server_key = "mysql_servers" if is_mysql else "pgsql_servers"

    # ── Step 1: generate ticket ──────────────────────────────────────────────
    ticket_service = TicketService(db)
    ticket = await ticket_service.generate_ticket_number(
        tenants_mst_code=_TENANT_CODE,
        user_mst_code=user_code,
        name=f"User management: {request.username} on {infra.name} ({request.environment})",
        description=f"Aspora-triggered user permission upsert on server {infra.name}",
    )
    logger.info("Aspora user ticket: generated ticket=%s", ticket.ticket_number)

    # ── Step 2: upsert user permissions ─────────────────────────────────────
    try:
        perm_service = DbPermissionService(db)
        upsert_result = await perm_service.upsert_user_permissions(
            infrastructure_mst_code=request.infrastructure_mst_code,
            username=request.username,
            grants=[g.model_dump() for g in request.grants],
            tenant_code=_TENANT_CODE,
            environment=request.environment,
            password=request.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    logger.info("Aspora user ticket: upserted user=%s result=%s", request.username, upsert_result)

    # ── Step 3: build config_snapshot ───────────────────────────────────────
    # Load all db_objects for this server to resolve names from IDs
    db_obj_repo = DbObjectMstRepository(db)
    all_objs = await db_obj_repo.get_by_server(request.infrastructure_mst_code)
    obj_map = {o.id: o for o in all_objs}

    def _root_db(obj_id: int):
        """Walk up parent chain to find the root database object."""
        obj = obj_map.get(obj_id)
        while obj and obj.parent_id is not None:
            obj = obj_map.get(obj.parent_id)
        return obj

    # Group grants by database name, building the nested snapshot structure
    db_grants: Dict[str, Any] = {}
    for g in request.grants:
        if not g.db_object_mst_id:
            continue
        obj = obj_map.get(g.db_object_mst_id)
        if not obj:
            continue
        root = _root_db(g.db_object_mst_id)
        if not root:
            continue

        db_name = root.name
        if db_name not in db_grants:
            db_grants[db_name] = (
                {"database": db_name, "tables": [{"table": "all", "privileges": []}]}
                if is_mysql
                else {"database": db_name, "permissions": [], "schemas": []}
            )

        if obj.type == "database":
            if is_mysql:
                db_grants[db_name]["tables"][0]["privileges"] = g.permissions
            else:
                db_grants[db_name]["permissions"] = g.permissions
        elif obj.type == "schema" and not is_mysql:
            schemas = db_grants[db_name]["schemas"]
            schema_entry = next((s for s in schemas if s["schemaName"] == obj.name), None)
            if not schema_entry:
                schema_entry = {"schemaName": obj.name, "permissions": [], "tables": []}
                schemas.append(schema_entry)
            schema_entry["permissions"] = g.permissions
        elif obj.type == "table":
            if is_mysql:
                db_grants[db_name]["tables"][0]["privileges"] = g.permissions
            else:
                parent = obj_map.get(obj.parent_id) if obj.parent_id else None
                if parent and parent.type == "schema":
                    schemas = db_grants[db_name]["schemas"]
                    schema_entry = next((s for s in schemas if s["schemaName"] == parent.name), None)
                    if not schema_entry:
                        schema_entry = {"schemaName": parent.name, "permissions": [], "tables": []}
                        schemas.append(schema_entry)
                    schema_entry["tables"].append({
                        "tableName": "all" if obj.name == "*" else obj.name,
                        "privileges": g.permissions,
                    })

    config_snapshot: Dict[str, Any] = {
        server_key: [{
            "db_server_name": infra.name,
            "users": [{
                "db_user_name": request.username,
                "db_password": request.password or "",
                "grants": list(db_grants.values()),
            }],
        }],
        "replace_grants": True,
        "environment": request.environment,
        "case_ref_code": "user_management",
        "case_type_ref_code": "database",
        "infrastructure_mst_code": infra.code,
        "infrastructuretype_ref_code": infra.infrastructuretype_ref_code,
        "applications_mst_code": infra.applications_mst_code or "",
        "geo_loc_mst_code": infra.geo_loc_mst_code or "",
    }

    # ── Step 4: add to transaction queue ────────────────────────────────────
    try:
        queue_service = TransactionQueueService(db)
        queue_item = await queue_service.add_item_to_queue(
            user_code=user_code,
            tenant_code=_TENANT_CODE,
            transaction_code=request.infrastructure_mst_code,
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            config_snapshot=config_snapshot,
            case_ref_code="user_management",
            ticket_code=ticket.code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    logger.info("Aspora user ticket: queue item created code=%s", queue_item.code)

    # ── Step 5: trigger script PR workflow ──────────────────────────────────
    try:
        pr_service = ScriptPRWorkflowService(db)
        pr_result = await pr_service.create(
            user_code=user_code,
            tenant_code=_TENANT_CODE,
            queue_ids=[queue_item.id],
        )
    except Exception as exc:
        logger.error("Aspora user ticket: PR workflow failed for queue=%s: %s", queue_item.code, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"permissions upserted (user={request.username}, queue={queue_item.code}) but PR workflow failed: {exc}",
        )

    logger.info("Aspora user ticket: PR workflow complete for queue=%s", queue_item.code)

    return AsporaUserTicketResponse(
        upsert_result=upsert_result,
        queue_code=queue_item.code,
        queue_id=queue_item.id,
        ticket_code=ticket.code,
        ticket_number=ticket.ticket_number,
        pr_result=pr_result,
        message=f"User '{request.username}' permissions saved and PR opened successfully",
    )


# ── POST /aspora-ticket ──────────────────────────────────────────────────────

class AsporaTicketRequest(BaseModel):
    infrastructure_mst_code: str = Field(..., description="Code of the target DB server (from /aspora-servers)")
    database_name: str = Field(..., min_length=1, max_length=100, description="Name of the database to create")
    environment: str = Field(..., description="Target environment: dev | stage | qa | prod")


class AsporaTicketResponse(BaseModel):
    db_object_code: str
    db_object_id: int
    queue_code: str
    queue_id: int
    ticket_code: str
    ticket_number: str
    pr_result: Dict[str, Any]
    message: str


@router.post(
    "/aspora-ticket",
    response_model=AsporaTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Aspora Ticket — Create Database + Queue Item",
    dependencies=[Depends(_verify_api_key)],
)
async def aspora_ticket(
    request: AsporaTicketRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Creates a db_object_mst entry and a transaction queue item for a new database.

    Authenticated via X-Api-Key (M2M — no user JWT required).

    Internally:
    - case_ref_code is hardcoded as "database_creation"
    - ticket_code is generated via TicketService
    - config_snapshot is built from the infrastructure_mst record (no caller input needed)
    """
    user_code = _TENANT_USER_CODE.get(_TENANT_CODE, _DEFAULT_USER_CODE)

    # ── Step 0: resolve infra_mst record to build config_snapshot ───────────
    infra_repo = InfrastructureMstRepository(db)
    infra = await infra_repo.get_by_code(request.infrastructure_mst_code)
    if not infra:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Infrastructure server not found: {request.infrastructure_mst_code}",
        )

    # ── Step 1: generate ticket ──────────────────────────────────────────────
    ticket_service = TicketService(db)
    ticket = await ticket_service.generate_ticket_number(
        tenants_mst_code=_TENANT_CODE,
        user_mst_code=user_code,
        name=f"Database creation: {request.database_name} ({request.environment})",
        description=f"Aspora-triggered database provisioning on server {infra.name}",
    )
    logger.info("Aspora ticket: generated ticket=%s", ticket.ticket_number)

    # ── Step 2: create db_object_mst entry ──────────────────────────────────
    logger.info(
        "Aspora ticket: creating database '%s' on infra=%s tenant=%s env=%s",
        request.database_name,
        request.infrastructure_mst_code,
        _TENANT_CODE,
        request.environment,
    )
    try:
        db_service = DbObjectService(db)
        db_result = await db_service.create_database(
            infrastructure_mst_code=request.infrastructure_mst_code,
            database_name=request.database_name,
            tenant_code=_TENANT_CODE,
            environment=request.environment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    db_object_code: str = db_result["code"]
    db_object_id: int = db_result["id"]
    logger.info("Aspora ticket: db_object created code=%s", db_object_code)

    # ── Step 3: build config_snapshot from infra_mst record ─────────────────
    config_snapshot: Dict[str, Any] = {
        "database_name": request.database_name,
        "db_server_name": infra.name,
        "infrastructure_mst_code": infra.code,
        "infrastructuretype_ref_code": infra.infrastructuretype_ref_code,
        "geo_loc_mst_code": infra.geo_loc_mst_code or "",
        "environment": request.environment,
        "applications_mst_code": infra.applications_mst_code or "",
        "case_ref_code": _CASE_REF_CODE,
    }
    # ── Step 4: add to transaction queue ────────────────────────────────────
    try:
        queue_service = TransactionQueueService(db)
        queue_item = await queue_service.add_item_to_queue(
            user_code=user_code,
            tenant_code=_TENANT_CODE,
            transaction_code=request.infrastructure_mst_code,
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            config_snapshot=config_snapshot,
            case_ref_code=_CASE_REF_CODE,
            ticket_code=ticket.code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    logger.info("Aspora ticket: queue item created code=%s", queue_item.code)

    # ── Step 5: trigger script PR workflow ──────────────────────────────────
    try:
        pr_service = ScriptPRWorkflowService(db)
        pr_result = await pr_service.create(
            user_code=user_code,
            tenant_code=_TENANT_CODE,
            queue_ids=[queue_item.id],
        )
    except Exception as exc:
        logger.error("Aspora ticket: PR workflow failed for queue=%s: %s", queue_item.code, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"db_object and queue item created (db={db_object_code}, queue={queue_item.code}) but PR workflow failed: {exc}",
        )

    logger.info("Aspora ticket: PR workflow complete for queue=%s", queue_item.code)

    return AsporaTicketResponse(
        db_object_code=db_object_code,
        db_object_id=db_object_id,
        queue_code=queue_item.code,
        queue_id=queue_item.id,
        ticket_code=ticket.code,
        ticket_number=ticket.ticket_number,
        pr_result=pr_result,
        message=f"Database '{request.database_name}' created, queued, and PR opened successfully",
    )
