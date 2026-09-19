"""
Database Object API Endpoints

Endpoints for fetching and creating database objects (databases, schemas, tables)
for a given database server (K8s Postgres, Aurora MySQL, etc.).
"""
import logging
from typing import Tuple, List, Any, Dict
from fastapi import APIRouter, HTTPException, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.db_object_service import DbObjectService

logger = logging.getLogger(__name__)

router = APIRouter()


class DatabaseItem(BaseModel):
    id: int
    code: str
    name: str
    type: str
    environment: str
    metadata: Dict[str, Any] = {}

    class Config:
        from_attributes = True


class DatabaseListResponse(BaseModel):
    total: int
    databases: List[DatabaseItem]


class CreateDatabaseRequest(BaseModel):
    infrastructure_mst_code: str = Field(..., description="Infrastructure code of the database server")
    database_name: str = Field(..., min_length=1, max_length=100, description="Name of the database to create")
    server_name: str = Field(..., description="Display name of the server")
    environment: str = Field(..., description="Environment: dev, stage, qa, prod")


class TableEntry(BaseModel):
    id: int
    code: str
    name: str
    type: str
    parent_id: int

    class Config:
        from_attributes = True


class CreateDatabaseResponse(BaseModel):
    database: DatabaseItem
    table_entry: TableEntry
    message: str


class TableTreeItem(BaseModel):
    id: int
    name: str


class SchemaTreeItem(BaseModel):
    id: int
    name: str
    tables: List[TableTreeItem] = []


class SingleDatabaseTree(BaseModel):
    schemas: List[SchemaTreeItem] = []
    tables: List[TableTreeItem] = []


class DatabaseTreeRequest(BaseModel):
    database_ids: List[int]


class DatabaseTreeResponse(BaseModel):
    trees: Dict[str, SingleDatabaseTree] = {}


@router.get("/databases", response_model=DatabaseListResponse, summary="List Databases for a Postgres Server")
async def list_databases(
    infrastructure_mst_code: str = Query(..., description="Infrastructure code of the database server"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all databases recorded for a given database server.
    """
    try:
        user, tenant = user_and_tenant
        service = DbObjectService(db)
        result = await service.list_databases(
            infrastructure_mst_code=infrastructure_mst_code,
            tenant_code=tenant.code,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/databases", response_model=CreateDatabaseResponse, status_code=status.HTTP_201_CREATED, summary="Create a Database under a Server")
async def create_database(
    request: CreateDatabaseRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new database under an existing server (Aurora MySQL, K8s Postgres, etc.).

    Inserts a row into db_object_mst (type='database').
    Frontend is responsible for adding to transaction queue after this call.
    """
    try:
        user, tenant = user_and_tenant

        service = DbObjectService(db)
        db_item = await service.create_database(
            infrastructure_mst_code=request.infrastructure_mst_code,
            database_name=request.database_name,
            tenant_code=tenant.code,
            environment=request.environment,
        )

        table_entry_data = db_item.pop("table_entry")
        return CreateDatabaseResponse(
            database=DatabaseItem(**db_item),
            table_entry=TableEntry(**table_entry_data),
            message=f"Database '{request.database_name}' created successfully",
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/databases/tree",
    response_model=DatabaseTreeResponse,
    summary="Get schemas and tables for one or more databases",
)
async def get_database_tree(
    request: DatabaseTreeRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the schema → table tree for one or more database objects.

    Accepts a list of database_ids. Returns a dict keyed by database ID.
    For a single database, pass one ID in the array.
    For "All Databases", pass all database IDs.
    """
    try:
        service = DbObjectService(db)
        trees = await service.get_database_tree(request.database_ids)
        return {"trees": trees}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
