"""One rulebook for the `type_specific_config` of an infrastructure request.

Every write path — the HTTP upsert, the Slack handlers, the devlift_mcp
dispatcher — reaches infrastructure_mst through InfrastructureCreationService,
so the rules are enforced there and defined here.

Until now they were scattered: the SQS rules lived in sqs_creation_service (a
service the upsert endpoint never calls), the DynamoDB rules sat on the HTTP
route (so the Slack and MCP callers skipped them), and S3 had none at all in
this repo — only in the dashboard's infraFieldRules.ts. A client-side rule is a
hint, not a guard.

A value of None is "not supplied", never an error. The dashboard sends null
explicitly to CLEAR a field (see the saveAndQueue comment in
useInfraResourceConfigHydration.ts) and the script generators read that as
"remove this line", so rejecting nulls would break every clear.
"""

import re
from typing import Any, NamedTuple, Optional

from fastapi import HTTPException, status


class InfraConfigError(ValueError):
    """A supplied configuration value breaks a naming or range rule."""


# ── Name rules ──────────────────────────────────────────────────────

# 1-80 chars, letters/numbers/hyphen/underscore. The .fifo suffix is appended by
# resolve_sqs_name, so a user-supplied one would produce "name.fifo.fifo".
SQS_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')

# AWS bucket naming, minus the rules the resolved name cannot break: the
# tenant/env/region prefix already guarantees the first character.
S3_IDENTIFIER_PATTERN = re.compile(r'^[a-z0-9.-]{3,63}$')

DYNAMODB_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+$')
DYNAMODB_ATTRIBUTE_PATTERN = re.compile(r'^[a-zA-Z0-9._-]{1,255}$')

# AWS caps the RESOLVED table name at 255. The name the user types is prefixed
# with {tenant}-{env}-{region}-{index}- before it reaches AWS, so the raw cap is
# held below the AWS one to leave room. Matches DYNAMO_TABLE_NAME_MAX in the
# dashboard's dynamoTableName.ts.
DYNAMODB_IDENTIFIER_MIN = 3
DYNAMODB_IDENTIFIER_MAX = 200

# AWS's own cap on the RESOLVED name, per type, and the locator keys holding it.
#
# The name a user types is not the name AWS sees: the terragrunt writes the raw
# identifier and the terraform layer prefixes it with
# {tenant}-{env}-{region}-{index}-, about 25 characters for aspora. So a typed
# name inside the AWS limit can still produce a resolved name over it — and it
# fails at terraform apply, after the pull request is merged, which is the
# expensive place to find out.
#
# Checked against the resolved value the factory just computed rather than by
# doing prefix arithmetic here: the shapes differ per type and per tenant (a
# PaaS DynamoDB table is prefixed, an enterprise one gets an `-{index}` suffix,
# and a FIFO queue carries `.fifo`), and every one of those would be a second
# place to keep in sync.
RESOLVED_NAME_LIMITS: dict[str, tuple] = {
    "s3_infrastructuretype_ref": (("bucket_name", 63),),
    "sqs_infrastructuretype_ref": (("queue_name", 80), ("dlq_name", 80)),
    "dynamodb_infrastructuretype_ref": (("table_name", 255),),
}


AWS_ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')

VALID_PARTITION_KEY_TYPES = {"S", "N", "B"}


# ── Range rules ─────────────────────────────────────────────────────

# (label, min, max) per SQS numeric field, mirroring the AWS limits and
# SQS_NUMBER_RULES in the dashboard's infraFieldRules.ts.
SQS_INT_RANGES: dict[str, tuple[str, int, int]] = {
    "max_receive_count": ("max_receive_count", 1, 1000),
    "visibility_timeout_seconds": ("visibility_timeout_seconds", 0, 43200),
    "message_retention_seconds": ("message_retention_seconds", 60, 1209600),
    "dlq_message_retention_seconds": ("dlq_message_retention_seconds", 60, 1209600),
}


