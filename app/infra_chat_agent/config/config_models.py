from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, NewType, Optional, Protocol, Sequence, Union

from app.core.enum import InfraVendorEnum


# ----------------------------
# Strong string identifiers
# ----------------------------

TenantId = NewType("TenantId", str)
InfraTypeCode = NewType("InfraTypeCode", str)   # e.g. "s3", "sqs", "ecs"
ParamName = NewType("ParamName", str)
ParamKey = NewType("ParamKey", str)
QueryRef = NewType("QueryRef", str)


# ----------------------------
# Enums (prefer Enum over Literal)
# ----------------------------

# Use InfraVendorEnum from app.core.enum (on_prem, aws, gcp, azure)


class Operation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    DESCRIBE = "describe"


class GroupName(str, Enum):
    # v1 supports only these two groups
    PLACEMENT = "placement"
    ATTRIBUTES = "attributes"


class ParamType(str, Enum):
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"
    JSON = "json"


class UiWidget(str, Enum):
    DROPDOWN = "dropdown"
    TEXT = "text"
    TOGGLE = "toggle"
    READONLY = "readonly"
    HIDDEN = "hidden"


class ValueSourceType(str, Enum):
    STATIC = "static"
    DATABASE = "database"
    API = "api"
    INTERNAL = "internal"


class PromptSourceType(str, Enum):
    STATIC = "static"
    INTERNAL = "internal"


# ----------------------------
# Helper models
# ----------------------------

@dataclass(frozen=True)
class Option:
    """
    UI-friendly choice item.
    Store `value` in state; `label` is for presentation only.
    """
    label: str
    value: str


@dataclass(frozen=True)
class ValidationRule:
    """
    Validation rules applied AFTER resolution (user/db/internal/derived).

    Attributes:
        regex: Pattern to validate the input format (e.g., "^~/.*\\$$" for Kong routes)
        allowed: List of allowed values
        validate_as_regex: If True, validates that the input itself is a compilable regex pattern
    """
    regex: Optional[str] = None
    allowed: Optional[List[str]] = None
    validate_as_regex: bool = False


@dataclass(frozen=True)
class ConditionalRequirement:
    """
    Makes a parameter conditionally required based on another parameter's value.

    This allows expressing dependency relationships between parameters.
    When the specified condition is met, this parameter becomes required.

    Attributes:
        if_param: The parameter key to check (must reference another parameter in same group)
        has_value: List of values that trigger the requirement.
                   If the if_param has any of these values, this param becomes required.
        message: Optional message explaining why this parameter is required.
                 Shown to user when listing missing required parameters.

    Example:
        # Make cross_account_id required when enable_s3_replication is True
        ConditionalRequirement(
            if_param=ParamKey("enable_s3_replication"),
            has_value=[True],
            message="required when replication is enabled"
        )
    """
    if_param: ParamKey
    has_value: List[Any]
    message: Optional[str] = None


@dataclass(frozen=True)
class UiMeta:
    """
    Defines how a parameter should be collected/rendered per channel.
    """
    form: UiWidget = UiWidget.HIDDEN
    slack: UiWidget = UiWidget.HIDDEN
    chatbot: UiWidget = UiWidget.HIDDEN
  


# ----------------------------
# ValueSource models (TypedDict alternative: dataclasses)
# ----------------------------

@dataclass(frozen=True)
class StaticSource:
    type: ValueSourceType = ValueSourceType.STATIC
    options: List[Option] = field(default_factory=list)


@dataclass(frozen=True)
class DatabaseSource:
    type: ValueSourceType = ValueSourceType.DATABASE
    query_ref: QueryRef = QueryRef("")
    label_field: str = "label"
    value_field: str = "value"
    filter_by: Dict[str, str] = field(default_factory=dict)  # templated


@dataclass(frozen=True)
class ApiSource:
    type: ValueSourceType = ValueSourceType.API
    endpoint_ref: str = ""
    label_field: str = "label"
    value_field: str = "value"
    params: Dict[str, str] = field(default_factory=dict)     # templated
    headers: Dict[str, str] = field(default_factory=dict)    # templated


