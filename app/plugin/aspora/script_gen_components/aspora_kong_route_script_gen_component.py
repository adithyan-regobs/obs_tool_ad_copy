import logging
import json
import uuid
from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(
    workflow_context,
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message


class AsporaKongRouteScriptGenComponent:
    """
    Component responsible for generating Kong route configuration by adding routes to existing HCL content.
    This component focuses ONLY on HCL content manipulation - no database operations, GitHub operations, or validation.
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository  # GitopsQueueRepository for DB operations

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        """
        Generate Kong route configuration by adding route to existing HCL content.
        Optionally uploads both original and preview HCL to S3.

        Follows Single Responsibility Principle - handles HCL content manipulation
        and fetching existing content from GitHub.

        Returns:
            str: Modified HCL content with route added

        Raises:
            ValueError: If gateway config file doesn't exist
            ValueError: If API doesn't exist in content
            ValueError: If route already exists (duplicate check)
        """
        config_snapshot = queue_dict.get('config_snapshot') or {}

        # Extract required parameters
        # github_token = config_snapshot.get('github_token')
        # github_base_url = config_snapshot.get('github_base_url')
        api_name = config_snapshot.get('api_name')
        method = config_snapshot.get('method')
        route = config_snapshot.get('route')

        # Ensure api_name ends with -service
        if api_name and not api_name.endswith('-service'):
            api_name = f"{api_name}-service"

        self.logger.info(f"Generating Kong route configuration for: {file_location.file_path}")
        self.logger.info(f"  API Name: {api_name}")
        self.logger.info(f"  Method: {method}")
        self.logger.info(f"  Route: {route}")

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # Fetch existing file from GitHub
        self.logger.info(f"Checking if file exists on {feature_branch}")
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path
            )
        if cached_entry:
            existing_file = {
                "exists": True,
                "content": cached_entry.get("content")
            }
        else:
            existing_file = await fetch_existing_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_location.file_path,
                base_branch=base_branch,
                feature_branch=feature_branch,
                workflow_context=workflow_context,
                logger=self.logger,
                component_name=component_name,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        # Check if file exists
        if not existing_file["exists"]:
            raise ValueError(
                f"Kong gateway configuration file does not exist: {file_location.file_path}. "
                f"Routes can only be added to existing gateway configurations."
            )

        # Use existing content from GitHub
        existing_content = existing_file["content"]
        self.logger.info(f"File exists - using existing configuration")

        # Check if API exists in kong_configs
        api_pattern = f'"{api_name}"'
        api_exists = api_pattern in existing_content

        if api_exists:
            self.logger.info(f"API '{api_name}' found in configuration")

            # Check if route already exists
            self.logger.info(f"Checking if route already exists in gateway configuration file...")
            route_exists_in_file = self._check_route_exists_in_hcl_content(
                existing_content,
                api_name,
                method,
                route
            )

            if route_exists_in_file:
                error_msg = (
                    f"Route '{route}' already exists in {method} method for API '{api_name}' "
                    f"in gateway configuration."
                )
                self.logger.error(f"HCL duplicate detected: {error_msg}")
                raise ValueError(error_msg)

            self.logger.info("No duplicate found in gateway configuration file")
        else:
            self.logger.info(
                f"API '{api_name}' not found — will create a new service block"
            )

        gen_context = f"path={file_location.file_path} mode=update"
        with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
            if api_exists:
                # Add route to the bottom of the method array
                final_content = self._add_route_to_method_array(
                    existing_content,
                    api_name,
                    method,
                    route
                )
            else:
                # Create a new service block as the last entry in kong_configs
                final_content = self._add_new_service_block(
                    existing_content,
                    api_name,
                    method,
                    route,
                    file_location.file_path
                )

            # Generate preview HCL
            preview_hcl = self.preview_kong_hcl(
                api_name=api_name,
                method=method,
                route=route
            )
        self.logger.info(f"Generated preview HCL for {api_name}")

        # Upload to S3 if requested
        if upload_to_s3:
            # Generate unique identifier for S3 storage
            route_identifier = f"{api_name}-{method}-{uuid.uuid4().hex[:8]}"

            # Upload original HCL to S3
            try:
                original_s3_key = f"kong-routes/{route_identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=original_s3_key,
                    content=final_content,
                    content_type="text/plain"
                )
                self.logger.info(f"Uploaded original HCL to S3: {result['location']}")
            except Exception as e:
                self.logger.error(f"Failed to upload original HCL to S3: {str(e)}")
                raise

            # Upload preview HCL to S3 (non-blocking - failure shouldn't break original)
            try:
                preview_s3_key = f"preview/kong-routes/{route_identifier}.hcl"
                result = await FileManagerHandler.upload_file(
                    key=preview_s3_key,
                    content=preview_hcl,
                    content_type="text/plain",
                    metadata={
                        "type": "preview",
                        "api_name": api_name,
                        "method": method,
                        "route": route,
                        "generated_by": "aspora_kong_route_script_gen_component"
                    }
                )
                self.logger.info(f"Uploaded preview HCL to S3: {result['location']}")
            except Exception as e:
                # Non-blocking error - preview upload failure shouldn't break original
                self.logger.warning(f"Failed to upload preview HCL to S3: {str(e)}")

            # Build artifact_s3_key JSON for database (include both keys)
            artifact_s3_key_json = {
                "original_s3_key": original_s3_key,
                "preview": preview_s3_key
            }

            # Save artifact_s3_key to database via repository
            if repository and queue_dict.get("code"):
                await repository.update_artifact_s3_key(config_snapshot.get('code') or queue_dict.get("code"), json.dumps(artifact_s3_key_json))
                self.logger.info(
                    "Saved artifact_s3_key to database for queue item %s: %s",
                    config_snapshot.get('code') or queue_dict.get("code"),
                    json.dumps(artifact_s3_key_json)
                )

        if tenant and file_location and workflow_context:
            # Skip commit if in preview mode
            if not workflow_context.skip_commit:
                _upsert_staged_entry(
                    workflow_context=workflow_context,
                    repo=file_location.repo,
                    base_branch=base_branch,
                    feature_branch=file_location.feature_branch,
                    file_path=file_location.file_path,
                    content=final_content,
                    queue_id=queue_dict.get("id"),
                    script_gen_key=file_location.script_gen_key
                )
                queue_label = queue_dict.get("code") or queue_dict.get("id")
                if queue_label:
                    commit_line = f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                else:
                    commit_line = f"{file_location.script_gen_key} -> {file_location.file_path}"
                _append_commit_message(
                    workflow_context,
                    file_location.repo,
                    base_branch,
                    commit_line
                )
            #TODO: we need to update the git commit sha response in the workflow context here after commit

            if queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": final_content,
                    "preview_content": preview_hcl
                }

        return final_content

    @staticmethod
    def _find_matching_brace(content: str, open_pos: int) -> int:
        """Find the closing } that matches the opening { at open_pos."""
        depth = 0
        in_string = False
        for i in range(open_pos, len(content)):
            ch = content[i]
            if ch == '"' and (i == 0 or content[i - 1] != '\\'):
                in_string = not in_string
            elif not in_string:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return i
        return -1

    def _check_route_exists_in_hcl_content(
        self,
        content: str,
        api_name: str,
        method: str,
        route: str
    ) -> bool:
        """
        Check if a route already exists in the HCL file's method array for a specific API.

        Args:
            content: Full HCL file content
            api_name: API service name (e.g., "partner-dashboard-api")
            method: HTTP method (e.g., "GET", "POST")
            route: Kong route pattern (e.g., "~/api/v1/users$")

        Returns:
            True if route exists in the specified API's method array, False otherwise

        Example:
            If "user-api" has GET = ["~/api/v1/users$"], then:
            - check("user-api", "GET", "~/api/v1/users$") → True (duplicate)
            - check("user-api", "POST", "~/api/v1/users$") → False (different method, allowed)
            - check("product-api", "GET", "~/api/v1/users$") → False (different API, allowed)
        """
        try:
            # Step 1: Find the API block
            api_pattern = f'"{api_name}"'
            api_start = content.find(api_pattern)
            if api_start == -1:
                # API doesn't exist in content - route cannot be duplicate
                return False

            # Step 2: Find routes = { within the API block
            routes_search_start = content.find('{', api_start)
            routes_pattern = 'routes'
            routes_start = content.find(routes_pattern, routes_search_start)

            if routes_start == -1:
                # Routes section doesn't exist - route cannot be duplicate
                return False

            # Find the opening { for routes and its matching closing }
            routes_brace_start = content.find('{', routes_start)
            routes_brace_end = self._find_matching_brace(content, routes_brace_start)
            if routes_brace_end == -1:
                return False

            # Step 3: Find the method array ONLY within this API's routes block
            method_pattern = f'"{method}"'
            method_start = content.find(method_pattern, routes_brace_start, routes_brace_end)

            if method_start == -1:
                # Method doesn't exist in this API - route cannot be duplicate
                return False

            # Step 4: Find the array opening [ for this method
            array_start = content.find('[', method_start)
            if array_start == -1:
                return False

            # Step 5: Find the matching closing ] for this array
            bracket_depth = 0
            i = array_start
            array_end = -1
            in_string = False
            escape_next = False

            while i < len(content):
                char = content[i]

                # Handle escape sequences
                if escape_next:
                    escape_next = False
                    i += 1
                    continue

                if char == '\\':
                    escape_next = True
                    i += 1
                    continue

                # Handle string literals
                if char == '"' and not in_string:
                    in_string = True
                    i += 1
                    continue

                if char == '"' and in_string:
                    in_string = False
                    i += 1
                    continue

                # Only count brackets outside of strings
                if not in_string:
                    if char == '[':
                        bracket_depth += 1
                    elif char == ']':
                        bracket_depth -= 1
                        if bracket_depth == 0:
                            array_end = i
                            break

                i += 1

            if array_end == -1:
                return False

            # Step 6: Extract array content and check if route exists
            array_content = content[array_start + 1:array_end].strip()
            route_pattern = f'"{route}"'

            # Route exists if the exact pattern is found in the array content
            return route_pattern in array_content

        except Exception as e:
            # If parsing fails, log and return False (assume no duplicate)
            self.logger.warning(f"Failed to parse HCL content for duplicate check: {str(e)}")
            return False

    def _add_route_to_method_array(
        self,
        content: str,
        api_name: str,
        method: str,
        route: str
    ) -> str:
        """
        Add a route to the bottom of a method's array in gateway kong_configs.

        Args:
            content: Full HCL file content
            api_name: API service name (e.g., "partner-dashboard-api")
            method: HTTP method (e.g., "GET", "POST")
            route: Kong route pattern (e.g., "~/api/v1/users$")

        Returns:
            Modified content with route added to the method array

        Example:
            Before: "GET" = ["~/api/v1/users$"]
            After:  "GET" = ["~/api/v1/users$", "~/api/v1/products$"]
        """
        # Step 1: Find the API block
        api_pattern = f'"{api_name}"'
        api_start = content.find(api_pattern)
        if api_start == -1:
            raise ValueError(f"API '{api_name}' not found in content")

        # Step 2: Find routes = { within the API block
        # Start searching after the API name
        routes_search_start = content.find('{', api_start)
        routes_pattern = 'routes'
        routes_start = content.find(routes_pattern, routes_search_start)

        if routes_start == -1:
            raise ValueError(f"routes section not found for API '{api_name}'")

        # Find the opening { for routes and its matching closing }
        routes_brace_start = content.find('{', routes_start)
        routes_brace_end = self._find_matching_brace(content, routes_brace_start)
        if routes_brace_end == -1:
            raise ValueError(f"Could not find closing brace for routes block of API '{api_name}'")

        # Step 3: Find the method array ONLY within this API's routes block
        method_pattern = f'"{method}"'
        method_start = content.find(method_pattern, routes_brace_start, routes_brace_end)

        if method_start == -1:
            # Method doesn't exist — create it with the route as the first entry
            # Detect indentation from existing method lines inside routes block
            routes_block = content[routes_brace_start + 1:routes_brace_end]
            indent = "        "  # default 8 spaces
            for line in routes_block.split('\n'):
                stripped = line.lstrip()
                if stripped.startswith('"') and '=' in stripped:
                    indent = line[:len(line) - len(stripped)]
                    break

            # Trim trailing whitespace before closing } and add exactly one newline
            # Add comma after previous method's closing ] if one exists
            before_stripped = content[:routes_brace_end].rstrip()
            if before_stripped.endswith(']'):
                before = before_stripped + ',\n'
            else:
                before = before_stripped + '\n'

            new_method_line = f'{indent}"{method}" = ["{route}"]\n'
            modified_content = before + new_method_line + content[routes_brace_end:]
            self.logger.info(f"Created new method '{method}' with route '{route}' for {api_name}")
            return modified_content

        # Step 4: Find the array opening [ for this method
        array_start = content.find('[', method_start)
        if array_start == -1:
            raise ValueError(f"Array not found for method '{method}'")

        # Step 5: Find the matching closing ] for this array
        # Need to handle nested brackets
        bracket_depth = 0
        i = array_start
        array_end = -1
        in_string = False
        escape_next = False

        while i < len(content):
            char = content[i]

            # Handle escape sequences
            if escape_next:
                escape_next = False
                i += 1
                continue

            if char == '\\':
                escape_next = True
                i += 1
                continue

            # Handle string literals
            if char == '"' and not in_string:
                in_string = True
                i += 1
                continue

            if char == '"' and in_string:
                in_string = False
                i += 1
                continue

            # Only count brackets outside of strings
            if not in_string:
                if char == '[':
                    bracket_depth += 1
                elif char == ']':
                    bracket_depth -= 1
                    if bracket_depth == 0:
                        array_end = i
                        break

            i += 1

        if array_end == -1:
            raise ValueError(f"Could not find closing ] for method '{method}' array")

        # Step 6: Extract current array content
        array_content = content[array_start + 1:array_end].strip()

        # Step 7: Add the new route at the end
        # Determine if we need a comma (if array is not empty)
        needs_comma = len(array_content.strip()) > 0 and not array_content.strip().endswith(',')

        # Insert the new route before the closing ]
        if needs_comma:
            new_route_entry = f', "{route}"'
        else:
            new_route_entry = f'"{route}"'

        # Build the modified content
        modified_content = (
            content[:array_end] +
            new_route_entry +
            content[array_end:]
        )

        self.logger.info(f"Successfully added route '{route}' to {api_name}.{method}")

        return modified_content

    def _add_new_service_block(
        self,
        content: str,
        api_name: str,
        method: str,
        route: str,
        file_path: str
    ) -> str:
        """
        Append a new service block as the last entry inside kong_configs.

        Used when the API service doesn't already exist. The upstream is built from
        the gateway file's path (see v2's resolve_service_config) rather than
        hardcoded — this used to write the stage Mumbai ALB into every gateway
        regardless of environment. Ensures the previous last entry has a trailing comma.
        """
        from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component_v2 import (
            resolve_service_config,
        )

        kong_configs_idx = content.find('kong_configs')
        if kong_configs_idx == -1:
            raise ValueError("kong_configs block not found in content")

        brace_start = content.find('{', kong_configs_idx)
        if brace_start == -1:
            raise ValueError("Could not find opening brace of kong_configs")

        brace_end = self._find_matching_brace(content, brace_start)
        if brace_end == -1:
            raise ValueError("Could not find closing brace of kong_configs")

        # Detect indentation by looking at the first existing service entry
        service_indent = "    "
        for line in content[brace_start + 1:brace_end].split('\n'):
            stripped = line.lstrip()
            if stripped.startswith('"') and '=' in stripped:
                service_indent = line[:len(line) - len(stripped)]
                break
        inner_indent = service_indent + "  "

        # Walk backwards from closing brace to the last non-whitespace char:
        # that is either the previous entry's `}` or a trailing `,`.
        i = brace_end - 1
        while i >= 0 and content[i] in ' \t\n\r':
            i -= 1
        if i < 0 or content[i] not in '},':
            raise ValueError("Unexpected kong_configs structure; cannot locate last entry")

        has_trailing_comma = content[i] == ','

        cfg = resolve_service_config(file_path)

        method_upper = method.strip().upper()
        new_block = (
            f'{service_indent}"{api_name}" = {{\n'
            f'{inner_indent}service = {{\n'
            f'{inner_indent}  host     = "{cfg["host"]}"\n'
            f'{inner_indent}  protocol = "{cfg["protocol"]}"\n'
            f'{inner_indent}  port     = {cfg["port"]}\n'
            f'{inner_indent}  path     = "{cfg["path"]}"\n'
            f'{inner_indent}}},\n'
            f'{inner_indent}routes = {{\n'
            f'{inner_indent}  "{method_upper}" = ["{route}"]\n'
            f'{inner_indent}}}\n'
            f'{service_indent}}}'
        )

        prefix = content[:i + 1]
        between = content[i + 1:brace_end]
        suffix = content[brace_end:]
        comma = "" if has_trailing_comma else ","

        modified_content = prefix + comma + '\n' + new_block + between + suffix

        self.logger.info(
            f"Created new service block '{api_name}' with {method_upper} route '{route}'"
        )
        return modified_content

    def preview_kong_hcl(self, api_name: str, method: str, route: str) -> str:
        """
        Format a Kong Gateway route configuration for display.

        This method returns a formatted message showing the route configuration.
        The actual template file is not modified - this is for display purposes only.

        Note: Validation is performed in the generate() method, so this method
        assumes parameters are already validated.

        Args:
            api_name: Name of the Kong API (e.g., "partner-dashboard-api", "falcon-service-api")
            method: HTTP method (GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD)
            route: Kong route pattern (e.g., "~/api/v1/users$", "~/api/v1/orders/(?<id>[^/]+)$")

        Returns:
            str: Formatted message with route configuration details

        Example:
            >>> component = AsporaKongRouteScriptGenComponent()
            >>> result = component.preview_kong_hcl("partner-dashboard-api", "GET", "~/api/v1/users$")
            >>> # Returns formatted message showing the route configuration
        """
        # Normalize parameters
        method = method.strip().upper()
        api_name = api_name.strip()
        route = route.strip()

        # Format success message to show HCL structure
        return f"""Route configuration:

"{api_name}" = {{
  routes = {{
    "{method}" = ["{route}"]
  }}
}}"""
