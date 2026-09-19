"""
Database User Management Service

Provides database user management operations for MCP server consumption.
Supports both MySQL and PostgreSQL with grant structuring.
"""
import logging
import re
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.core.config import settings
from app.utils.tenant_config import get_tenant_config
from app.core.enum import (
    MysqlPrivilegeEnum,
    PgDatabasePrivilegeEnum,
    PgSchemaPrivilegeEnum,
    PgTablePrivilegeEnum,
)
from app.integrations.github_integration import GitHubIntegration

logger = logging.getLogger(__name__)

MYSQL_PRIVILEGES = {p.value for p in MysqlPrivilegeEnum}
PG_DATABASE_PRIVILEGES = {p.value for p in PgDatabasePrivilegeEnum}
PG_SCHEMA_PRIVILEGES = {p.value for p in PgSchemaPrivilegeEnum}
PG_TABLE_PRIVILEGES = {p.value for p in PgTablePrivilegeEnum}


class DatabaseUserManagementService:
    """
    Service for database user management operations.

    Validates user input, structures grants, and formats responses.
    Designed for MCP server consumption.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_database_servers(
        self,
        tenant_code: str,
        environment: Environment,
        geo_loc_code: str,
        product_name: str,
    ) -> Dict[str, Any]:
        """
        List available database servers for a tenant.

        Args:
            tenant_code: Tenant identifier
            environment: Environment enum
            geo_loc_code: Geographic location code
            product_name: Product name (e.g., 'Core', 'Falcon')

        Returns:
            Dict with servers list
        """
        logger.info(
            "DATABASE USER MANAGEMENT - list_database_servers",
            extra={
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        try:
            from app.services.ui_dynamic_form_config_service import UIDynamicFormConfigService

            ui_service = UIDynamicFormConfigService(self.db)

            cfg = await get_tenant_config(tenant_code, self.db)
            result = await ui_service.get_servers_list(
                product_name=product_name,
                environment=environment.value,
                github_repository=cfg.github_infra_repository,
                branch_name=cfg.github_infra_branch,
                tenant=tenant_code,
                geo_loc=geo_loc_code
            )

            logger.info(f"[DB_USER_SERVICE] GitHub API result: success={result.get('success')}, servers={result.get('servers')}")

            servers = []
            for server in result.get("servers", []):
                servers.append({
                    "server_name": server.get("name", ""),
                    "server_type": server.get("type", "Unknown"),
                    "description": f"{server.get('type', 'Database')} server"
                })

            return {
                "servers": servers,
                "count": len(servers),
                "metadata": result.get("metadata", {})
            }

        except Exception as e:
            logger.error(f"Failed to fetch servers: {e}", exc_info=True)
            raise

    async def list_databases_for_server(
        self,
        server_name: str,
        tenant_code: str,
        environment: Environment,
        geo_loc_code: str,
        product_name: str,
    ) -> Dict[str, Any]:
        """
        List databases available on a specific server.

        Args:
            server_name: Server name (e.g., 'common-mysql', 'common-pg')
            tenant_code: Tenant identifier
            environment: Environment enum
            geo_loc_code: Geographic location code
            product_name: Product name

        Returns:
            Dict with databases list and server type
        """
        logger.info(
            "DATABASE USER MANAGEMENT - list_databases_for_server",
            extra={
                "server_name": server_name,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        try:
            from app.services.ui_dynamic_form_config_service import UIDynamicFormConfigService

            ui_service = UIDynamicFormConfigService(self.db)
            cfg = await get_tenant_config(tenant_code, self.db)

            result = await ui_service.get_database_list_from_terragrunt(
                product_name=product_name,
                environment=environment.value,
                github_repository=cfg.github_infra_repository,
                branch_name=cfg.github_infra_branch,
                server_name=server_name,
                tenant=tenant_code,
                geo_loc=geo_loc_code
            )

            databases = result.get("databases", [])
            database_type = result.get("database_type", "unknown")

            logger.info(f"[DB_USER_SERVICE] Server '{server_name}' type: {database_type}, databases: {databases}")

            return {
                "server_name": server_name,
                "server_type": database_type,
                "databases": databases,
                "database_count": len(databases),
                "metadata": result.get("metadata", {})
            }

        except Exception as e:
            logger.error(f"Failed to fetch databases for server '{server_name}': {e}", exc_info=True)
            raise

    async def structure_database_user_grants(
        self,
        server_name: str,
        db_type: str,
        username: str,
        password: str,
        available_databases: List[str],
        # MySQL parameters
        mysql_databases: Optional[List[Dict[str, Any]]] = None,
        # PostgreSQL parameters
        pg_database: Optional[str] = None,
        pg_schema_grants: Optional[List[Dict[str, Any]]] = None,
        # Common placement parameters
        tenant_code: Optional[str] = None,
        product_name: Optional[str] = None,
        environment: Optional[Environment] = None,
        geo_loc_code: Optional[str] = None,
        applications_mst_code: Optional[str] = None,
        infra_vendor: str = "aws",
    ) -> Dict[str, Any]:
        """
        Structure database user grants for MySQL or PostgreSQL.

        Validates databases against available_databases and formats grants.

        Args:
            server_name: Server name (e.g., 'common-mysql', 'common-pg')
            db_type: Database type ('mysql' or 'postgresql')
            username: Username for the new user
            password: Password for the new user
            available_databases: List of databases available on this server (from Tool 2)
            mysql_databases: MySQL grants - list of dicts with database, tables, privileges
            pg_database: PostgreSQL database name
            pg_schema_grants: PostgreSQL schema grants - list of dicts with schema, object_type, privileges
            tenant_code: Tenant identifier
            product_name: Product name
            environment: Environment enum
            geo_loc_code: Geographic location code
            applications_mst_code: Application master code (UUID); falls back to product_name
            infra_vendor: Infrastructure vendor (default: 'aws')

        Returns:
            Dict with attribute_parameters, placement_parameters, is_ready
        """
        logger.info(
            "DATABASE USER MANAGEMENT - structure_database_user_grants",
            extra={
                "server_name": server_name,
                "db_type": db_type,
                "username": username,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value if environment else None,
            }
        )

        # Route to appropriate handler based on db_type
        if db_type == "mysql":
            grants = await self._structure_mysql_grants(
                mysql_databases, available_databases, server_name
            )
            attribute_parameters = {
                "username": username,
                "password": password,
                "server_name": server_name,
                "db_type": "mysql",
                "grants": grants,
            }
        elif db_type == "postgresql":
            grants = await self._structure_pgsql_grants(
                pg_database, pg_schema_grants, available_databases, server_name
            )
            attribute_parameters = {
                "username": username,
                "password": password,
                "server_name": server_name,
                "db_type": "postgresql",
                "grants": grants,
            }
        else:
            raise ValueError(f"Invalid db_type: {db_type}. Must be 'mysql' or 'postgresql'")

        # Resolve product_name → applications_mst_code (UUID)
        from app.infra_chat_agent.config.placement_utils import resolve_product_application_code
        resolved_applications_mst_code = applications_mst_code
        if not resolved_applications_mst_code and tenant_code and product_name:
            app_code, _ = resolve_product_application_code(
                tenant_code, "DatabaseUserManagement", product_name
            )
            if app_code:
                resolved_applications_mst_code = app_code

        # Build placement_parameters
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "applications_mst_code": resolved_applications_mst_code or product_name,
            "environment_enum": environment.value if environment else None,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "database",
            "case_ref_code": "user_management",
        }

        return {
            "status": "success",
            "message": f"Database user '{username}' grants structured for {db_type} server '{server_name}'",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
        }

    async def _structure_mysql_grants(
        self,
        mysql_databases: Optional[List[Dict[str, Any]]],
        available_databases: List[str],
        server_name: str,
    ) -> List[Dict[str, Any]]:
        """
        Structure MySQL grants and validate databases.

        Args:
            mysql_databases: List of dicts with database, tables, privileges
            available_databases: Databases available on this server
            server_name: Server name for error messages

        Returns:
            List of structured grant objects in format expected by script generator

        Raises:
            ValueError: If a database is not found on this server
        """
        if not mysql_databases:
            raise ValueError("At least one database and one privilege are required.")

        grants = []
        invalid_databases = []
        missing_privilege_dbs = []
        invalid_privileges = {}

        for db_grant in mysql_databases:
            database = db_grant.get("database")
            tables = db_grant.get("tables", "*")
            raw_privileges = db_grant.get("privileges") or []
            privileges = [str(p).strip().upper() for p in raw_privileges if str(p).strip()]

            # Validate database exists on this server
            if database not in available_databases:
                invalid_databases.append(database)
                continue

            if not privileges:
                missing_privilege_dbs.append(database or "unknown")
                continue
            invalid_privs = sorted({p for p in privileges if p not in MYSQL_PRIVILEGES})
            if invalid_privs:
                invalid_privileges[database or "unknown"] = invalid_privs
                continue

            # Map "*" → "all" for script generator compatibility
            table_value = "all" if tables == "*" else tables

            # Structure in format expected by script generator:
            # {database, tables: [{table, privileges}]}
            grants.append({
                "database": database,
                "tables": [
                    {
                        "table": table_value,
                        "privileges": privileges
                    }
                ]
            })

        # Raise error if any databases not found
        if invalid_databases:
            available_list = ", ".join(available_databases[:10])  # Show first 10
            if len(available_databases) > 10:
                available_list += f" ... and {len(available_databases) - 10} more"

            raise ValueError(
                f"Database(s) not found on server '{server_name}': {', '.join(invalid_databases)}. "
                f"Available databases on this server: {available_list}"
            )

        if invalid_privileges:
            details = "; ".join(
                f"{db}: {', '.join(privs)}" for db, privs in invalid_privileges.items()
            )
            allowed_list = ", ".join(sorted(MYSQL_PRIVILEGES))
            raise ValueError(
                f"Invalid MySQL privilege(s): {details}. Allowed values: {allowed_list}"
            )

        if missing_privilege_dbs:
            missing_list = ", ".join(sorted(set(missing_privilege_dbs)))
            raise ValueError(
                f"At least one privilege is required for database(s): {missing_list}."
            )

        if not grants:
            raise ValueError("At least one database and one privilege are required.")

        logger.info(f"Structured {len(grants)} MySQL grant(s)")
        return grants

    async def _structure_pgsql_grants(
        self,
        pg_databases: List[Dict[str, Any]],
        available_databases: List[str],
        server_name: str,
    ) -> List[Dict[str, Any]]:
        """
        Structure PostgreSQL grants and validate databases.

        Args:
            pg_databases: List of database configs with database, database_permissions, schema_grants
            available_databases: Databases available on this server
            server_name: Server name for error messages

        Returns:
            List of structured grants in backend format

        Raises:
            ValueError: If a database is not found on this server
        """
        if not pg_databases:
            raise ValueError("At least one database and one privilege are required.")

        grants = []
        invalid_databases = []
        missing_privilege_dbs = []
        invalid_privilege_errors = []

        for db_config in pg_databases:
            database = db_config.get("database")
            raw_database_permissions = db_config.get("database_permissions", [])
            database_permissions = [
                str(p).strip().upper()
                for p in raw_database_permissions
                if str(p).strip()
            ]
            schema_grants = db_config.get("schema_grants", [])

            # Validate database exists on this server
            if database not in available_databases:
                invalid_databases.append(database)
                continue

            invalid_db_perms = sorted({p for p in database_permissions if p not in PG_DATABASE_PRIVILEGES})
            if invalid_db_perms:
                invalid_privilege_errors.append(
                    f"{database}: database_permissions invalid: {', '.join(invalid_db_perms)}"
                )
            valid_database_permissions = [p for p in database_permissions if p in PG_DATABASE_PRIVILEGES]
            has_db_perms = bool(valid_database_permissions)

            # Process schema grants for this database and group by schema_name
            schema_grants_by_name = {}  # {schema_name: {"permissions": [], "tables": []}}
            has_schema_perms = False
            has_table_perms = False
            invalid_schema_privs = set()
            invalid_table_privs = set()
            invalid_object_types = set()
            if schema_grants:
                for schema_grant in schema_grants:
                    schema_name = schema_grant.get("schema", "public")
                    object_type = str(schema_grant.get("object_type", "table")).strip().lower()
                    raw_privileges = schema_grant.get("privileges", [])
                    privileges = [
                        str(p).strip().upper()
                        for p in raw_privileges
                        if str(p).strip()
                    ]

                    if not privileges:
                        continue

                    if object_type == "table":
                        invalid = {p for p in privileges if p not in PG_TABLE_PRIVILEGES}
                        if invalid:
                            invalid_table_privs.update(invalid)
                            continue
                        has_table_perms = True
                        # Table-level grants - merge with existing schema entry
                        if schema_name not in schema_grants_by_name:
                            schema_grants_by_name[schema_name] = {
                                "permissions": [],
                                "tables": []
                            }
                        schema_grants_by_name[schema_name]["tables"].extend([{
                            "tableName": "all",
                            "privileges": privileges
                        }])
                    elif object_type == "schema":
                        invalid = {p for p in privileges if p not in PG_SCHEMA_PRIVILEGES}
                        if invalid:
                            invalid_schema_privs.update(invalid)
                            continue
                        has_schema_perms = True
                        # Schema-level grants - merge with existing schema entry
                        if schema_name not in schema_grants_by_name:
                            schema_grants_by_name[schema_name] = {
                                "permissions": [],
                                "tables": []
                            }
                        schema_grants_by_name[schema_name]["permissions"].extend(privileges)
                    else:
                        invalid_object_types.add(object_type or "unknown")

            if invalid_schema_privs:
                invalid_privilege_errors.append(
                    f"{database}: schema privileges invalid: {', '.join(sorted(invalid_schema_privs))}"
                )
            if invalid_table_privs:
                invalid_privilege_errors.append(
                    f"{database}: table privileges invalid: {', '.join(sorted(invalid_table_privs))}"
                )
            if invalid_object_types:
                invalid_privilege_errors.append(
                    f"{database}: invalid object_type(s): {', '.join(sorted(invalid_object_types))}"
                )

            if not has_db_perms and not has_schema_perms and not has_table_perms:
                missing_privilege_dbs.append(database or "unknown")
                continue

            # Build schemas_list from grouped grants
            schemas_list = []
            for schema_name, schema_data in schema_grants_by_name.items():
                schemas_list.append({
                    "schemaName": schema_name,
                    "permissions": schema_data["permissions"],
                    "tables": schema_data["tables"]
                })
            if not schemas_list:
                # Explicit empty schema/table grants to preserve shape in config snapshot
                schemas_list = [{
                    "schemaName": "public",
                    "permissions": [],
                    "tables": [{
                        "tableName": "all",
                        "privileges": []
                    }]
                }]

            # Create grant entry for this database
            grants.append({
                "database": database,
                "permissions": valid_database_permissions,
                "schemas": schemas_list
            })

        # Raise error if any databases not found
        if invalid_databases:
            available_list = ", ".join(available_databases[:10])
            if len(available_databases) > 10:
                available_list += f" ... and {len(available_databases) - 10} more"

            raise ValueError(
                f"Database(s) not found on server '{server_name}': {', '.join(invalid_databases)}. "
                f"Available databases on this server: {available_list}"
            )

        if invalid_privilege_errors:
            allowed_db = ", ".join(sorted(PG_DATABASE_PRIVILEGES))
            allowed_schema = ", ".join(sorted(PG_SCHEMA_PRIVILEGES))
            allowed_table = ", ".join(sorted(PG_TABLE_PRIVILEGES))
            raise ValueError(
                "Invalid PostgreSQL privilege(s): "
                f"{'; '.join(invalid_privilege_errors)}. "
                f"Allowed database_permissions: {allowed_db}. "
                f"Allowed schema privileges: {allowed_schema}. "
                f"Allowed table privileges: {allowed_table}."
            )

        if missing_privilege_dbs:
            missing_list = ", ".join(sorted(set(missing_privilege_dbs)))
            raise ValueError(
                f"At least one privilege is required for database(s): {missing_list}."
            )

        if not grants:
            raise ValueError("At least one database and one privilege are required.")

        logger.info(f"Structured PostgreSQL grants: {len(grants)} database(s)")

        return grants

    async def structure_mysql_grants(
        self,
        server_name: str,
        username: str,
        password: str,
        available_databases: List[str],
        mysql_databases: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Structure MySQL database user grants (returns structured data, no is_ready).

        Args:
            server_name: Server name
            username: Username
            password: Password
            available_databases: Databases available on this server
            mysql_databases: MySQL grants to structure

        Returns:
            Dict with structured grants (for intermediate step, not final)
        """
        logger.info(
            "DATABASE USER MANAGEMENT - structure_mysql_grants",
            extra={
                "server_name": server_name,
                "username": username,
            }
        )

        grants = await self._structure_mysql_grants(
            mysql_databases, available_databases, server_name
        )

        return {
            "username": username,
            "password": password,
            "server_name": server_name,
            "db_type": "mysql",
            "grants": grants,
        }

    async def structure_psql_grants(
        self,
        server_name: str,
        username: str,
        password: str,
        available_databases: List[str],
        pg_databases: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Structure PostgreSQL database user grants (returns structured data, no is_ready).

        Args:
            server_name: Server name
            username: Username
            password: Password
            available_databases: Databases available on this server
            pg_databases: List of database configs with database_permissions and schema_grants

        Returns:
            Dict with structured grants (for intermediate step, not final)
        """
        logger.info(
            "DATABASE USER MANAGEMENT - structure_psql_grants",
            extra={
                "server_name": server_name,
                "username": username,
                "pg_databases_count": len(pg_databases),
            }
        )

        grants = await self._structure_pgsql_grants(
            pg_databases, available_databases, server_name
        )

        return {
            "username": username,
            "password": password,
            "server_name": server_name,
            "db_type": "postgresql",
            "grants": grants,
        }

    async def finalize_database_user_creation(
        self,
        server_name: str,
        username: str,
        password: str,
        db_type: str,
        structured_grants: Dict[str, Any],
        tenant_code: Optional[str] = None,
        product_name: Optional[str] = None,
        environment: Optional[Environment] = None,
        geo_loc_code: Optional[str] = None,
        infra_vendor: str = "aws",
    ) -> Dict[str, Any]:
        """
        Finalize database user creation (sets is_ready=true).

        Args:
            server_name: Server name
            username: Username
            password: Password
            db_type: Database type ('mysql' or 'postgresql')
            structured_grants: Structured grants from previous tool calls
            tenant_code: Tenant identifier
            product_name: Product name
            environment: Environment enum
            geo_loc_code: Geographic location code
            infra_vendor: Infrastructure vendor (default: 'aws')

        Returns:
            Dict with attribute_parameters, placement_parameters, is_ready=true
        """
        logger.info(
            "DATABASE USER MANAGEMENT - finalize_database_user_creation",
            extra={
                "server_name": server_name,
                "db_type": db_type,
                "username": username,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value if environment else None,
            }
        )

        # Build attribute_parameters from structured_grants
        # Wrap grants in the format expected by backend (matching frontend logic)
        grants_data = structured_grants.get("grants", {})

        if db_type == "postgresql":
            # PostgreSQL format: pgsql_servers array with 3-level grant structure
            attribute_parameters = {
                "db_user_name": username,
                "db_password": password,
                "server_name": server_name,
                "db_type": db_type,
                "replace_grants": True,
                "pgsql_servers": [{
                    "db_server_name": server_name,
                    "grants": grants_data
                }]
            }
        elif db_type == "mysql":
            # MySQL format: mysql_servers array with 1-level grant structure
            attribute_parameters = {
                "db_user_name": username,
                "db_password": password,
                "server_name": server_name,
                "db_type": db_type,
                "replace_grants": True,
                "mysql_servers": [{
                    "db_server_name": server_name,
                    "grants": grants_data
                }]
            }
        else:
            raise ValueError(f"Invalid db_type: {db_type}. Must be 'mysql' or 'postgresql'")

        # Resolve product_name → applications_mst_code (UUID)
        from app.infra_chat_agent.config.placement_utils import resolve_product_application_code
        applications_mst_code = None
        resolved_product_name = product_name
        if tenant_code and product_name:
            app_code, product_label = resolve_product_application_code(
                tenant_code, "DatabaseUserManagement", product_name
            )
            if app_code:
                applications_mst_code = app_code
                resolved_product_name = product_label or product_name

        # Build placement_parameters
        # case_type_ref_code is critical for the file locator to route to the
        # correct handler (user_management vs database_creation)
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "applications_mst_code": applications_mst_code or product_name,
            "environment_enum": environment.value if environment else None,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "database",
            "case_ref_code": "user_management",
        }

        return {
            "status": "success",
            "message": f"Database user '{username}' creation request finalized for {db_type} server '{server_name}'",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
            "queue_status": "draft",
        }

    # ========================================================================
    # GitHub / Terragrunt Helper Methods
    # ========================================================================

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.db)

    def _get_region_for_environment(self, environment: str) -> str:
        """Get the region for a given environment from settings."""
        env_lower = environment.lower()
        if env_lower == "dev":
            return settings.infra_region_dev
        elif env_lower == "stage":
            return settings.infra_region_staging
        elif env_lower == "qa":
            return settings.infra_region_qa
        elif env_lower == "prod":
            return settings.infra_region_prod
        else:
            return settings.infra_region_dev

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
        """
        Map business/deployment region (geo_loc) to AWS region.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'london')

        Returns:
            AWS region code (e.g., 'ap-south-1', 'eu-west-2')
        """
        mapping = {
            "mumbai": "ap-south-1",
            "london": "eu-west-2",
            "uk": "eu-west-2",
            "us": "us-east-1",
            "aspora-mumbai": "ap-south-1",
            "aspora-london": "eu-west-2",
            "aspora-uk": "eu-west-2",
            "aspora-us": "us-east-1",
            "region-aspora-mumbai": "ap-south-1",
            "region-aspora-london": "eu-west-2",
            "region-aspora-us": "us-east-1",
        }
        return mapping.get(geo_loc.lower(), "ap-south-1")

    @staticmethod
    def _get_folder_env(environment: str, tenant: str = "") -> str:
        """
        Get the environment folder name for file paths.

        Default: dev→dev, staging→staging, qa→qa, prod→prod
        Vance/Aspora: dev→stage, staging→stage, qa→qa, prod→prod

        Args:
            environment: Environment name (dev, staging, qa, prod)
            tenant: Tenant identifier (e.g., 'vance', 'aspora')

        Returns:
            Folder environment name for path construction
        """
        env_lower = environment.strip().lower()
        tenant_lower = tenant.lower() if tenant else ""

        # Vance/Aspora tenants
        if tenant_lower in ("vance", "aspora"):
            if env_lower in ("dev", "staging", "stage"):
                return "stage"
            if env_lower == "qa":
                return "qa"
            return "prod"

        # Default tenants
        if env_lower == "staging":
            return "stage"
        return env_lower  # dev, qa, prod

    def _extract_balanced_brackets(self, content: str, start_pos: int, open_char: str = '[', close_char: str = ']') -> str:
        """
        Extract content within balanced brackets starting at start_pos.

        Args:
            content: Full string content
            start_pos: Position of the opening bracket
            open_char: Opening bracket character (default '[')
            close_char: Closing bracket character (default ']')

        Returns:
            Content inside the balanced brackets (excluding the brackets themselves)
        """
        if start_pos >= len(content) or content[start_pos] != open_char:
            return ""

        depth = 1
        i = start_pos + 1
        while i < len(content) and depth > 0:
            if content[i] == open_char:
                depth += 1
            elif content[i] == close_char:
                depth -= 1
            i += 1

        return content[start_pos + 1:i - 1]

    def _parse_mysql_users_from_hcl(self, hcl_content: str) -> List[Dict[str, Any]]:
        """
        Parse mysql_users array from HCL content.

        Args:
            hcl_content: Raw HCL file content

        Returns:
            List of user dictionaries with username, password, and grants
        """
        users = []

        # Find mysql_users array start
        match = re.search(r'mysql_users\s*=\s*\[', hcl_content)
        if not match:
            logger.debug("No mysql_users array found in HCL content")
            return users

        # Find the opening bracket position and extract balanced content
        bracket_pos = hcl_content.find('[', match.start())
        users_block = self._extract_balanced_brackets(hcl_content, bracket_pos)

        if not users_block:
            logger.debug("Empty mysql_users array")
            return users

        # Extract individual user blocks using balanced brace matching
        i = 0
        while i < len(users_block):
            if users_block[i] == '{':
                user_block = self._extract_balanced_brackets(users_block, i, '{', '}')
                if user_block:
                    user = self._parse_user_block(user_block)
                    if user and user.get("username"):
                        users.append(user)
                    i += len(user_block) + 2  # Skip past closing brace
                else:
                    i += 1
            else:
                i += 1

        logger.info(f"Parsed {len(users)} MySQL users from HCL")
        return users

    def _parse_psql_users_from_hcl(self, hcl_content: str) -> List[Dict[str, Any]]:
        """
        Parse psql_users array from HCL content.

        Args:
            hcl_content: Raw HCL file content

        Returns:
            List of user dictionaries with username, password, and grants
        """
        users = []

        # Find psql_users array start
        match = re.search(r'psql_users\s*=\s*\[', hcl_content)
        if not match:
            logger.debug("No psql_users array found in HCL content")
            return users

        # Find the opening bracket position and extract balanced content
        bracket_pos = hcl_content.find('[', match.start())
        users_block = self._extract_balanced_brackets(hcl_content, bracket_pos)

        if not users_block:
            logger.debug("Empty psql_users array")
            return users

        # Extract individual user blocks using balanced brace matching
        i = 0
        while i < len(users_block):
            if users_block[i] == '{':
                user_block = self._extract_balanced_brackets(users_block, i, '{', '}')
                if user_block:
                    user = self._parse_user_block(user_block)
                    if user and user.get("username"):
                        users.append(user)
                    i += len(user_block) + 2  # Skip past closing brace
                else:
                    i += 1
            else:
                i += 1

        logger.info(f"Parsed {len(users)} PostgreSQL users from HCL")
        return users

    def _parse_user_block(self, user_block: str) -> Dict[str, Any]:
        """
        Parse a single user block from HCL.

        Args:
            user_block: HCL content for a single user object

        Returns:
            Dictionary with username, password, and grants
        """
        user = {
            "username": "",
            "password": "",
            "grants": []
        }

        # Extract username (support both 'name' and 'username' field names)
        username_match = re.search(r'(?:username|name)\s*=\s*"([^"]*)"', user_block)
        if username_match:
            user["username"] = username_match.group(1)

        # Extract password
        password_match = re.search(r'password\s*=\s*"([^"]*)"', user_block)
        if password_match:
            user["password"] = password_match.group(1)

        # Extract grants array using balanced bracket matching
        grants_match = re.search(r'grants\s*=\s*\[', user_block)
        if grants_match:
            bracket_pos = user_block.find('[', grants_match.start())
            grants_block = self._extract_balanced_brackets(user_block, bracket_pos)

            if grants_block:
                # Extract individual grant blocks {...} and parse them
                i = 0
                while i < len(grants_block):
                    if grants_block[i] == '{':
                        grant_content = self._extract_balanced_brackets(grants_block, i, '{', '}')
                        if grant_content:
                            # Parse the grant block into a structured dict
                            grant = self._parse_grant_block(grant_content)
                            if grant.get("database"):
                                user["grants"].append(grant)
                            i += len(grant_content) + 2
                        else:
                            i += 1
                    else:
                        i += 1

        return user

    def _parse_grant_block(self, grant_block: str) -> Dict[str, Any]:
        """
        Parse a single grant block from HCL.

        Supports both MySQL and PostgreSQL formats.

        Args:
            grant_block: HCL content for a single grant object

        Returns:
            Dictionary with grant fields (database, table/schema, object_type, privileges)
        """
        grant = {}

        # Extract database
        db_match = re.search(r'database\s*=\s*"([^"]*)"', grant_block)
        if db_match:
            grant["database"] = db_match.group(1)

        # Extract table (MySQL)
        table_match = re.search(r'table\s*=\s*"([^"]*)"', grant_block)
        if table_match:
            grant["table"] = table_match.group(1)

        # Extract schema (PostgreSQL)
        schema_match = re.search(r'schema\s*=\s*"([^"]*)"', grant_block)
        if schema_match:
            grant["schema"] = schema_match.group(1)

        # Extract object_type (PostgreSQL)
        object_type_match = re.search(r'object_type\s*=\s*"([^"]*)"', grant_block)
        if object_type_match:
            grant["object_type"] = object_type_match.group(1)

        # Extract privileges array using balanced bracket matching
        priv_match = re.search(r'privileges\s*=\s*\[', grant_block)
        if priv_match:
            bracket_pos = grant_block.find('[', priv_match.start())
            priv_content = self._extract_balanced_brackets(grant_block, bracket_pos)
            if priv_content:
                # Extract individual privileges
                privileges = re.findall(r'"([^"]*)"', priv_content)
                grant["privileges"] = privileges
            else:
                grant["privileges"] = []
        else:
            grant["privileges"] = []

        return grant

    async def _try_parse_separate_users_file(
        self,
        token: str,
        owner: str,
        repo: str,
        subdir_path: str,
        branch_name: str,
        filename: str,
        parser_fn,
    ) -> tuple:
        """
        Try to fetch and parse a separate users file (e.g. psql_users.hcl or
        mysql_users.hcl) from the same directory.  Used as a fallback when the
        inline users array in terragrunt.hcl is empty.

        Returns:
            (users_list, was_parsed) — users_list may be [] if the file does
            not exist or parsing yields nothing; was_parsed is True when a file
            was successfully fetched and parsed.
        """
        file_path = f"{subdir_path}/{filename}"
        try:
            result = await GitHubIntegration.get_file_content(
                token=token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                file_path=file_path,
                branch=branch_name,
            )
            if result and result.get("exists"):
                users = parser_fn(result.get("content", ""))
                if users:
                    logger.info(f"Loaded {len(users)} user(s) from separate file {file_path}")
                    return users, True
        except Exception as e:
            logger.debug(f"Separate users file not available at {file_path}: {e}")
        return [], False

    async def get_database_users(
        self,
        product_name: str,
        environment: str,
        github_repository: str,
        branch_name: str,
        region: Optional[str] = None,
        tenant: Optional[str] = None,
        geo_loc: Optional[str] = None,
        server_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch database users from terragrunt.hcl files in the database directory.

        This method:
        1. Builds the path: environment/{product}-{env}-{version}/{region}/database/
        2. Lists all subdirectories inside the database folder
        3. If server_name is provided, only fetches from that specific server subdirectory
        4. Parses mysql_users or psql_users arrays from each file
        5. Returns user information (filtered by server_name if provided)

        Args:
            product_name: Product name (e.g., "genorim")
            environment: Environment name (dev, staging, prod)
            github_repository: GitHub repository in 'owner/repo' format
            branch_name: Target branch name
            region: Optional AWS region (auto-detected from environment if not provided)
            tenant: Optional tenant identifier (e.g., 'vance', 'aspora') for tenant-specific path logic
            geo_loc: Optional geographic location code (e.g., 'mumbai', 'london') for region mapping
            server_name: Optional server name to filter results (e.g., 'common-pg', 'common-mysql-1')

        Returns:
            Dict with:
            - success: Boolean
            - users: List of {username, password, database_type, server}
            - full_details: List of detailed user info with passwords and grants
            - servers: List of all server names scanned
            - selected_server: The server_name filter (if provided)
            - metadata: directories_scanned, files_parsed, etc.
            - message: Result message
        """
        # Validate inputs
        if not github_repository or '/' not in github_repository:
            raise ValueError("Invalid repository format, expected 'owner/repo'")

        if not branch_name or not branch_name.strip():
            raise ValueError("Branch name is required")

        if not environment or not environment.strip():
            raise ValueError("Environment is required")

        # Parse repository
        owner, repo = github_repository.split('/')

        # Determine region: prefer geo_loc mapping, fallback to environment-based
        if not region:
            if geo_loc:
                region = self._get_aws_region_from_geo_loc(geo_loc)
                logger.info(f"Using geo_loc-based region: {geo_loc} → {region}")
            else:
                region = self._get_region_for_environment(environment)
                logger.info(f"Using environment-based region fallback: {environment} → {region}")

        version_index = settings.infra_version_index

        # Sanitize/normalize path components using tenant-aware helper
        product_name_sanitized = re.sub(r'[\s_-]+', '-', product_name.strip()).strip('-').lower()
        env_sanitized = self._get_folder_env(environment, tenant)
        region_sanitized = region.lower().replace('_', '-')

        # Build base path to database directory
        # Format: environment/{product}-{env}-{version}/{region}/database
        database_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database"

        logger.info(f"Fetching database users from: {database_path}")
        logger.info(f"Repository: {github_repository}, Branch: {branch_name}")

        # Get token for API calls
        token = await self._get_github_token(owner)

        # List all subdirectories in the database folder
        try:
            directory_contents = await GitHubIntegration.list_directory_contents(
                token=token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                path=database_path,
                branch=branch_name
            )
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                return {
                    "success": True,
                    "users": [],
                    "full_details": [],
                    "servers": [],
                    "metadata": {
                        "directories_scanned": 0,
                        "files_parsed": 0,
                        "base_path": database_path
                    },
                    "message": f"Database directory not found: {database_path}"
                }
            raise

        # Filter to get only directories
        subdirectories = [
            item for item in directory_contents
            if item.get("type") == "dir"
        ]

        # Extract server names (subdirectory names)
        servers = [item.get("name") for item in subdirectories]

        if not subdirectories:
            return {
                "success": True,
                "users": [],
                "full_details": [],
                "servers": [],
                "metadata": {
                    "directories_scanned": 0,
                    "files_parsed": 0,
                    "base_path": database_path
                },
                "message": f"No subdirectories found in {database_path}"
            }

        # Collect users from all subdirectories
        all_users_summary = []
        all_users_details = []
        files_parsed = 0

        for subdir in subdirectories:
            subdir_name = subdir.get("name")
            subdir_path = subdir.get("path")
            terragrunt_file_path = f"{subdir_path}/terragrunt.hcl"

            logger.info(f"Checking directory: {subdir_name}")

            # Fetch terragrunt.hcl from this subdirectory
            try:
                file_result = await GitHubIntegration.get_file_content(
                    token=token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=terragrunt_file_path,
                    branch=branch_name
                )

                if not file_result or not file_result.get("exists"):
                    logger.info(f"No terragrunt.hcl found in {subdir_name}")
                    continue

                hcl_content = file_result.get("content", "")
                files_parsed += 1

                # Try to parse MySQL users (inline in terragrunt.hcl first,
                # then fall back to separate mysql_users.hcl)
                mysql_users = self._parse_mysql_users_from_hcl(hcl_content)
                if not mysql_users:
                    mysql_users, parsed = await self._try_parse_separate_users_file(
                        token, owner, repo, subdir_path, branch_name,
                        "mysql_users.hcl", self._parse_mysql_users_from_hcl
                    )
                    if parsed:
                        files_parsed += 1

                for user in mysql_users:
                    all_users_summary.append({
                        "username": user.get("username"),
                        "password": user.get("password"),
                        "database_type": "mysql",
                        "server": subdir_name
                    })
                    if not server_name or subdir_name == server_name:
                        all_users_details.append({
                            "username": user.get("username"),
                            "password": user.get("password"),
                            "database_type": "mysql",
                            "source_directory": subdir_name,
                            "grants": user.get("grants", [])
                        })

                # Try to parse PostgreSQL users (inline in terragrunt.hcl first,
                # then fall back to separate psql_users.hcl)
                psql_users = self._parse_psql_users_from_hcl(hcl_content)
                if not psql_users:
                    psql_users, parsed = await self._try_parse_separate_users_file(
                        token, owner, repo, subdir_path, branch_name,
                        "psql_users.hcl", self._parse_psql_users_from_hcl
                    )
                    if parsed:
                        files_parsed += 1

                for user in psql_users:
                    all_users_summary.append({
                        "username": user.get("username"),
                        "password": user.get("password"),
                        "database_type": "postgresql",
                        "server": subdir_name
                    })
                    if not server_name or subdir_name == server_name:
                        all_users_details.append({
                            "username": user.get("username"),
                            "password": user.get("password"),
                            "database_type": "postgresql",
                            "source_directory": subdir_name,
                            "grants": user.get("grants", [])
                        })

            except Exception as e:
                logger.warning(f"Failed to fetch/parse terragrunt.hcl from {subdir_name}: {e}")
                continue

        user_count = len(all_users_summary)
        message = f"Found {user_count} database user(s) across {files_parsed} file(s)"

        logger.info(message)

        return {
            "success": True,
            "users": all_users_summary,
            "full_details": all_users_details,
            "servers": servers,
            "metadata": {
                "directories_scanned": len(subdirectories),
                "files_parsed": files_parsed,
                "base_path": database_path,
                "product": product_name,
                "environment": environment,
                "region": region_sanitized
            },
            "message": message
        }
