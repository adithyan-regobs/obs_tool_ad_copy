"""
Database Permission Service

Fetches users and their grouped permissions for a K8s-hosted database server.

For PostgreSQL: groups as user → database → schema → table
For MySQL:      groups as user → database (table-level privileges, no schema)
"""
import logging
import uuid
from typing import Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.db_object_mst_repository import DbObjectMstRepository
from app.repository.db_permission_mst_repository import DbPermissionMstRepository
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


class DbPermissionService:

    def __init__(self, session: AsyncSession):
        self.session = session
        self.db_object_repo = DbObjectMstRepository(session)
        self.db_permission_repo = DbPermissionMstRepository(session)
        self.infra_repo = InfrastructureMstRepository(session)

    async def upsert_user_permissions(
        self,
        infrastructure_mst_code: str,
        username: str,
        grants: List[Dict[str, Any]],
        tenant_code: str,
        environment: str,
        password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create or update permission rows for a user on a database server.

        Each grant has { permission_id, db_object_mst_id, permissions: [...] }.
        If permission_id is present → update by primary key.
        Otherwise → create new row.
        Password is stored in metadata_json on every row for the user.
        """
        created = 0
        updated = 0
        metadata = {"password": password} if password else None

        for grant in grants:
            permission_id = grant.get("permission_id")
            permissions = grant.get("permissions", [])

            if permission_id:
                # Existing permission row → update by primary key
                existing = await self.db_permission_repo.get_by_id(permission_id)
                if existing:
                    update_fields: Dict[str, Any] = {"permissions": permissions}
                    if metadata:
                        existing_meta = existing.metadata_json or {}
                        existing_meta.update(metadata)
                        update_fields["metadata_json"] = existing_meta
                    await self.db_permission_repo.update(existing, update_fields)
                    updated += 1
            else:
                # New permission row → create
                db_object_mst_id = grant.get("db_object_mst_id")
                await self.db_permission_repo.create(
                    code=f"dbperm-{uuid.uuid4().hex[:12]}",
                    name=username,
                    infrastructure_mst_code=infrastructure_mst_code,
                    db_object_mst_id=db_object_mst_id,
                    username=username,
                    permissions=permissions,
                    tenant_code=tenant_code,
                    environment=environment,
                    metadata_json=metadata or {},
                )
                created += 1

        await self.session.commit()

        return {
            "username": username,
            "infrastructure_mst_code": infrastructure_mst_code,
            "created": created,
            "updated": updated,
            "total_grants": len(grants),
        }

    async def search_usernames(
        self,
        tenant_code: str,
        applications_mst_code: str,
        exclude_infrastructure_mst_code: str,
        username_prefix: str = "",
        environment: Optional[str] = None,
        geo_loc_mst_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search usernames from sibling servers under the same application,
        excluding the current server. Returns username, password, server info.
        """
        rows = await self.db_permission_repo.search_usernames_across_servers(
            tenant_code=tenant_code,
            applications_mst_code=applications_mst_code,
            exclude_infrastructure_mst_code=exclude_infrastructure_mst_code,
            username_prefix=username_prefix,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )

        results = []
        for row in rows:
            infra_type = row.get("infrastructuretype_ref_code", "")
            if infra_type in MYSQL_INFRA_TYPES:
                db_type = "mysql"
            elif infra_type in POSTGRES_INFRA_TYPES:
                db_type = "postgresql"
            else:
                db_type = "unknown"

            results.append({
                "username": row["username"],
                "password": row.get("password"),
                "server_name": row["server_name"],
                "infrastructure_mst_code": row["infrastructure_mst_code"],
                "db_type": db_type,
            })

        return results

    async def list_users_filtered(
        self,
        infrastructure_mst_code: str,
        tenant_code: str,
        username_pattern: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Same as list_users but with an optional regex filter on username.
        Uses PostgreSQL ~* (case-insensitive) to filter at the DB level.
        """
        infra = await self.infra_repo.get_by_code(infrastructure_mst_code)
        infra_type = infra.infrastructuretype_ref_code if infra else None

        if username_pattern:
            all_perms = await self.db_permission_repo.get_by_server_username_filter(
                infrastructure_mst_code=infrastructure_mst_code,
                username_pattern=username_pattern,
            )
        else:
            all_perms = await self.db_permission_repo.get_by_server(infrastructure_mst_code)

        all_perms = [p for p in all_perms if p.tenant_code == tenant_code]

        all_objects = await self.db_object_repo.get_by_server(infrastructure_mst_code)
        all_objects = [o for o in all_objects if o.tenant_code == tenant_code]
        obj_index: Dict[int, Any] = {o.id: o for o in all_objects}
        all_databases = [o for o in all_objects if o.type == "database"]

        is_mysql = infra_type in MYSQL_INFRA_TYPES

        if is_mysql:
            users = _build_mysql_users(all_perms, obj_index, server_name=infrastructure_mst_code)
        else:
            users = _build_postgres_users(
                all_perms, obj_index, all_databases=all_databases,
                server_name=infrastructure_mst_code,
            )

        return {
            "total": len(users),
            "db_type": "mysql" if is_mysql else "postgresql",
            "users": users,
        }

    async def list_users(
        self,
        infrastructure_mst_code: str,
        tenant_code: str,
    ) -> Dict[str, Any]:
        """
        Return all users on a database server grouped by permission hierarchy.

        PostgreSQL response shape per user:
          { username, is_server_admin, databases: [ PsqlDatabasePermission ] }

        MySQL response shape per user:
          { username, databases: [ MysqlDatabasePermission ] }
        """
        # 1. Detect infrastructure type
        infra = await self.infra_repo.get_by_code(infrastructure_mst_code)
        infra_type = infra.infrastructuretype_ref_code if infra else None

        # 2. Load all permission records for this server (tenant-scoped)
        all_perms = await self.db_permission_repo.get_by_server(infrastructure_mst_code)
        all_perms = [p for p in all_perms if p.tenant_code == tenant_code]

        # 3. Load all db objects → build id→object index
        all_objects = await self.db_object_repo.get_by_server(infrastructure_mst_code)
        all_objects = [o for o in all_objects if o.tenant_code == tenant_code]
        obj_index: Dict[int, Any] = {o.id: o for o in all_objects}

        # All database objects on this server (for server-admin expansion)
        all_databases = [o for o in all_objects if o.type == "database"]

        is_mysql = infra_type in MYSQL_INFRA_TYPES

        if is_mysql:
            users = _build_mysql_users(
                all_perms, obj_index, server_name=infrastructure_mst_code
            )
        else:
            # Default to PostgreSQL structure
            users = _build_postgres_users(
                all_perms, obj_index, all_databases=all_databases,
                server_name=infrastructure_mst_code
            )

        return {
            "total": len(users),
            "db_type": "mysql" if is_mysql else "postgresql",
            "users": users,
        }


# ── PostgreSQL hierarchy builder ───────────────────────────────────────────────

def _build_postgres_users(
    all_perms: List[Any],
    obj_index: Dict[int, Any],
    all_databases: List[Any],
    server_name: str,
) -> List[Dict]:
    """
    Groups permissions by: user → database → schema → table
    Matches PsqlDatabasePermission frontend interface.

    Server admins automatically get all databases with ALL PRIVILEGES
    since superuser access bypasses all permission checks.
    """
    # user_map[username] = { is_server_admin, databases: { db_id: {...} } }
    user_map: Dict[str, Dict] = {}

    for perm in all_perms:
        username = perm.username
        raw_perms = perm.permissions

        if username not in user_map:
            user_map[username] = {
                "username": username,
                "is_server_admin": False,
                "password": None,
                "databases": {},
            }

        # Extract password from metadata_json if not already found
        if user_map[username]["password"] is None and perm.metadata_json:
            pwd = perm.metadata_json.get("password") if isinstance(perm.metadata_json, dict) else None
            if pwd:
                user_map[username]["password"] = pwd

        # Server-level (null db_object_mst_id)
        if perm.db_object_mst_id is None:
            if raw_perms == "admin" or raw_perms == ["admin"]:
                user_map[username]["is_server_admin"] = True
            continue

        obj = obj_index.get(perm.db_object_mst_id)
        if obj is None:
            continue

        if obj.type == "database":
            _ensure_db(user_map[username]["databases"], obj, server_name)
            user_map[username]["databases"][obj.id]["permissionId"] = perm.id
            user_map[username]["databases"][obj.id]["dbObjectId"] = obj.id
            user_map[username]["databases"][obj.id]["permissions"] = _to_list(raw_perms)

        elif obj.type == "schema":
            db_id = _ancestor_id(obj, obj_index, "database")
            if db_id is None:
                continue
            _ensure_db_from_index(user_map[username]["databases"], db_id, obj_index, server_name)
            schemas = user_map[username]["databases"][db_id]["schemas"]
            if obj.id not in schemas:
                schemas[obj.id] = {"permissionId": perm.id, "dbObjectId": obj.id, "schemaName": obj.name, "permissions": [], "tables": []}
            else:
                schemas[obj.id]["permissionId"] = perm.id
            schemas[obj.id]["permissions"] = _to_list(raw_perms)

        elif obj.type == "table":
            schema_id = _ancestor_id(obj, obj_index, "schema")
            if schema_id is None:
                continue
            schema_obj = obj_index.get(schema_id)
            db_id = _ancestor_id(schema_obj, obj_index, "database") if schema_obj else None
            if db_id is None:
                continue
            _ensure_db_from_index(user_map[username]["databases"], db_id, obj_index, server_name)
            schemas = user_map[username]["databases"][db_id]["schemas"]
            if schema_id not in schemas:
                schemas[schema_id] = {
                    "permissionId": None,
                    "dbObjectId": schema_id,
                    "schemaName": schema_obj.name if schema_obj else "",
                    "permissions": [],
                    "tables": [],
                }
            schemas[schema_id]["tables"].append({
                "permissionId": perm.id,
                "dbObjectId": obj.id,
                "tableName": obj.name,
                "privileges": _to_list(raw_perms),
            })

    # For server admins — fill all databases with ALL PRIVILEGES
    for u in user_map.values():
        if u["is_server_admin"]:
            for db_obj in all_databases:
                if db_obj.id not in u["databases"]:
                    u["databases"][db_obj.id] = {
                        "permissionId": None,
                        "dbObjectId": db_obj.id,
                        "serverName": server_name,
                        "databaseName": db_obj.name,
                        "permissions": ["ALL PRIVILEGES"],
                        "schemas": {},
                    }
                else:
                    # Override with ALL PRIVILEGES since they're a superuser
                    u["databases"][db_obj.id]["permissions"] = ["ALL PRIVILEGES"]

    # Flatten dicts → lists
    result = []
    for u in user_map.values():
        db_list = []
        for db_entry in u["databases"].values():
            db_list.append({
                "permissionId": db_entry.get("permissionId"),
                "dbObjectId": db_entry.get("dbObjectId"),
                "serverName": db_entry["serverName"],
                "databaseName": db_entry["databaseName"],
                "permissions": db_entry["permissions"],
                "schemas": list(db_entry["schemas"].values()),
            })
        result.append({
            "username": u["username"],
            "is_server_admin": u["is_server_admin"],
            "password": u.get("password"),
            "databases": db_list,
        })
    return result


# ── MySQL hierarchy builder ────────────────────────────────────────────────────

def _build_mysql_users(
    all_perms: List[Any],
    obj_index: Dict[int, Any],
    server_name: str,
) -> List[Dict]:
    """
    Groups permissions by: user → database
    Matches MysqlDatabasePermission frontend interface (no schema level).
    """
    user_map: Dict[str, Dict] = {}

    for perm in all_perms:
        username = perm.username
        raw_perms = perm.permissions

        if username not in user_map:
            user_map[username] = {"username": username, "password": None, "databases": {}}

        # Extract password from metadata_json if not already found
        if user_map[username]["password"] is None and perm.metadata_json:
            pwd = perm.metadata_json.get("password") if isinstance(perm.metadata_json, dict) else None
            if pwd:
                user_map[username]["password"] = pwd

        if perm.db_object_mst_id is None:
            continue

        obj = obj_index.get(perm.db_object_mst_id)
        if obj is None:
            continue

        if obj.type == "database":
            if obj.id not in user_map[username]["databases"]:
                user_map[username]["databases"][obj.id] = {
                    "permissionId": perm.id,
                    "dbObjectId": obj.id,
                    "serverName": server_name,
                    "databaseName": obj.name,
                    "privileges": _to_list(raw_perms),
                }
            else:
                existing = user_map[username]["databases"][obj.id]["privileges"]
                for p in _to_list(raw_perms):
                    if p not in existing:
                        existing.append(p)

        elif obj.type == "table":
            db_obj = obj_index.get(obj.parent_id) if obj.parent_id else None
            if db_obj is None or db_obj.type != "database":
                continue
            if db_obj.id not in user_map[username]["databases"]:
                user_map[username]["databases"][db_obj.id] = {
                    "permissionId": perm.id,
                    "dbObjectId": db_obj.id,
                    "serverName": server_name,
                    "databaseName": db_obj.name,
                    "privileges": _to_list(raw_perms),
                }
            else:
                existing = user_map[username]["databases"][db_obj.id]["privileges"]
                for p in _to_list(raw_perms):
                    if p not in existing:
                        existing.append(p)

    result = []
    for u in user_map.values():
        result.append({
            "username": u["username"],
            "password": u.get("password"),
            "databases": list(u["databases"].values()),
        })
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _to_list(permissions: Any) -> List[str]:
    if isinstance(permissions, list):
        return permissions
    if isinstance(permissions, str):
        return [permissions]
    return []


def _ancestor_id(obj: Any, obj_index: Dict[int, Any], target_type: str) -> Optional[int]:
    """Walk parent_id chain to find the nearest ancestor of target_type."""
    current = obj
    visited: set = set()
    while current is not None:
        if current.id in visited:
            break
        visited.add(current.id)
        parent = obj_index.get(current.parent_id) if current.parent_id else None
        if parent is None:
            break
        if parent.type == target_type:
            return parent.id
        current = parent
    return None


def _ensure_db(db_dict: Dict, obj: Any, server_name: str) -> None:
    if obj.id not in db_dict:
        db_dict[obj.id] = {
            "permissionId": None,
            "dbObjectId": obj.id,
            "serverName": server_name,
            "databaseName": obj.name,
            "permissions": [],
            "schemas": {},
        }


def _ensure_db_from_index(
    db_dict: Dict,
    db_id: int,
    obj_index: Dict[int, Any],
    server_name: str,
) -> None:
    if db_id not in db_dict:
        db_obj = obj_index.get(db_id)
        db_dict[db_id] = {
            "permissionId": None,
            "dbObjectId": db_id,
            "serverName": server_name,
            "databaseName": db_obj.name if db_obj else "",
            "permissions": [],
            "schemas": {},
        }
