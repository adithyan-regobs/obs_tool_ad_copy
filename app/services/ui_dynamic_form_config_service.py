"""
UI Dynamic Form Config Service

Service for reading and returning UI form configuration data.
Also handles fetching database users from GitHub terragrunt files.
"""

import json
import os
import re
import logging
from typing import Dict, List, Any, Optional, Union
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.integrations.github_integration import GitHubIntegration
from app.services.terragrunt_mgmt_service import TerragruntMgmtService
logger = logging.getLogger(__name__)


class UIDynamicFormConfigService:
    """Service for providing UI form configuration data."""

    def __init__(self,session):
        self.db = session
        self.application_mst_repo=ApplicationsMstRepository(session)
        self.config_path = os.path.join(
            os.path.dirname(__file__),
            "../../aspora/database_user_config.json"
        )

    async def get_database_list(
        self,
        type_filter: Optional[str] = None,
        product: Optional[str] = None
    ):
        """
        Read database configuration and return filtered or full database info.

        Args:
            type_filter: Filter by database type
                - "mysql_database"
                - "postgresql_database"
            product: Product/application name to filter by (e.g., "core", "falcon")
                     This is the application NAME (resolved from code in the endpoint)

        Returns:
            If type_filter provided → ONLY list (array)
            Else → Full dict with mysqldb & psqldb
            Returns empty array/dict if product not found or not provided
        """
        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)

            # Check if product is provided before querying database
            if not product:
                return []

            application = await self.application_mst_repo.get_by_code(product)
            if not application:
                return []

            product = application.name
            # Get product data - returns empty dict if product is None or not found
            product_data = config.get(product, {}) if product else {}

            mysql_list = product_data.get("mysqldb", [])
            psql_list = product_data.get("psqldb", [])

            # If filter present → return ONLY list without wrapping dict
            if type_filter:
                if type_filter == "mysql_database":
                    return mysql_list
                elif type_filter == "postgresql_database":
                    return psql_list
                else:
                    return []  # Invalid type → empty array

            # Default: return full structure
            return {
                "mysqldb": mysql_list,
                "psqldb": psql_list
            }

        except FileNotFoundError:
            logger.error(f"Database config file not found: {self.config_path}")
            return {"mysqldb": [], "psqldb": []}

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in database config: {e}")
            return {"mysqldb": [], "psqldb": []}

        except Exception as e:
            logger.error(f"Unexpected error reading database config: {e}")
            return {"mysqldb": [], "psqldb": []}

    # ============================================================
    # Database Users Methods - HCL Parsing for MySQL/PostgreSQL
    # ============================================================

    async def _get_github_token(self, owner: str) -> str:
        """Get GitHub App installation token for the given org."""
        from app.utils.github_app_token import get_token_for_org
        return await get_token_for_org(owner, self.db)

    def _get_region_for_environment(self, environment: str) -> str:
        """Get the region for a given environment from settings."""
        env_lower = environment.lower()
        if env_lower == "dev":
            return settings.infra_region_dev
        elif env_lower == "staging":
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

        Expected HCL format:
        mysql_users = [
          {
            username       = "appuser"
            password       = "SecurePass123!"
            grants = [
              {
                database   = "mydb"
                privileges = ["SELECT", "INSERT", "UPDATE"]
              }
            ]
          }
        ]

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

        Expected HCL format:
        psql_users = [
          {
            username       = "pguser"
            password       = "PgSecurePass!"
            grants = [
              {
                database   = "analytics"
                privileges = ["SELECT", "INSERT"]
              }
            ]
          }
        ]

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

        Supports both MySQL format:
            database   = "appserver_noref"
            table      = "*"
            privileges = ["SELECT", "INSERT", "UPDATE"]

        And PostgreSQL format:
            database    = "recon_service"
            schema      = "public"
            object_type = "database"
            privileges  = ["CREATE"]

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
        geo_loc: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch all database users from terragrunt.hcl files in the database directory.

        This method:
        1. Builds the path: environment/{product}-{env}-{version}/{region}/database/
        2. Lists all subdirectories inside the database folder
        3. Fetches terragrunt.hcl from each subdirectory
        4. Parses mysql_users or psql_users arrays from each file
        5. Returns aggregated user information

        Args:
            product_name: Product name (e.g., "genorim")
            environment: Environment name (dev, staging, prod)
            github_repository: GitHub repository in 'owner/repo' format
            branch_name: Target branch name
            region: Optional AWS region (auto-detected from environment if not provided)
            tenant: Optional tenant identifier (e.g., 'vance', 'aspora') for tenant-specific path logic
            geo_loc: Optional geographic location code (e.g., 'mumbai', 'london') for region mapping

        Returns:
            Dict with:
            - success: Boolean
            - users: List of {username, database_type}
            - full_details: List of detailed user info with passwords and grants
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
        region_sanitized = TerragruntMgmtService._normalize_region_for_path(region)

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

    async def get_servers_list(
        self,
        product_name: str,
        environment: str,
        github_repository: str,
        branch_name: str,
        region: Optional[str] = None,
        tenant: Optional[str] = None,
        geo_loc: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of database server directories from the database folder.

        Args:
            product_name: Product name (e.g., "core")
            environment: Environment name (dev, staging, prod)
            github_repository: GitHub repository in 'owner/repo' format
            branch_name: Target branch name
            region: Optional AWS region (auto-detected from environment if not provided)
            tenant: Optional tenant identifier for tenant-specific path logic
            geo_loc: Optional geographic location code for region mapping

        Returns:
            Dict with success, servers (list of dir names), metadata, message
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

        # Determine region
        if not region:
            if geo_loc:
                region = self._get_aws_region_from_geo_loc(geo_loc)
            else:
                region = self._get_region_for_environment(environment)

        version_index = settings.infra_version_index

        # Sanitize/normalize path components
        product_name_sanitized = re.sub(r'[\s_-]+', '-', product_name.strip()).strip('-').lower()
        env_sanitized = self._get_folder_env(environment, tenant)
        region_sanitized = TerragruntMgmtService._normalize_region_for_path(region)

        # Build base path to database directory
        database_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database"

        logger.info(f"Fetching servers list from: {database_path}")
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
                    "servers": [],
                    "metadata": {
                        "base_path": database_path,
                        "server_count": 0
                    },
                    "message": f"Database directory not found: {database_path}"
                }
            raise

        # Filter to get only directories
        subdirectories = [
            item for item in directory_contents
            if item.get("type") == "dir"
        ]

        # Extract server names and detect database types
        servers = []
        for item in subdirectories:
            server_name = item.get("name")
            db_type = "unknown"  # Default to "unknown" instead of None

            # Fetch terragrunt.hcl for this server to detect database type
            terragrunt_path = f"{database_path}/{server_name}/terragrunt.hcl"
            try:
                logger.info(f"Fetching terragrunt file for db type detection: {terragrunt_path}")
                file_result = await GitHubIntegration.get_file_content(
                    token=token,
                    base_url=settings.github_base_url,
                    owner=owner,
                    repo=repo,
                    file_path=terragrunt_path,
                    branch=branch_name
                )
                if file_result and file_result.get("content"):
                    parsed = self._parse_databases_from_hcl(file_result["content"])
                    detected_type = parsed.get("database_type")
                    if detected_type:
                        db_type = detected_type
                    logger.info(f"Detected db type for {server_name}: {db_type}")
                else:
                    logger.warning(f"Empty file content for {server_name}")
            except Exception as e:
                logger.warning(f"Could not detect db type for {server_name}: {str(e)}")

            servers.append({
                "name": server_name,
                "type": db_type
            })

        return {
            "success": True,
            "servers": servers,
            "metadata": {
                "base_path": database_path,
                "server_count": len(servers),
                "product": product_name,
                "environment": environment,
                "region": region_sanitized
            },
            "message": f"Found {len(servers)} database server(s)"
        }

    def _parse_databases_from_hcl(self, hcl_content: str) -> Dict[str, Any]:
        """
        Parse mysql_databases or psql_databases array from HCL content.

        Args:
            hcl_content: Content of terragrunt.hcl file

        Returns:
            Dict with:
            - databases: List of database names
            - database_type: "mysql" or "postgresql" or None if not found
        """
        # Pattern to match: mysql_databases = [...] or psql_databases = [...]
        # Use balanced bracket extraction for multiline arrays
        mysql_match = re.search(r'mysql_databases\s*=\s*\[', hcl_content)
        psql_match = re.search(r'psql_databases\s*=\s*\[', hcl_content)

        if mysql_match:
            start_pos = mysql_match.end() - 1  # Position of '['
            array_content = self._extract_balanced_brackets(hcl_content, start_pos, '[', ']')
            databases = re.findall(r'"([^"]+)"', array_content)
            return {"databases": databases, "database_type": "mysql"}

        if psql_match:
            start_pos = psql_match.end() - 1  # Position of '['
            array_content = self._extract_balanced_brackets(hcl_content, start_pos, '[', ']')
            databases = re.findall(r'"([^"]+)"', array_content)
            return {"databases": databases, "database_type": "postgresql"}

        return {"databases": [], "database_type": None}

    async def get_database_list_from_terragrunt(
        self,
        product_name: str,
        environment: str,
        github_repository: str,
        branch_name: str,
        server_name: str,
        region: Optional[str] = None,
        tenant: Optional[str] = None,
        geo_loc: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of databases from terragrunt.hcl for a specific server.

        Parses mysql_databases or psql_databases array from the HCL file.

        Args:
            product_name: Product name (e.g., "core")
            environment: Environment name (dev, staging, prod)
            github_repository: GitHub repository in 'owner/repo' format
            branch_name: Target branch name
            server_name: Server/subdirectory name (e.g., "common-mysql")
            region: Optional AWS region (auto-detected from environment if not provided)
            tenant: Optional tenant identifier for tenant-specific path logic
            geo_loc: Optional geographic location code for region mapping

        Returns:
            Dict with success, databases, database_type, server_name, metadata, message
        """
        # Validate inputs
        if not github_repository or '/' not in github_repository:
            raise ValueError("Invalid repository format, expected 'owner/repo'")

        if not branch_name or not branch_name.strip():
            raise ValueError("Branch name is required")

        if not environment or not environment.strip():
            raise ValueError("Environment is required")

        if not server_name or not server_name.strip():
            raise ValueError("Server name is required")

        # Parse repository
        owner, repo = github_repository.split('/')

        # Determine region
        if not region:
            if geo_loc:
                region = self._get_aws_region_from_geo_loc(geo_loc)
            else:
                region = self._get_region_for_environment(environment)

        version_index = settings.infra_version_index

        # Sanitize/normalize path components
        product_name_sanitized = re.sub(r'[\s_-]+', '-', product_name.strip()).strip('-').lower()
        env_sanitized = self._get_folder_env(environment, tenant)
        region_sanitized = TerragruntMgmtService._normalize_region_for_path(region)

        # Build path to terragrunt.hcl file
        terragrunt_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{server_name}/terragrunt.hcl"

        logger.info(f"Fetching database list from: {terragrunt_file_path}")
        logger.info(f"Repository: {github_repository}, Branch: {branch_name}")

        # Get token for API calls
        token = await self._get_github_token(owner)

        # Fetch terragrunt.hcl content
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
                return {
                    "success": True,
                    "databases": [],
                    "database_type": None,
                    "server_name": server_name,
                    "metadata": {
                        "file_path": terragrunt_file_path
                    },
                    "message": f"Terragrunt file not found: {terragrunt_file_path}"
                }

            hcl_content = file_result.get("content", "")

        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                return {
                    "success": True,
                    "databases": [],
                    "database_type": None,
                    "server_name": server_name,
                    "metadata": {
                        "file_path": terragrunt_file_path
                    },
                    "message": f"Terragrunt file not found: {terragrunt_file_path}"
                }
            raise

        # Parse databases from HCL content
        parse_result = self._parse_databases_from_hcl(hcl_content)
        databases = parse_result.get("databases", [])
        database_type = parse_result.get("database_type")

        if not databases:
            return {
                "success": True,
                "databases": [],
                "database_type": database_type,
                "server_name": server_name,
                "metadata": {
                    "file_path": terragrunt_file_path
                },
                "message": f"No databases found in {server_name}/terragrunt.hcl"
            }

        return {
            "success": True,
            "databases": databases,
            "database_type": database_type,
            "server_name": server_name,
            "metadata": {
                "file_path": terragrunt_file_path,
                "database_count": len(databases),
                "product": product_name,
                "environment": environment,
                "region": region_sanitized
            },
            "message": f"Found {len(databases)} database(s) in {server_name}"
        }