# How each infrastructure type carries its name, in ONE table.
#
# Three tables used to disagree about this — the update guard's, the factories'
# and this module's — and every disagreement was a hole. The guard accepted only
# `db_server_name` for aurora while the factory also accepts `identifier`, so an
# aurora rename went through unchecked; redis was the same story with
# `redis_cluster_name`.
#
#   stored_key   the locator key the factory writes the name to
#   request_keys the keys a caller may send it under, in the order the factory
#                resolves them — first match wins, so this order must match the
#                factory's `config.get(a) or config.get(b)` exactly
#   derived_keys values the factory RECOMPUTES from the name on every write.
#                A caller may not supply them: each factory spreads the leftover
#                config last, so a supplied `bucket_name` or `queue_arn` lands
#                on top of the computed one and repoints the row at an arbitrary
#                resource — in another AWS account, for `accountId`.
class NameSpec(NamedTuple):
    stored_key: str
    request_keys: tuple
    derived_keys: tuple


NAME_SPECS: dict[str, NameSpec] = {
    "s3_infrastructuretype_ref": NameSpec(
        "identifier", ("identifier",), ("bucket_name", "bucket_arn"),
    ),
    "sqs_infrastructuretype_ref": NameSpec(
        "identifier", ("identifier",),
        ("queue_name", "queue_url", "queue_arn", "dlq_name", "dlq_url", "dlq_arn"),
    ),
    "dynamodb_infrastructuretype_ref": NameSpec(
        "identifier", ("identifier",), ("table_name",),
    ),
    "elasticache_redis_infrastructuretype_ref": NameSpec(
        "identifier", ("identifier", "redis_cluster_name"),
        ("redis_cluster_name", "user_id"),
    ),
    "devlift_k8s_postgres_infrastructuretype_ref": NameSpec(
        "server_name", ("identifier", "server_name"), (),
    ),
    "aurora_postgres_infrastructuretype_ref": NameSpec(
        "db_server_name", ("db_server_name", "identifier"), (),
    ),
    "aurora_mysql_infrastructuretype_ref": NameSpec(
        "db_server_name", ("db_server_name", "identifier"), (),
    ),
}


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _name_value(raw: Any) -> str:
    """The name as it will be STORED — trimmed, otherwise untouched.

    Deliberately not space-normalised. The dashboard rewrites spaces to dashes
    itself before it sends anything (saveAndQueue), but nothing on the server
    does, so normalising here would accept "order events" from the chatbot or
    MCP and hand it to resolve_sqs_name, which would build an AWS name AWS
    rejects. Refusing it keeps the stored value and the real resource name the
    same thing.
    """
    return _clean(raw)


# ── Per-field validators ────────────────────────────────────────────

def validate_sqs_identifier(identifier: Any) -> None:
    name = _name_value(identifier)
    if not name:
        raise InfraConfigError("identifier (queue name) is required and cannot be empty.")

    if len(name) > 80:
        raise InfraConfigError(
            f"Queue identifier must be 1-80 characters long. "
            f"Got: {len(name)} characters ('{name}'). "
            f"Examples: 'order-events', 'payment-processor'"
        )

    if name.lower().endswith('.fifo'):
        raise InfraConfigError(
            f"Queue identifier must NOT include '.fifo' suffix. "
            f"Got: '{name}'. The system appends '.fifo' automatically for FIFO queues. "
            f"Try: '{name[:-5]}'"
        )

    if not SQS_IDENTIFIER_PATTERN.match(name):
        raise InfraConfigError(
            f"Queue identifier '{name}' contains invalid characters. "
            f"Only letters (a-z, A-Z), numbers (0-9), hyphens (-), and underscores (_) are allowed. "
            f"Examples: 'order-events', 'payment_processor'"
        )