@dataclass(frozen=True)
class InternalSource:
    type: ValueSourceType = ValueSourceType.INTERNAL
    class_name: str = ""
    method_name: str = ""
    args: Dict[str, str] = field(default_factory=dict)       # templated
    mode: str = "value"  # "options" | "value" (keep simple v1)


ValueSource = Union[
    StaticSource,
    DatabaseSource,
    ApiSource,
    InternalSource
]

Validator = Union[
    InternalSource
]


# ----------------------------
# Prompt models
# ----------------------------

@dataclass(frozen=True)
class StaticPrompt:
    """
    Fully custom, hard-coded prompt string.
    """
    type: PromptSourceType = PromptSourceType.STATIC
    text: str = ""


@dataclass(frozen=True)
class InternalPrompt:
    """
    Prompt generated by allow-listed class.method.
    """
    type: PromptSourceType = PromptSourceType.INTERNAL
    class_name: str = ""
    method_name: str = ""


PromptSpec = Union[StaticPrompt, InternalPrompt]


# ----------------------------
# Core metadata models
# ----------------------------

@dataclass(frozen=True)
class ParameterMeta:
    """
    Describes ONE logical parameter.

    Example:
        ParameterMeta(
            key=ParamKey("product"),
            name=ParamName("Product"),
            type=ParamType.ENUM,
            required=True,
            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
            value_source=DatabaseSource(query_ref=QueryRef("tenant_products"))
        )
    """
    key: ParamKey  # Exact key used in state/dict
    name: ParamName  # Descriptive human name for display
    type: ParamType
    order: int
    required: bool = True
    # requredrule:=[]
    conditional: Optional[ConditionalRequirement] = None  # Makes this param conditionally required
    mutable: bool = True

    ui: UiMeta = field(default_factory=UiMeta)
    value_source: Optional[ValueSource] = None
    default: Optional[Any] = None
    validation: Optional[ValidationRule] = None

    # Prompt hinting (NEW)
    description: Optional[str] = None
    examples: Optional[List[str]] = None

    # Guardrail metadata (optional)
    pii_exempt: bool = False  # True if values for this param should be exempt from PII redaction


@dataclass(frozen=True)
class ParameterGroupMeta:
    """
    Grouping controls collection order and state partition.
    """
    group: GroupName
    order: int
    parameters: List[ParameterMeta]


@dataclass(frozen=True)
class ResourceMeta:
    """
    Resource contract (v1).
    We keep two fixed groups for simplicity and clarity.
    """
    tenantId: str
    infra_type: InfraTypeCode          # Database FK code: "s3_infrastructuretype_ref", "sqs_infrastructuretype_ref"
    infra_display_name: str             # Display name for prompts: "s3", "sqs", "dynamodb"
    cases: List[str]

    placement: ParameterGroupMeta      # must be GroupName.PLACEMENT
    attributes: ParameterGroupMeta     # must be GroupName.ATTRIBUTES

    # Prompt builder reference
    prompt: PromptSpec   # can be static OR internal

    # Validators - run at different stages of the workflow (fields with defaults must come last)
    pre_attributes_collect_validators: List[Validator] = field(default_factory=list)  # Runs BEFORE collecting attributes
    # pre_submit_validators : List[Validator]   # Runs BEFORE submitting (final validation/gating; can block submission by raising/returning errors)
    # post_submit_hooks      = []   # Runs AFTER a successful submit (side-effects like logging, notifications, auditing, async follow-ups)

    def __post_init__(self) -> None:
        # enforce correct group binding at construction time
        if self.placement.group is not GroupName.PLACEMENT:
            raise ValueError("placement group must be GroupName.PLACEMENT")
        if self.attributes.group is not GroupName.ATTRIBUTES:
            raise ValueError("attributes group must be GroupName.ATTRIBUTES")
