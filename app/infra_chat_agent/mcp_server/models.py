from __future__ import annotations

from asyncio.log import logger
from typing import Annotated, Generic, Literal, Optional, TypeVar, Callable, ClassVar

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from app.domain.validators.kong_route_validator import KongRouteValidator
from app.infra_chat_agent.mcp_server.normalizers import normalize_partition_key_type
from app.plugin.aspora.validator.aspora_kong_validator import AsporaKongValidator
from app.infra_chat_agent.plugins.aspora.database_validator import validate as validate_database_server_aspora
from app.infra_chat_agent.mcp_server.validation import (
    _get_enum_values,
    _get_field_annotation,
    _is_bool_field,
)
from app.services.service_config_agent import state


# --- Type Variables for fields that vary per tenant ---

NameT = TypeVar("NameT", bound=str)
PartitionKeyT = TypeVar("PartitionKeyT", bound=str)
PartitionKeyTypeT = TypeVar("PartitionKeyTypeT", bound=str)
RegionT = TypeVar("RegionT", bound=str)
ProductT = TypeVar("ProductT", bound=str)
EnvironmentT = TypeVar("EnvironmentT", bound=str)
DatabaseServerT = TypeVar("DatabaseServerT", bound=str)
AwsAccountId = Annotated[str, StringConstraints(pattern=r"^\d{12}$")]


# --- Generic Base Models (fully type-safe, no LSP violation) ---

class CreateS3Model(
    BaseModel,
    Generic[NameT, RegionT, ProductT, EnvironmentT],
):
    """Create an S3 bucket."""
    name: NameT = Field(description="Bucket name")
    version: bool = Field(default=False, description="Enable versioning")
    region: RegionT = Field(description="Bucket region")
    product: ProductT = Field(description="Product type")
    environment: EnvironmentT = Field(description="Deployment environment")
    replication: Optional[bool] = Field(default=None, description="Enable replication")
    crossAccountId: Optional[AwsAccountId] = Field(
        default=None,
        description="Optional cross-account AWS account ID (exactly 12 digits)",
    )

    @model_validator(mode="after")
    def check_cross_account(self) -> CreateS3Model:
        if self.replication is True and not self.crossAccountId:
            raise ValueError("crossAccountId is required when replication is enabled")
        return self


class CreateSQSModel(
    BaseModel,
    Generic[NameT, RegionT, ProductT, EnvironmentT],
):
    """Create an SQS queue."""
    name: NameT = Field(description="Queue name")
    product: ProductT = Field(description="Product type")
    region: RegionT = Field(description="Queue region")
    environment: EnvironmentT = Field(description="Deployment environment")
    fifo: bool = Field(default=True, description="Enable FIFO queue")
    dlq: bool = Field(default=True, description="Enable dead letter queue")
    max_receive_count: Optional[int] = Field(
        default=None,
        ge=1,
        le=1000,
        description=(
            "Number of times a message can be received before moving to DLQ "
            "(must be between 1 and 1000)."
        ),
    )
    visibility_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        le=43200,
        description="Visibility timeout in seconds (0-43200, up to 12 hours).",
    )
    main_queue_retention_seconds: Optional[int] = Field(
        default=None,
        ge=60,
        le=1209600,
        description="Main queue retention in seconds (60-1209600, 1 minute to 14 days).",
    )
    dlq_retention_seconds: Optional[int] = Field(
        default=None,
        ge=60,
        le=1209600,
        description=(
            "DLQ retention in seconds (60-1209600)."
        ),
    )
    cross_account_ids: Optional[list[AwsAccountId]] = Field(
        default=None,
        description=(
            "Optional list of cross-account AWS account IDs; each ID must be exactly 12 "
            "digits; duplicates are normalized."
        ),
    )

    @field_validator("cross_account_ids", mode="before")
    @classmethod
    def normalize_cross_account_ids(cls, value: object) -> object:
        if value is None:
            return value

        # LLM may send a comma-separated string instead of a list
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]

        if not isinstance(value, list):
            return value

        normalized_ids: list[object] = []
        seen: set[object] = set()
        for account_id in value:
            cleaned = account_id.strip() if isinstance(account_id, str) else account_id
            if cleaned in seen:
                continue
            seen.add(cleaned)
            normalized_ids.append(cleaned)

        return normalized_ids

    # @model_validator(mode="after")
    # def validate_retention_relationship(self) -> CreateSQSModel:
    #     if (
    #         self.main_queue_retention_seconds is not None
    #         and self.dlq_retention_seconds is not None
    #         and self.dlq_retention_seconds < self.main_queue_retention_seconds
    #     ):
    #         raise ValueError(
    #             "dlq_retention_seconds must be greater than or equal to "
    #             "main_queue_retention_seconds."
    #         )
    #     return self