def validate_s3_identifier(identifier: Any) -> None:
    name = _name_value(identifier)
    if not name:
        raise InfraConfigError("identifier (bucket name) is required and cannot be empty.")

    if len(name) < 3 or len(name) > 63:
        raise InfraConfigError(
            f"Bucket identifier must be 3-63 characters long. "
            f"Got: {len(name)} characters ('{name}'). "
            f"Examples: 'app-logs', 'user-uploads'"
        )

    if not S3_IDENTIFIER_PATTERN.match(name):
        raise InfraConfigError(
            f"Bucket identifier '{name}' contains invalid characters. "
            f"Only lowercase letters (a-z), numbers (0-9), dots (.) and hyphens (-) "
            f"are allowed. Examples: 'app-logs', 'user-uploads'"
        )

    if not re.match(r'^[a-z0-9]', name) or not re.search(r'[a-z0-9]$', name):
        raise InfraConfigError(
            f"Bucket identifier '{name}' must start and end with a letter or number."
        )


def validate_dynamodb_identifier(identifier: Any) -> None:
    name = _name_value(identifier)
    if not name:
        raise InfraConfigError("identifier (table name) is required and cannot be empty.")

    if len(name) < DYNAMODB_IDENTIFIER_MIN or len(name) > DYNAMODB_IDENTIFIER_MAX:
        raise InfraConfigError(
            f"Table identifier must be {DYNAMODB_IDENTIFIER_MIN}-{DYNAMODB_IDENTIFIER_MAX} "
            f"characters long. Got: {len(name)} characters ('{name}'). AWS caps the full "
            f"table name at 255 and DevLift prefixes it with the tenant, environment and "
            f"region."
        )

    if not DYNAMODB_IDENTIFIER_PATTERN.match(name):
        raise InfraConfigError(
            f"Table identifier '{name}' contains invalid characters. "
            f"Only letters (a-z, A-Z), numbers (0-9), underscores (_), hyphens (-), "
            f"and dots (.) are allowed."
        )


def validate_dynamodb_partition_key(partition_key: Any) -> None:
    key = _clean(partition_key)
    if not key:
        raise InfraConfigError("partition_key is required and cannot be empty.")

    if not DYNAMODB_ATTRIBUTE_PATTERN.match(key):
        raise InfraConfigError(
            f"Partition key '{key}' is invalid. Must be 1-255 characters. "
            f"Only letters (a-z, A-Z), numbers (0-9), underscores (_), hyphens (-), "
            f"and dots (.) are allowed."
        )


def validate_partition_key_type(partition_key_type: Any) -> None:
    value = _clean(partition_key_type).upper()
    if not value:
        raise InfraConfigError("partition_key_type is required and cannot be empty.")
    if value not in VALID_PARTITION_KEY_TYPES:
        raise InfraConfigError(
            f"Invalid partition_key_type: '{partition_key_type}'. "
            f"Must be one of: {', '.join(sorted(VALID_PARTITION_KEY_TYPES))} "
            f"(String, Number, Binary)."
        )


def validate_aws_account_id(account_id: Any, *, field: str) -> None:
    value = _clean(account_id)
    if not AWS_ACCOUNT_ID_PATTERN.match(value):
        raise InfraConfigError(
            f"Each {field} must be exactly 12 digits. "
            f"Got: '{value}' ({len(value)} characters). Example: '123456789012'"
        )


def validate_cross_account_ids(cross_account_ids: Optional[list]) -> None:
    """A list of 12-digit account ids, no repeats.

    Empty strings are skipped rather than refused: the dashboard's array editor
    leaves a blank row behind and drops it at save time.
    """
    if not cross_account_ids:
        return

    if not isinstance(cross_account_ids, (list, tuple)):
        raise InfraConfigError(
            f"cross_account_ids must be a list of 12-digit AWS account IDs. "
            f"Got: {type(cross_account_ids).__name__}"
        )

    seen = set()
    for account_id in cross_account_ids:
        value = _clean(account_id)
        if not value:
            continue
        validate_aws_account_id(value, field="cross_account_id")
        if value in seen:
            raise InfraConfigError(
                f"Duplicate cross_account_id: '{value}'. List each account once."
            )
        seen.add(value)


