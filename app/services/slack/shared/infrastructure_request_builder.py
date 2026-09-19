"""Infrastructure Request Builder for Slack Integration

Builds InfrastructureCreateRequest from collected parameters.
Extracts business logic that was previously in Slack interaction handler.
"""
from typing import Dict, Any, List, Optional
from app.schemas.infrastructure_schemas import InfrastructureCreateRequest
from app.core.enum import EnvironmentEnum
import logging

logger = logging.getLogger(__name__)


async def resolve_kong_host_config_code(
    db, tenant_code: str, config_snapshot: Dict[str, Any]
) -> Optional[str]:
    """The kong HOST service's service_configs code for an add_route row, or None.

    Kong queue rows are keyed on the gateway service that RUNS kong — the
    deployable host the route is added onto, so deployment status tracks on
    that service (same anchoring as the Kong Gateway tab, which passes the
    host node's service_config code). The kong_route_configs (KRC) record
    stays as route inventory only, and the snapshot's service_mst_code keeps
    naming the TARGET service being routed to.

    The host is found by name: services in this tenant + application whose
    name contains "kong" — the same detection the canvas static data uses
    ("detected_via": "resource_name") — then its config row for the
    environment + region. Returns None instead of raising so the chat flow
    can fall back to the legacy KRC keying rather than dying mid-conversation.
    """
    from sqlalchemy import select
    from app.db.models.service_config_model import ServiceConfigModel
    from app.db.models.services_mst_model import ServicesMstModel

    applications_mst_code = config_snapshot.get("applications_mst_code")
    environment = config_snapshot.get("environment")
    geo_loc_mst_code = config_snapshot.get("geo_loc_mst_code")
    if not (applications_mst_code and environment and geo_loc_mst_code):
        return None

    try:
        environment = EnvironmentEnum(getattr(environment, "value", environment))
    except ValueError:
        return None

    rows = (await db.execute(
        select(ServiceConfigModel)
        .join(
            ServicesMstModel,
            ServicesMstModel.code == ServiceConfigModel.services_mst_code,
        )
        .where(
            ServicesMstModel.tenants_mst_code == tenant_code,
            ServicesMstModel.applications_mst_code == applications_mst_code,
            ServicesMstModel.name.ilike("%kong%"),
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.geo_loc_mst_code == geo_loc_mst_code,
        )
        .order_by(ServiceConfigModel.id)
    )).scalars().all()

    chosen = next((r for r in rows if not r.is_deleted), rows[0] if rows else None)
    return chosen.code if chosen else None


def is_v2_text_placement_resource(turn_resource: str) -> bool:
    """Check if a resource uses text-based placement parameter collection (no buttons).

    S3, SQS, and DynamoDB collect placement params from user text via MCP tools.
    Supports raw family names (e.g. "s3"), canonical infra refs
    (e.g. "s3_infrastructuretype_ref"), and common aliases.
    """
    if not turn_resource:
        return False

    normalized = str(turn_resource).strip().lower()
    if normalized.endswith("_infrastructuretype_ref"):
        normalized = normalized[: -len("_infrastructuretype_ref")]

    return normalized in {"s3", "sqs", "dynamodb", "dynamo"}


def validate_placement_params(placement_params: Dict[str, Any]) -> List[str]:
    """Validate required placement parameters are present.

    Checks both v1 key names (environment_enum, applications_mst_code, geo_loc_mst_code)
    and v2 key names (environment, product_name, geo_loc_code).

    Returns:
        List of missing field display names, empty if all present.
    """
    missing = []
    if not (placement_params.get("environment_enum") or placement_params.get("environment")):
        missing.append("environment")
    if not (placement_params.get("applications_mst_code") or placement_params.get("product_name")):
        missing.append("product_name")
    if not (placement_params.get("geo_loc_mst_code") or placement_params.get("geo_loc_code")):
        missing.append("region")
    return missing