class CreateDynamoDBModel(
    BaseModel,
    Generic[NameT, PartitionKeyT, PartitionKeyTypeT, RegionT, ProductT, EnvironmentT],
):
    """Create a DynamoDB table."""
    identifier: NameT = Field(description="DynamoDB table name")
    partition_key: PartitionKeyT = Field(description="Partition key attribute name")
    partition_key_type: PartitionKeyTypeT = Field(
        description="Partition key data type (S=String, N=Number, B=Binary)"
    )
    product: ProductT = Field(description="Product type")
    region: RegionT = Field(description="Table region")
    environment: EnvironmentT = Field(description="Deployment environment")

    @field_validator("partition_key_type", mode="before")
    @classmethod
    def normalize_partition_key_type(cls, value: object) -> object:
        return normalize_partition_key_type(value)



class CreateKongRouteModel(BaseModel, Generic[NameT, RegionT, ProductT, EnvironmentT]):
    """Create a Kong Gateway route."""

    route: NameT = Field(description="Kong route path. Must begin with `~/` and end with `$`. Can be a home route (`~/$`), a fixed path (`~/api/v1/users$`), include path parameters (`~/api/v1/users/{id}$`), or use regex segments (`~/api/v1/users/[^/]+$`).")
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = Field(
        description="REST API method"
    )
    product: ProductT = Field(description="Product type")
    service: str = Field(description="API service name (validated against available services)")
    environment: EnvironmentT = Field(description="Deployment environment")
    region: RegionT = Field(description="Route region")
    # Optional, exactly like the Gateway tab's Tag field. Omitted, the route joins
    # the service's default group — which is what every chat-created route used to
    # do silently. The executor now says so in its reply and suggests a name.
    tag: Optional[str] = Field(
        default=None,
        description=(
            "Route group name (the Gateway tab's 'Tag'). Optional — omit it and the "
            "route joins the service's default group. Letters, digits and "
            "underscores joined by single hyphens, e.g. 'goms-service-tag1'. Give a "
            "NEW name to create a separate route group, which is what you need in "
            "order to set regex_priority."
        ),
    )
    regex_priority: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Kong regex priority for the route GROUP (not the individual route). "
            "Defaults to 0. When two groups match the same path Kong serves the "
            "higher priority and shadows the other; equal priorities have no "
            "tie-break, so overriding an existing group means setting this above "
            "that group's value."
        ),
    )

    @field_validator("tag", mode="before")
    @classmethod
    def normalize_tag(cls, value: object) -> object:
        """
        Blank means "no tag" — an LLM filling an optional field with "" or "none"
        must not create a group literally called that. Format is checked here
        rather than at save time so the user is corrected inside the chat turn,
        while they can still answer.
        """
        if value is None:
            return None
        tag = str(value).strip()
        if not tag or tag.lower() in {"none", "null", "n/a", "default"}:
            return None
        KongRouteValidator.validate_route_group_key(tag)
        return tag


class CreateDatabaseModel(
    BaseModel,
    Generic[NameT, DatabaseServerT, RegionT, ProductT, EnvironmentT],
):
    """Create a database."""
    database_name: NameT = Field(description="Database name")
    database_server: DatabaseServerT = Field(
        description="Database server to create the database on"
    )
    region: RegionT = Field(description="Database region")
    product: ProductT = Field(description="Product type")
    environment: EnvironmentT = Field(description="Deployment environment")


# --- Tenant A (concrete types) ---