def validate_int_range(value: Any, *, field: str) -> None:
    """One of the SQS numeric fields. Booleans are refused — bool subclasses int."""
    rule = SQS_INT_RANGES.get(field)
    if rule is None:
        return

    label, minimum, maximum = rule

    if isinstance(value, bool) or not isinstance(value, int):
        text = _clean(value)
        if not text:
            return
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise InfraConfigError(
                f"{label} must be a whole number. Got: '{text}'"
            )

    if not (minimum <= value <= maximum):
        raise InfraConfigError(
            f"{label} must be between {minimum} and {maximum}. Got: {value}"
        )


# ── Per-type entry points ───────────────────────────────────────────

def _validate_s3_fields(config: dict) -> None:
    account_id = config.get("cross_account_account_id")
    if _clean(account_id):
        validate_aws_account_id(account_id, field="cross_account_account_id")


def _validate_sqs_fields(config: dict) -> None:
    for field in SQS_INT_RANGES:
        if config.get(field) is not None:
            validate_int_range(config[field], field=field)

    validate_cross_account_ids(config.get("cross_account_ids"))


def _validate_dynamodb_fields(config: dict) -> None:
    partition_key = config.get("partition_key")
    if partition_key is not None:
        validate_dynamodb_partition_key(partition_key)

    partition_key_type = config.get("partition_key_type")
    if _clean(partition_key_type):
        validate_partition_key_type(partition_key_type)


_NAME_VALIDATORS = {
    "s3_infrastructuretype_ref": validate_s3_identifier,
    "sqs_infrastructuretype_ref": validate_sqs_identifier,
    "dynamodb_infrastructuretype_ref": validate_dynamodb_identifier,
}

_FIELD_VALIDATORS = {
    "s3_infrastructuretype_ref": _validate_s3_fields,
    "sqs_infrastructuretype_ref": _validate_sqs_fields,
    "dynamodb_infrastructuretype_ref": _validate_dynamodb_fields,
}

# Fields required on a complete create payload, beyond the name.
_REQUIRED_FIELDS = {
    "dynamodb_infrastructuretype_ref": ("partition_key",),
}


def _check_no_surrounding_space(config: dict, infra_type: str) -> None:
    """A name with a leading or trailing space is refused, not silently trimmed.

    The factories store `config[key]` untouched, so trimming here would validate
    one value and store another — " orders" passing the check and reaching AWS
    with the space still on it.
    """
    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return
    for key in spec.request_keys:
        raw = (config or {}).get(key)
        if not isinstance(raw, str) or raw == "":
            continue
        if raw.strip() == "":
            # Not the same as "not sent": the factories read a whitespace-only
            # string as truthy and store it, so a resource ends up named with
            # spaces. Empty-and-absent is handled by the require_all path.
            raise InfraConfigError(
                f"'{key}' cannot be only whitespace."
            )
        if raw != raw.strip():
            raise InfraConfigError(
                f"'{key}' must not start or end with a space. Got: '{raw}'"
            )


def validate_config(
    config: Optional[dict],
    infra_type: str,
    *,
    require_all: bool,
    existing_name: Optional[str] = None,
) -> None:
    """Validate the type_specific_config of a create or update request.

    `require_all` is True only where a complete payload is expected. The canvas
    creates the infrastructure_mst row when a node is dropped, before the user
    has named anything, so its first request legitimately carries no identifier
    — passing True there fails the create and leaves a node the detail panel
    then locks.

    `existing_name` is the name already stored. A name equal to it skips the
    syntax check: rows predating these rules can hold names the rules now refuse
    (uppercase buckets, say), and re-validating one on every save would make
    those rows permanently unsavable over a value the request is not changing.

    A type with no entry in NAME_SPECS passes untouched: the ones listed are the
    ones whose rules are known.
    """
    config = config or {}

    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return

    _check_no_surrounding_space(config, infra_type)

    name = submitted_name(config, infra_type)
    if name is None:
        if require_all:
            raise InfraConfigError(
                f"'{spec.stored_key}' (resource name) is required and cannot be empty."
            )
    elif name != existing_name:
        name_validator = _NAME_VALIDATORS.get(infra_type)
        if name_validator:
            name_validator(name)

    for field in _REQUIRED_FIELDS.get(infra_type, ()):
        if require_all and config.get(field) is None:
            raise InfraConfigError(f"{field} is required and cannot be empty.")

    field_validator = _FIELD_VALIDATORS.get(infra_type)
    if field_validator:
        field_validator(config)


