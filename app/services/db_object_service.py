"""
Database Object Service

Service layer for db_object_mst operations.
"""
import logging
from typing import Dict, Any, List
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.db_object_mst_repository import DbObjectMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

logger = logging.getLogger(__name__)

POSTGRES_INFRA_TYPES = {
    "devlift_k8s_postgres_infrastructuretype_ref",
    "rds_postgres_infrastructuretype_ref",
    "aurora_postgres_infrastructuretype_ref",
}
MYSQL_INFRA_TYPES = {
    "devlift_k8s_mysql_infrastructuretype_ref",
    "rds_mysql_infrastructuretype_ref",
    "aurora_mysql_infrastructuretype_ref",
}


class DbObjectService:

    def __init__(self, session: AsyncSession):
        self.session = session
        self.db_object_repo = DbObjectMstRepository(session)
        self.infra_repo = InfrastructureMstRepository(session)

    async def list_databases(
        self,
        infrastructure_mst_code: str,
        tenant_code: str,
    ) -> Dict[str, Any]:
        """
        List all databases for a given database server.
        """
        databases = await self.db_object_repo.get_by_server_and_type(
            infrastructure_mst_code=infrastructure_mst_code,
            object_type="database",
        )

        # Filter by tenant
        databases = [db for db in databases if db.tenant_code == tenant_code]

        items = []
        for db in databases:
            items.append({
                "id": db.id,
                "code": db.code,
                "name": db.name,
                "type": db.type,
                "environment": db.environment,
                "metadata": db.metadata_json or {},
            })

        return {
            "total": len(items),
            "databases": items,
        }

    async def list_databases_with_tree(
        self,
        infrastructure_mst_code: str,
        tenant_code: str,
    ) -> Dict[str, Any]:
        """
        Returns all databases on a server with their full schema/table tree embedded.
        Also includes db_type (postgresql or mysql) derived from the infra record.

        Combined response shape:
          {
            "db_type": "postgresql",
            "databases": [
              {
                "id": 101, "name": "app_db", "environment": "prod",
                "schemas": [{ "id": 202, "name": "public", "tables": [{ "id": 303, "name": "orders" }] }],
                "tables": []
              }
            ]
          }
        """
        list_result = await self.list_databases(
            infrastructure_mst_code=infrastructure_mst_code,
            tenant_code=tenant_code,
        )
        databases = list_result["databases"]

        db_type = "postgresql"
        if databases:
            infra = await self.infra_repo.get_by_code(infrastructure_mst_code)
            if infra and infra.infrastructuretype_ref_code in MYSQL_INFRA_TYPES:
                db_type = "mysql"

        db_ids = [d["id"] for d in databases]
        trees = await self.get_database_tree(db_ids) if db_ids else {}

        enriched = []
        for d in databases:
            tree = trees.get(str(d["id"]), {})
            enriched.append({
                "id": d["id"],
                "name": d["name"],
                "environment": d["environment"],
                "schemas": tree.get("schemas", []),
                "tables": tree.get("tables", []),
            })

        return {"db_type": db_type, "databases": enriched}

    async def create_database(
        self,
        infrastructure_mst_code: str,
        database_name: str,
        tenant_code: str,
        environment: str,
    ) -> Dict[str, Any]:
        """
        Create a new database entry under a given server (Aurora MySQL / K8s Postgres).

        Inserts a row into db_object_mst with type='database' and parent_id=NULL.

        Returns:
            Dict with the created database item details.

        Raises:
            ValueError: If infrastructure not found or database name already exists.
        """
        # 1. Validate infrastructure exists
        infra = await self.infra_repo.get_by_code(infrastructure_mst_code)
        if not infra:
            raise ValueError(f"Infrastructure not found: {infrastructure_mst_code}")

        # 2. Check duplicate database name on same server
        existing = await self.db_object_repo.get_by_server_name_type(
            infrastructure_mst_code=infrastructure_mst_code,
            name=database_name,
            object_type="database",
        )
        if existing:
            raise ValueError(f"Database '{database_name}' already exists on this server")

        # 3. Generate unique code and create database entry
        db_code = f"DBOBJ_{uuid4().hex[:8].upper()}"
        db_obj = await self.db_object_repo.create(
            code=db_code,
            name=database_name,
            description=f"Database {database_name} on {infrastructure_mst_code}",
            infrastructure_mst_code=infrastructure_mst_code,
            type="database",
            parent_id=None,
            tenant_code=tenant_code,
            environment=environment,
        )

        is_postgres = infra.infrastructuretype_ref_code in POSTGRES_INFRA_TYPES

        result = {
            "id": db_obj.id,
            "code": db_obj.code,
            "name": db_obj.name,
            "type": db_obj.type,
            "environment": db_obj.environment,
            "metadata": db_obj.metadata_json or {},
        }

        if is_postgres:
            # 4a. PostgreSQL: database → schema(*) → table(*)
            schema_code = f"DBOBJ_{uuid4().hex[:8].upper()}"
            schema_obj = await self.db_object_repo.create(
                code=schema_code,
                name="public",
                description=f"Schemas public in {database_name} on {infrastructure_mst_code}",
                infrastructure_mst_code=infrastructure_mst_code,
                type="schema",
                parent_id=db_obj.id,
                tenant_code=tenant_code,
                environment=environment,
            )

            table_code = f"DBOBJ_{uuid4().hex[:8].upper()}"
            table_obj = await self.db_object_repo.create(
                code=table_code,
                name="*",
                description=f"All tables in {database_name} on {infrastructure_mst_code}",
                infrastructure_mst_code=infrastructure_mst_code,
                type="table",
                parent_id=schema_obj.id,
                tenant_code=tenant_code,
                environment=environment,
            )

            result["schema_entry"] = {
                "id": schema_obj.id,
                "code": schema_obj.code,
                "name": schema_obj.name,
                "type": schema_obj.type,
                "parent_id": db_obj.id,
            }
            result["table_entry"] = {
                "id": table_obj.id,
                "code": table_obj.code,
                "name": table_obj.name,
                "type": table_obj.type,
                "parent_id": schema_obj.id,
            }
        else:
            # 4b. MySQL: database → table(*)
            table_code = f"DBOBJ_{uuid4().hex[:8].upper()}"
            table_obj = await self.db_object_repo.create(
                code=table_code,
                name="*",
                description=f"All tables in {database_name} on {infrastructure_mst_code}",
                infrastructure_mst_code=infrastructure_mst_code,
                type="table",
                parent_id=db_obj.id,
                tenant_code=tenant_code,
                environment=environment,
            )

            result["table_entry"] = {
                "id": table_obj.id,
                "code": table_obj.code,
                "name": table_obj.name,
                "type": table_obj.type,
                "parent_id": db_obj.id,
            }

        return result

    async def _build_single_tree(self, database_id: int) -> Dict[str, Any]:
        """Build tree for a single database."""
        children = await self.db_object_repo.get_children(database_id)
        schemas_raw = [c for c in children if c.type == "schema"]
        tables_raw = [c for c in children if c.type == "table"]

        schemas: List[Dict[str, Any]] = []
        for schema in schemas_raw:
            schema_tables = await self.db_object_repo.get_children(schema.id)
            schema_tables = [t for t in schema_tables if t.type == "table"]
            schemas.append({
                "id": schema.id,
                "name": schema.name,
                "tables": [{"id": t.id, "name": t.name} for t in schema_tables],
            })

        return {
            "schemas": schemas,
            "tables": [{"id": t.id, "name": t.name} for t in tables_raw],
        }

    async def get_database_tree(self, database_ids: List[int]) -> Dict[str, Any]:
        """
        Get the full tree for one or more databases.

        Accepts a list of database IDs. Returns a dict keyed by database ID.
        Each entry contains:
          PostgreSQL: { schemas: [...], tables: [] }
          MySQL:      { schemas: [], tables: [...] }
        """
        result: Dict[str, Any] = {}
        for db_id in database_ids:
            result[str(db_id)] = await self._build_single_tree(db_id)
        return result