class CreateS3TenantA(
    CreateS3Model[
        str,                                    # name
        Literal["london", "mumbai","canada"],# region
        Literal["core", "falcon"],  # product
        Literal["qa", "stage", "prod"],      # environment
    ]
):
    LABEL: ClassVar[str] = "an S3 bucket"
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        description=(
            "S3 bucket name (3-63 chars): use lowercase letters, numbers, dots (.), "
            "and hyphens (-); start and end with a letter/number; no consecutive dots (..)."
        ),
    )


class CreateSQSTenantA(
    CreateSQSModel[
        str,                                # name
        Literal["london", "mumbai","canada"],        # region
        Literal["core", "falcon"],          # product,
        Literal["qa", "stage", "prod"],     # environment
    ]
):
    LABEL: ClassVar[str] = "an SQS queue"
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        description=(
            "Sqs queue name (3-63 chars): use lowercase letters, numbers, dots (.), "
            "and hyphens (-); start and end with a letter/number; no consecutive dots (..)."
        ),
    )


class CreateDynamoDBTenantA(
    CreateDynamoDBModel[
        str,                                # identifier
        str,                                # partition_key
        Literal["S", "N", "B"],             # partition_key_type
        Literal["mumbai", "london"],        # region
        Literal["core", "falcon"],          # product
        Literal["qa", "stage", "prod"],     # environment
    ]
):
    """Create a DynamoDB table."""
    LABEL: ClassVar[str] = "a DynamoDB table"
    identifier: str = Field(
        pattern=r"^[a-zA-Z0-9._-]{3,255}$",
        description=(
            "DynamoDB table name (3-255 chars): use letters, numbers, dots (.), "
            "underscores (_), and hyphens (-)."
        ),
    )
    partition_key: str = Field(
        pattern=r"^[a-zA-Z0-9._-]{1,255}$",
        description=(
            "Partition key name (1-255 chars): use letters, numbers, dots (.), "
            "underscores (_), and hyphens (-)."
        ),
    )

class CreateKongRouteTenantA(
    CreateKongRouteModel[
        str,  # route
        Literal["london", "mumbai","canada"],        # region
        Literal["core", "falcon"],          # product,
        Literal["qa", "stage", "prod"],     # environment
    ]
):
    """Create a Kong route."""
    route: str = Field(
        pattern=r"^~/[a-zA-Z0-9/_\-\.\{\}\(\)\?\+\[\]\^\\\|dDwWsS]*\$$",
        description=(
            "Kong route regex expression: must start with `~/` and end with `$`. "
        ),
    )

    LABEL: ClassVar[str] = "a Kong Gateway route"
    VALIDATOR: ClassVar[Callable | None] = AsporaKongValidator.validate_from_chat


class CreateDatabaseTenantA(
    CreateDatabaseModel[
        str,                                          # database_name
        str,                                          # database_server
        Literal["london", "mumbai", "canada"],        # region
        Literal["core", "falcon"],                    # product
        Literal["qa", "stage", "prod"],                  # environment
    ]
):
    database_name: str = Field(
        pattern=r"^[a-zA-Z0-9_]{1,64}$",
        description=(
            "Database name (1-64 chars): use letters, numbers, "
            "and underscores (_)."
        ),
    )

    LABEL: ClassVar[str] = "a database"
    VALIDATOR: ClassVar[Callable | None] = validate_database_server_aspora


# --- Tenant B (concrete types) ---

class CreateS3TenantB(
    CreateS3Model[
        str,                                              # name
        Literal["us", "mumbai", "london"],                # region
        Literal["core", "falcon", "platform", "analytics"],  # product
        Literal["dev", "qa", "stage", "prod"],          # environment
    ]
):
    name: str = Field(
        pattern=r"^[a-zA-Z]{4,10}$",
        description="Bucket name, must be 4-10 letters only",
    )


class CreateSQSTenantB(
    CreateSQSModel[
        str,                                        # name
        Literal["mumbai", "london"],                # region
        str,                                        # product
        Literal["qa", "stage", "prod"],             # environment
    ]
):
    pass



# --- Tenant Registry ---

