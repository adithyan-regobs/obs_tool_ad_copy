"""
Kong Route Configuration Validator

Domain-level validation for Kong Gateway route configurations.
Implements business rules and format validation for route parameters.
"""

import re
from typing import Optional
from app.core.enum import HttpMethodEnum


class KongRouteValidationError(ValueError):
    """Custom exception for Kong route validation errors"""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class KongRouteValidator:
    """Validator for Kong Gateway route configuration business rules"""

    # Kong route path regex pattern
    # All Kong routes must follow the format: ~/pattern$
    ROUTE_PATH_REGEX_PATTERN = r'^~/.+\$$'  # Regex routes: ~/pattern$

    # API name pattern (alphanumeric, hyphens, underscores)
    API_NAME_PATTERN = r'^[a-zA-Z0-9_-]+$'

    # Route group key (the Gateway tab's "tag"). Alphanumerics and underscores
    # joined by SINGLE hyphens — no leading, trailing or doubled ones. Underscores
    # pass as ordinary characters: only the hyphen collides with the "<tag>-<METHOD>"
    # rendering, and service names like "abcd_service" auto-fill the tag verbatim.
    # Mirrors TAG_RE in GatewayContentV4.tsx; keep the two in step.
    ROUTE_GROUP_KEY_PATTERN = r'^[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$'

    # kong_route_groups.regex_priority is a Postgres int4, so this is a storage
    # limit, not a Kong one — Kong's own schema declares regex_priority as an
    # unbounded integer ({ type = "integer", default = 0 }); only the expressions
    # router's separate `priority` field is bounded, at 2^46 - 1.
    MAX_REGEX_PRIORITY = 2**31 - 1

    @staticmethod
    def validate_http_method(method: str) -> None:
        """
        Validate HTTP method is one of the allowed values.

        Args:
            method: HTTP method string

        Raises:
            KongRouteValidationError: If method is invalid

        Example:
            >>> KongRouteValidator.validate_http_method("GET")  # Valid
            >>> KongRouteValidator.validate_http_method("INVALID")  # Raises error
        """
        errors: list[str] = []

        if not method or not method.strip():
            errors.append("HTTP method cannot be empty")
        else:
            method_upper = method.strip().upper()
            valid_methods = [m.value for m in HttpMethodEnum]

            if method_upper not in valid_methods:
                errors.append(
                    f"HTTP method '{method}' is not valid. "
                    f"Must be one of: {', '.join(valid_methods)}"
                )

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_route_path(route_path: str) -> None:
        """
        Validate Kong route path format.

        Kong route paths must follow the pattern: ~/pattern$
        - MUST start with ~/ (regex prefix)
        - MUST end with $ (exact match suffix)
        - Can include regex patterns like (?<id>[^/]+) for path parameters

        Args:
            route_path: Kong route pattern

        Raises:
            KongRouteValidationError: If route path format is invalid

        Example:
            >>> KongRouteValidator.validate_route_path("~/api/v1/users$")  # Valid
            >>> KongRouteValidator.validate_route_path("~/api/(?<id>[^/]+)$")  # Valid with param
            >>> KongRouteValidator.validate_route_path("/api/v1/users$")  # Invalid - missing ~/
            >>> KongRouteValidator.validate_route_path("~/api/v1/users")  # Invalid - missing $
            >>> KongRouteValidator.validate_route_path("bad format")  # Invalid
        """
        errors: list[str] = []

        if not route_path or not route_path.strip():
            errors.append("Route path cannot be empty")
        else:
            route_path = route_path.strip()

            # Check if it matches the required pattern: ~/pattern$
            if not re.match(KongRouteValidator.ROUTE_PATH_REGEX_PATTERN, route_path):
                errors.append(
                    f"Route path '{route_path}' format is invalid. "
                    "Must start with '~/' and end with '$' (e.g., '~/api/v1/users$')"
                )

            # Length validation
            if len(route_path) > 500:
                errors.append("Route path cannot exceed 500 characters")

            # Check for invalid characters (whitespace)
            if any(char in route_path for char in [' ', '\t', '\n']):
                errors.append("Route path cannot contain whitespace characters")

            # Check for common mistakes with angle brackets
            # 1. Backslash-angle-bracket (e.g., \<name>)
            if r'\<' in route_path or r'\>' in route_path:
                errors.append(
                    "Route pattern contains '\\<' or '\\>'. "
                    "Use named capture syntax instead. "
                    "Examples: (?<id>[^/]+) or (?<userId>[0-9]+)"
                )

            # 2. Bare angle brackets without (?<...>) (e.g., <apikey>)
            # Check for < or > that are NOT part of (?<name>...) pattern
            # Remove all valid (?<name>...) patterns first, then check for remaining < or >
            test_for_bare_brackets = re.sub(r'\(\?<[^>]+>', '', route_path)
            if '<' in test_for_bare_brackets or '>' in test_for_bare_brackets:
                errors.append(
                    "Route pattern contains '<' or '>' outside of named capture groups. "
                    "Use proper syntax: (?<name>pattern). "
                    "Example: (?<apikey>[^/]+) instead of <apikey>"
                )

            # Validate regex syntax (convert PCRE to Python syntax for validation)
            try:
                # Extract pattern between ~/ and $
                test_pattern = route_path[2:]  # Remove ~/
                if test_pattern.endswith('$'):
                    test_pattern = test_pattern[:-1]  # Remove $

                # Convert Kong PCRE named groups (?<name>...) to Python syntax (?P<name>...)
                python_pattern = re.sub(r'\(\?<([^>]+)>', r'(?P<\1>', test_pattern)

                # Try to compile to check for syntax errors
                re.compile(python_pattern)
            except re.error as regex_error:
                errors.append(
                    f"Route pattern has invalid regex syntax: {str(regex_error)}"
                )

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_api_name(api_name: str) -> None:
        """
        Validate API name format.

        API name should:
        - Not exceed 100 characters
        - Not be empty

        Args:
            api_name: API identifier

        Raises:
            KongRouteValidationError: If API name format is invalid

        Example:
            >>> KongRouteValidator.validate_api_name("user_api")  # Valid
            >>> KongRouteValidator.validate_api_name("user-api")  # Valid
        """
        errors: list[str] = []

        if not api_name or not api_name.strip():
            errors.append("API name cannot be empty")
        else:
            api_name = api_name.strip()

            # Length validation
            if len(api_name) > 100:
                errors.append("API name cannot exceed 100 characters")

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_route_group_key(route_group_key: str) -> None:
        """
        Validate a Gateway route group key (the UI's "tag").

        The key becomes the terragrunt kong_configs map key, rendered as
        "<tag>-<METHOD>" in target_keys. A trailing hyphen therefore yields
        "argentina--GET" — valid HCL, so it ships silently, but it reads as a
        typo and cannot be matched against the file by eye. Hence: alphanumerics
        and underscores joined by single hyphens, none leading or trailing.

        Case is unrestricted. Nothing downstream lowercases it, and services are
        named with capitals ("Test-Service-3"); an unlabelled group defaults its
        tag to the service name verbatim, so a lowercase-only rule would reject
        the tag the UI itself just generated.

        Args:
            route_group_key: The group tag

        Raises:
            KongRouteValidationError: If the key is empty or malformed

        Example:
            >>> KongRouteValidator.validate_route_group_key("payments")      # Valid
            >>> KongRouteValidator.validate_route_group_key("Test-Service-3")  # Valid
            >>> KongRouteValidator.validate_route_group_key("abcd_service")  # Valid
            >>> KongRouteValidator.validate_route_group_key("argentina-")    # Raises
        """
        errors: list[str] = []

        if not route_group_key or not route_group_key.strip():
            errors.append("Route group key cannot be empty")
        else:
            route_group_key = route_group_key.strip()

            if len(route_group_key) > 100:
                errors.append("Route group key cannot exceed 100 characters")

            if not re.match(KongRouteValidator.ROUTE_GROUP_KEY_PATTERN, route_group_key):
                errors.append(
                    f"Route group key '{route_group_key}' is not valid. "
                    "Use letters, numbers, underscores and single hyphens only "
                    "(no leading, trailing or repeated hyphens)"
                )

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_plugins(plugins) -> None:
        """
        Validate a group's plugin list.

        Deliberately NOT an allowlist: Kong plugins are open-ended and new ones
        get enabled without a code change here. Only shape is checked — a list
        of non-blank strings — which is what stops a null or a nested object
        reaching the generator's plugin block.

        Args:
            plugins: The group's plugin list

        Raises:
            KongRouteValidationError: If the list or any entry is malformed
        """
        errors: list[str] = []

        if plugins is None:
            return
        if not isinstance(plugins, (list, tuple)):
            raise KongRouteValidationError(["Plugins must be a list"])

        for p in plugins:
            if not isinstance(p, str) or not p.strip():
                errors.append(f"Plugin name must be a non-empty string (got {p!r})")

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_regex_priority(regex_priority) -> None:
        """
        Validate regex_priority is a non-negative integer.

        Priority decides which route wins when two patterns both match, so a
        bad value silently changes routing rather than failing.

        The only ceiling is the column's: regex_priority is a Postgres int4, so
        anything above 2^31-1 fails as a numeric-overflow 500 rather than a
        readable error. Rejecting it here turns that into a clear message.

        NOT a product limit. The Gateway tab's input deliberately has no clamp —
        "the gateway already uses 10000, and clamping to 1000 would silently
        rewrite those groups on the next save" — and v1's validate_priority caps
        at 50000, which belongs to the v1 form. int4max is ~2 billion, far above
        any real gateway value, so it cannot conflict with either.

        Args:
            regex_priority: The group's priority

        Raises:
            KongRouteValidationError: If not a non-negative integer, or too large
                for the column
        """
        if regex_priority is None:
            return

        errors: list[str] = []

        # bool is a subclass of int — True would otherwise pass as 1.
        if isinstance(regex_priority, bool) or not isinstance(regex_priority, int):
            errors.append(f"Regex priority must be an integer (got {regex_priority!r})")
        elif regex_priority < 0:
            errors.append("Regex priority cannot be negative")
        elif regex_priority > KongRouteValidator.MAX_REGEX_PRIORITY:
            errors.append(
                f"Regex priority cannot exceed {KongRouteValidator.MAX_REGEX_PRIORITY}"
            )

        if errors:
            raise KongRouteValidationError(errors)

    @staticmethod
    def validate_create_route_request(
        api_name: str,
        http_method: str,
        route_path: str,
        product_name: Optional[str] = None,
        region: Optional[str] = None
    ) -> None:
        """
        Validate complete create route request.

        Performs all validations and accumulates errors.

        Args:
            api_name: API identifier in kong_configs
            http_method: HTTP method (GET, POST, etc.)
            route_path: Kong route pattern
            product_name: Product name (optional)
            region: Region (optional)

        Raises:
            KongRouteValidationError: If any validation fails

        Example:
            >>> KongRouteValidator.validate_create_route_request(
            ...     api_name="user_api",
            ...     http_method="GET",
            ...     route_path="~/api/v1/users$",
            ...     product_name="myapp",
            ...     region="ap-south-1"
            ... )  # All valid - no error
            >>> KongRouteValidator.validate_create_route_request(
            ...     api_name="user_api",
            ...     http_method="GET",
            ...     route_path="/api/v1/users",
            ... )  # Raises error - invalid route format
        """
        errors: list[str] = []

        # Validate each field and accumulate errors
        try:
            KongRouteValidator.validate_api_name(api_name)
        except KongRouteValidationError as e:
            errors.extend(e.errors)

        try:
            KongRouteValidator.validate_http_method(http_method)
        except KongRouteValidationError as e:
            errors.extend(e.errors)

        try:
            KongRouteValidator.validate_route_path(route_path)
        except KongRouteValidationError as e:
            errors.extend(e.errors)

        # Product name and region validation (if provided)
        if product_name is not None:
            if not product_name.strip():
                errors.append("Product name cannot be empty if provided")
            elif len(product_name) > 100:
                errors.append("Product name cannot exceed 100 characters")

        if region is not None:
            if not region.strip():
                errors.append("Region cannot be empty if provided")
            elif len(region) > 50:
                errors.append("Region cannot exceed 50 characters")

        if errors:
            raise KongRouteValidationError(errors)