def spec_for(infra_type: str) -> Optional[NameSpec]:
    return NAME_SPECS.get(infra_type)


def name_key_for(infra_type: str) -> Optional[str]:
    """The locator key holding the user-facing name, or None for unknown types."""
    spec = NAME_SPECS.get(infra_type)
    return spec.stored_key if spec else None


def submitted_name(config: Optional[dict], infra_type: str) -> Optional[str]:
    """The name in the request as the FACTORY will resolve it, or None.

    Walks request_keys in factory order and takes the first non-empty value, so
    a payload carrying two of them resolves to the one that will actually be
    stored. Reading only the stored key instead is what let an aurora or redis
    rename slip past the guard.
    """
    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return None
    for key in spec.request_keys:
        value = _name_value((config or {}).get(key))
        if value:
            return value
    return None


def stored_name(locator: Optional[dict], infra_type: str) -> Optional[str]:
    """The name already on the row.

    Falls back to the other accepted keys when the canonical one is absent.
    Rows written by an older factory can hold the name under the key that
    factory used — aurora rows carrying `server_name` rather than
    `db_server_name`, say. Reading only the canonical key returns nothing for
    those, and a guard comparing against nothing lets every rename through.
    """
    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return None
    locator = locator or {}
    for key in (spec.stored_key, *spec.request_keys):
        value = _name_value(locator.get(key))
        if value:
            return value
    return None


def with_name(config: Optional[dict], infra_type: str, name: str) -> dict:
    """A copy of `config` whose only name key is the one given.

    Every other accepted key is dropped, not just overwritten. The factories
    read the keys in their own order — k8s postgres takes `identifier` over
    `server_name` — so leaving a second one in place lets it win over the value
    being set here.
    """
    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return dict(config or {})
    cleaned = {
        k: v for k, v in (config or {}).items() if k not in spec.request_keys
    }
    cleaned[spec.request_keys[0]] = name
    return cleaned


def identity_keys(infra_type: str) -> tuple:
    """Every config key that names the resource or is computed from the name.

    The factories must keep these out of the leftover config they spread into
    the locator, or a caller supplies the computed value and overwrites what was
    just derived.
    """
    spec = NAME_SPECS.get(infra_type)
    if spec is None:
        return ()
    return tuple(spec.request_keys) + tuple(spec.derived_keys)


def passthrough_extra(
    config: Optional[dict], infra_type: str, *, also_exclude: tuple = (),
) -> dict:
    """The config keys a factory may copy into the locator verbatim."""
    excluded = set(identity_keys(infra_type)) | {"region"} | set(also_exclude)
    return {k: v for k, v in (config or {}).items() if k not in excluded}


def validate_resolved_names(locator: Optional[dict], infra_type: str) -> None:
    """Refuse a name that is too long once the prefix AWS sees is on it.

    `locator` is the one the factory just built, so the resolved values are
    already computed. The error says how many characters the user actually has,
    worked out from this resource's own overhead rather than a hardcoded
    number — the prefix differs per tenant, environment and type.
    """
    locator = locator or {}
    typed = _clean(locator.get(name_key_for(infra_type) or "identifier"))

    for key, limit in RESOLVED_NAME_LIMITS.get(infra_type, ()):
        resolved = _clean(locator.get(key))
        if not resolved or len(resolved) <= limit:
            continue

        overhead = len(resolved) - len(typed) if typed else 0
        allowed = max(limit - overhead, 0)
        raise InfraConfigError(
            f"The name '{typed}' is too long once DevLift builds the full AWS "
            f"name: '{resolved}' is {len(resolved)} characters and AWS allows "
            f"{limit}. Use {allowed} characters or fewer."
        )


def as_http_error(exc: InfraConfigError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