TENANT_MODELS: dict[str, dict[str, type[BaseModel]]] = {
    "aspora": {
        "CreateS3": CreateS3TenantA,
        "CreateSQS": CreateSQSTenantA,
        "CreateDynamoDB": CreateDynamoDBTenantA,
        "CreateKongRoute": CreateKongRouteTenantA,
        "CreateDatabase": CreateDatabaseTenantA,
    },
    "vance": {
        "CreateS3": CreateS3TenantA,
        "CreateSQS": CreateSQSTenantA,
        "CreateDynamoDB": CreateDynamoDBTenantA,
        "CreateKongRoute": CreateKongRouteTenantA,
        "CreateDatabase": CreateDatabaseTenantA,
    },
}








_MASTER_DATA_HINTS = {
    "region": "validate against MasterData Availability Matrix",
    "product": "validate against MasterData Availability Matrix per region",
    "environment": "validate against MasterData Availability Matrix per region and product",
}


def _build_tools_summary(tenant_models: dict[str, type[BaseModel]]) -> str:
    """Build a human-readable summary of available tools and their parameters."""
    parts = []
    for tool_name, model in tenant_models.items():
        label = getattr(model, "LABEL", None)
        label_suffix = f' [display: "{label}"]' if label else ""
        lines = [f"{tool_name}{label_suffix}: {model.__doc__ or ''}"]
        for pname, field_info in model.model_fields.items():
            desc = field_info.description or ""
            annotation = _get_field_annotation(pname, model)
            if annotation:
                enum_vals = _get_enum_values(annotation)
                if enum_vals:
                    desc += f" Options: {enum_vals}"
                if _is_bool_field(annotation):
                    desc += " (true/false)"
            if pname in _MASTER_DATA_HINTS:
                desc += f" — {_MASTER_DATA_HINTS[pname]}"
            req = "required" if field_info.is_required() else "optional"
            lines.append(f"  - {pname} ({req}): {desc}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_all_tenants_tools_summary(
    tenant_id: str | None = None,
    resource_type: str | None = None,
) -> str:
    """
    Build a tenant-scoped tool summary.

    Optional filters:
    - tenant_id: return summary for a single tenant
    - resource_type: return summary for a single tool type
      (accepts values like "CreateS3", "s3", "CreateSQS", "sqs", "CreateDynamoDB", "dynamodb")
    """
    if tenant_id is not None and tenant_id not in TENANT_MODELS:
        raise ValueError(f"Unknown tenant '{tenant_id}'. Available: {list(TENANT_MODELS.keys())}")

    selected_tenants = (
        {tenant_id: TENANT_MODELS[tenant_id]}
        if tenant_id is not None
        else TENANT_MODELS
    )

    canonical_tool_name: str | None = None
    if resource_type:
        normalized = resource_type.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        tool_aliases = {
            "s3": "CreateS3",
            "creates3": "CreateS3",
            "sqs": "CreateSQS",
            "createsqs": "CreateSQS",
            "dynamo": "CreateDynamoDB",
            "dynamodb": "CreateDynamoDB",
            "createdynamodb": "CreateDynamoDB",
            "kong": "CreateKongRoute",
            "kongroute": "CreateKongRoute",
            "konggateway": "CreateKongRoute",
            "createkongroute": "CreateKongRoute",
            "database": "CreateDatabase",
            "db": "CreateDatabase",
            "createdatabase": "CreateDatabase",
        }
        canonical_tool_name = tool_aliases.get(normalized)
        if canonical_tool_name is None:
            raise ValueError(
                f"Unknown resource_type '{resource_type}'. "
                "Supported values include: 'CreateS3', 'CreateSQS', 'CreateDynamoDB', 'CreateKongRoute', 's3', 'sqs', 'dynamodb', 'kong'"
            )

    tenant_parts = []
    for selected_tenant_id, tenant_models in selected_tenants.items():
        filtered_models = tenant_models
        if canonical_tool_name is not None:
            if canonical_tool_name not in tenant_models:
                raise ValueError(
                    f"Tool '{canonical_tool_name}' is not registered for tenant '{selected_tenant_id}'. "
                    f"Available: {list(tenant_models.keys())}"
                )
            filtered_models = {canonical_tool_name: tenant_models[canonical_tool_name]}
        tenant_parts.append(f"Tenant '{selected_tenant_id}':\n{_build_tools_summary(filtered_models)}")
    logger.info("[VALIDATE_PARAMS] tool prompt %s", "\n\n".join(tenant_parts))
    return "\n\n".join(tenant_parts)