class InfrastructureRequestBuilder:
    """Builds infrastructure requests from collected parameters.

    Extracts the infrastructure request building logic that was previously
    embedded in the Slack interaction handler, making it reusable.
    """

    def __init__(self, tenant_code: str):
        """Initialize the builder.

        Args:
            tenant_code: Tenant code for the request
        """
        self.tenant_code = tenant_code

    def build_infrastructure_request(
        self,
        turn_resource: str,
        collected_parameters: Dict[str, Any],
        collected_placement_parameters: Dict[str, Any],
        infrastructure_mst_code: str = None
    ) -> InfrastructureCreateRequest:
        """Build InfrastructureCreateRequest from graph state.

        Args:
            turn_resource: The resource type (e.g., "s3_infrastructuretype_ref" or "s3")
            collected_parameters: Type-specific parameters (identifier, region, etc.)
            collected_placement_parameters: Placement parameters (environment, applications_mst_code, etc.)
            infrastructure_mst_code: Existing infrastructure code for UPDATE. If None, creates new record.

        Returns:
            InfrastructureCreateRequest ready for infrastructure service

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Map resource type to infrastructuretype_ref_code
        if turn_resource.endswith("_infrastructuretype_ref"):
            infra_type_code = turn_resource
        else:
            infra_type_code = f"{turn_resource}_infrastructuretype_ref"

        case_type = collected_placement_parameters.get("case_type_ref_code", "")

        # Extract environment
        environment_str = (
            collected_placement_parameters.get("environment_enum") or
            collected_placement_parameters.get("environment")
        )
        if not environment_str:
            raise ValueError("environment is required but not found in placement parameters")

        environment = EnvironmentEnum(environment_str.lower())

        # Extract application code (try v1 keys, then v2 tool key)
        application_code = (
            collected_placement_parameters.get("applications_mst_code") or
            collected_placement_parameters.get("application_code") or
            collected_placement_parameters.get("product_name")
        )
        geo_loc_mst_code = (
            collected_placement_parameters.get("geo_loc_mst_code") or
            collected_placement_parameters.get("geo_loc_code")
        )

        logger.info(f"[BUILD_INFRA_REQUEST] application_code={application_code}, "
                    f"geo_loc={geo_loc_mst_code}, env={environment_str}")

        # Validate required fields
        if not application_code:
            raise ValueError("applications_mst_code is required but not found in placement parameters")
        if not geo_loc_mst_code:
            raise ValueError("geo_loc_mst_code is required but not found in placement parameters")

        # Build type_specific_config
        type_specific_config = {**collected_parameters}
        self._ensure_identifier(type_specific_config, case_type, turn_resource)

        if "region" not in type_specific_config:
            region = collected_placement_parameters.get("region")
            if region:
                type_specific_config["region"] = region

        # Extract service_mst_code for Kong routes
        service_mst_code = collected_placement_parameters.get("service_mst_code")

        return InfrastructureCreateRequest(
            code=infrastructure_mst_code,
            infrastructuretype_ref_code=infra_type_code,
            application_code=application_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            type_specific_config=type_specific_config,
            service_mst_code=service_mst_code
        )

    def _ensure_identifier(self, config: Dict[str, Any], case_type: str, turn_resource: str = "") -> None:
        """Ensure identifier field is present in config.

        Args:
            config: Config dict to modify in place
            case_type: Case type for context-specific identifier generation
            turn_resource: Resource type as fallback (e.g., "database_infrastructuretype_ref")
        """
        if "identifier" in config:
            return

        if "name" in config:
            config["identifier"] = config["name"]
        elif case_type == "add_route" and "route" in config:
            # For Kong Gateway routes, generate identifier from method and route
            method = config.get("method", "GET")
            route = config.get("route", "")
            route_clean = route.replace("~", "").replace("$", "").replace("/", "-").strip("-")
            config["identifier"] = f"{method.lower()}-{route_clean}"
        elif case_type == "create_bucket" and "bucket_name" in config:
            config["identifier"] = config["bucket_name"]
        elif case_type == "create_queue" and "queue_name" in config:
            config["identifier"] = config["queue_name"]
        elif case_type == "table_management" and "table_name" in config:
            config["identifier"] = config["table_name"]
        elif case_type in ("create_database", "database_creation") and "database_name" in config:
            config["identifier"] = config["database_name"]
        elif case_type in ("user_management", "database_user_management") and "database_name" in config:
            # For database user management, use database_name as identifier
            config["identifier"] = config["database_name"]
        # Fallback: check turn_resource if case_type didn't match (for MCP tool flows)
        elif "database_user" in turn_resource and "database_name" in config:
            config["identifier"] = config["database_name"]
        elif "database" in turn_resource and "database_name" in config:
            config["identifier"] = config["database_name"]

    def build_config_snapshot(
        self,
        turn_resource: str,
        collected_parameters: Dict[str, Any],
        collected_placement_parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build config_snapshot for transaction queue.

        Merges all parameters into a single snapshot for the queue.

        Args:
            turn_resource: Resource type
            collected_parameters: Type-specific parameters
            collected_placement_parameters: Placement parameters

        Returns:
            Config snapshot dictionary
        """
        logger.info(f"[BUILD_CONFIG_SNAPSHOT] turn_resource={turn_resource}")
        logger.info(f"[BUILD_CONFIG_SNAPSHOT] collected_placement_parameters: {collected_placement_parameters}")
        logger.info(f"[BUILD_CONFIG_SNAPSHOT] _service_mst_name: {collected_placement_parameters.get('_service_mst_name')}")

        # Filter out environment_enum from placement params to avoid duplicate with environment
        # Also filter out internal keys (starting with _)
        filtered_placement_params = {
            k: v for k, v in collected_placement_parameters.items()
            if k not in ("environment_enum", "environment") and not k.startswith("_")
        }

        # Clean infra_type - remove _infrastructuretype_ref suffix if present
        # This ensures Atlantis gets clean type like "s3", "sqs", "dynamodb"
        infra_type = turn_resource
        if infra_type.endswith("_infrastructuretype_ref"):
            infra_type = infra_type.replace("_infrastructuretype_ref", "")

        # Get product_name from _applications_mst_name (display name) for Atlantis naming
        # Falls back to v2 tool key (product_name is display name), then applications_mst_code
        product_name = (
            collected_placement_parameters.get("_applications_mst_name") or
            collected_placement_parameters.get("product_name") or
            collected_placement_parameters.get("applications_mst_code") or
            ""
        )

        # DEBUG: Log product_name extraction
        logger.info(f"[DB_USER_MGMT_DEBUG] build_config_snapshot - product_name='{product_name}', _applications_mst_name='{collected_placement_parameters.get('_applications_mst_name')}', applications_mst_code='{collected_placement_parameters.get('applications_mst_code')}'")



        snapshot = {
            "infra_type": infra_type,
            "product_name": product_name,
            "identifier": collected_parameters.get("identifier") or collected_parameters.get("name") or collected_parameters.get("database_name"),
            "environment": (
                collected_placement_parameters.get("environment_enum") or
                collected_placement_parameters.get("environment")
            ),
            "geo_loc_mst_code": (
                collected_placement_parameters.get("geo_loc_mst_code") or
                collected_placement_parameters.get("geo_loc_code")
            ),
            "applications_mst_code": (
                collected_placement_parameters.get("applications_mst_code") or
                collected_placement_parameters.get("product_name")
            ),
            "tenant_code": self.tenant_code,
            **collected_parameters,
            **filtered_placement_params
        }

        # Kong Gateway special handling - derive api_name from service name
        if turn_resource == "kong_gateway":
            # First check if api_name is already provided
            existing_api_name = snapshot.get("api_name")
            if existing_api_name and existing_api_name != "None":
                # api_name already set, ensure it ends with -service
                if not existing_api_name.endswith("-service"):
                    snapshot["api_name"] = f"{existing_api_name}-service"
            else:
                # Derive api_name from service name
                # Note: key is "_service_mst_name" (singular), not "_services_mst_name"
                service_name = collected_placement_parameters.get("_service_mst_name", "")
                if service_name:
                    api_name = service_name.lower().replace(" ", "-").replace("_", "-")
                    if not api_name.endswith("-service"):
                        api_name = f"{api_name}-service"
                    snapshot["api_name"] = api_name
                else:
                    # Log warning if api_name cannot be derived
                    logger.warning(
                        f"[BUILD_CONFIG_SNAPSHOT] Kong Gateway: api_name not found and "
                        f"_service_mst_name not available to derive it. "
                        f"collected_placement_parameters keys: {list(collected_placement_parameters.keys())}"
                    )

        # Database user management special handling - transform single server_name to mysql_servers/pgsql_servers array
        # File locator expects arrays with db_server_name, but Slack chat collects individual server_name
        case_type = collected_placement_parameters.get("case_type_ref_code", "")
        if case_type in ("user_management", "database_user_management", "mysql_user_management", "postgresql_user_management") or "database_user" in turn_resource:
            existing_mysql_servers = snapshot.get("mysql_servers")
            existing_pgsql_servers = snapshot.get("pgsql_servers")

            # Prefer grants produced by MCP tool outputs if already present in collected_parameters
            if existing_mysql_servers or existing_pgsql_servers:
                logger.info(
                    "[BUILD_CONFIG_SNAPSHOT] Database user management: using existing mysql_servers/pgsql_servers from collected_parameters"
                )
            else:
                server_name = collected_parameters.get("server_name")
                db_type = collected_parameters.get("db_type", "").lower()

                if server_name:
                    # Build server object with db_server_name (expected by file locator)
                    server_obj = {
                        "db_server_name": server_name,
                        "grants": []  # Empty grants if none were provided in collected_parameters
                    }

                    # Add to appropriate array based on db_type
                    if db_type == "mysql":
                        snapshot["mysql_servers"] = [server_obj]
                        logger.info(f"[BUILD_CONFIG_SNAPSHOT] Database user management: created mysql_servers with server_name={server_name}")
                    elif db_type in ("postgresql", "postgres", "pgsql"):
                        snapshot["pgsql_servers"] = [server_obj]
                        logger.info(f"[BUILD_CONFIG_SNAPSHOT] Database user management: created pgsql_servers with server_name={server_name}")
                    else:
                        # If db_type not specified, log warning
                        logger.warning(
                            f"[BUILD_CONFIG_SNAPSHOT] Database user management: db_type '{db_type}' not recognized, "
                            f"server_name={server_name} will not be added to mysql_servers or pgsql_servers"
                        )

            # Also add db_user_name and db_password for script generation
            username = (
                collected_parameters.get("db_user_name")
                or collected_parameters.get("username", "")
            )
            password = (
                collected_parameters.get("db_password")
                or collected_parameters.get("password", "")
            )
            if username:
                snapshot["db_user_name"] = username
            if password:
                snapshot["db_password"] = password
            snapshot["replace_grants"] = True

        return snapshot
