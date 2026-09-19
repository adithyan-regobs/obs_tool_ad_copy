"""Editing a live S3 bucket or SQS queue: what may change, and the Redis
handoff between `start_resource_edit` and `apply_resource_edit`.

Deliberately NOT routed through the chatbot. On the create path the chatbot
earns its place: it asks field by field, turns dropdown labels into codes and
checks the name is free. An edit needs none of that — the values are already
resolved codes sitting in `infrastructure_mst`, the placement is fixed, and the
name cannot change.

What may NOT change is derived, never listed here: the identity keys come from
infra_config_validator.NAME_SPECS and the immutable ones from the update guard.
Writing a second copy of those is how the guard, the factories and the validator
drifted apart the last time.

What MAY change is listed, because there is no source to derive it from. The
tenant metadata modules in this repo are empty stubs — chat-bot-POC owns the
forms now — so the list below is taken from what the tenant's script generator
actually patches into the terragrunt (aspora_s3_script_gen_component and
aspora_sqs_script_gen_component). A field outside that set produces a
byte-identical file, and an empty diff is not harmless: on the enterprise path
it fails PR creation and raises an alert.
"""

from typing import Any, Optional

from app.integrations.redis_integration import RedisIntegration
from app.domain.validators import infra_config_validator as infra_config
from app.domain.validators import infrastructure_update_guard as update_guard


_EDIT_PREFIX = "mcp:resource_edit:"
_EDIT_TTL_SECONDS = 6 * 3600


# Editing is offered only where the tenant's script generator PATCHES the
# committed terragrunt. Elsewhere the generator opens a blank template and
# writes over the file, so a second deploy would silently drop every hand-added
# input and reset anything the generator does not itself write.
SUPPORTED_TENANTS = frozenset({"aspora"})


# infrastructure type -> (resource label, case_ref_code).
#
# Read from here rather than from the tenant metadata registry: that registry is
# an empty stub for every tenant in this repo now, so get_resource_metadata
# returns None and any path that depends on it dies with "not configured for
# this tenant". The chatbot flow works around the same gap by building a
# pseudo-metadata dict from the values it already holds
# (provision_and_trigger_from_ticket_handler); this is the same move.
#
# The case_ref codes are the ones transaction_queue_service accepts for infra
# (transaction_queue_service.py:82-83).
#
# DynamoDB is absent on purpose: its table cannot be updated in place, so an
# "edit" would have to destroy and recreate it. Kong routes have their own
# editing path.
RESOURCE_TYPE_BY_INFRA_TYPE = {
    "s3_infrastructuretype_ref": "s3_bucket",
    "sqs_infrastructuretype_ref": "sqs_queue",
}

CASE_REF_BY_INFRA_TYPE = {
    "s3_infrastructuretype_ref": "create_bucket",
    "sqs_infrastructuretype_ref": "create_queue",
}

TRANSACTION_TABLE = "infrastructure_mst"


def trigger_metadata(infra_type: str) -> dict:
    """The three values _execute_trigger_core needs, without the registry."""
    return {
        "case_ref_code": CASE_REF_BY_INFRA_TYPE[infra_type],
        "infrastructuretype_ref_code": infra_type,
        "transaction_table": TRANSACTION_TABLE,
    }


EDITABLE_FIELDS: dict[str, tuple] = {
    "s3_infrastructuretype_ref": (
        {"field": "versioning", "type": "boolean",
         "description": "Keep every version of an object instead of overwriting."},
        {"field": "enable_s3_replication", "type": "boolean",
         "description": "Replicate objects to a bucket in another region."},
        {"field": "cross_account_account_id", "type": "string",
         "description": "12-digit AWS account allowed to read this bucket."},
    ),
    "sqs_infrastructuretype_ref": (
        {"field": "create_dlq", "type": "boolean",
         "description": "Create a dead-letter queue for messages that keep failing."},
        {"field": "max_receive_count", "type": "integer",
         "description": "Failed deliveries before a message moves to the DLQ (1-1000)."},
        {"field": "visibility_timeout_seconds", "type": "integer",
         "description": "How long a received message stays hidden (0-43200)."},
        {"field": "message_retention_seconds", "type": "integer",
         "description": "How long a message is kept (60-1209600)."},
        {"field": "dlq_message_retention_seconds", "type": "integer",
         "description": "How long a DLQ message is kept (60-1209600)."},
        {"field": "cross_account_ids", "type": "array",
         "description": "12-digit AWS accounts allowed to use this queue."},
    ),
}


FIELD_LABELS = {
    "versioning": "Object versioning",
    "enable_s3_replication": "Cross-region replication",
    "cross_account_account_id": "Cross-account account ID",
    "create_dlq": "Dead-letter queue",
    "max_receive_count": "Max receive count",
    "visibility_timeout_seconds": "Visibility timeout (seconds)",
    "message_retention_seconds": "Message retention (seconds)",
    "dlq_message_retention_seconds": "DLQ message retention (seconds)",
    "cross_account_ids": "Cross-account account IDs",
    "identifier": "Name",
    "fifo_queue": "FIFO queue",
    "bucket_name": "Resolved bucket name",
    "bucket_arn": "Bucket ARN",
    "queue_name": "Resolved queue name",
    "queue_url": "Queue URL",
    "queue_arn": "Queue ARN",
    "dlq_name": "Resolved DLQ name",
    "dlq_url": "DLQ URL",
    "dlq_arn": "DLQ ARN",
}


# Keys that are not fields at all — they describe WHERE the resource lives.
# Neither tool accepts them, so one can only arrive inside `changes`, which
# means the user asked to move the resource and the LLM tried anyway. Answering
# "not a field on this resource" would be true and useless; the user needs to
# hear that placement is fixed and that a new resource is the way to get one
# somewhere else.
PLACEMENT_KEYS = {
    "environment", "env", "product", "application", "application_code",
    "region", "geo_location", "geo_loc_mst_code", "cloudRegion",
}


def supported_infra_types() -> tuple:
    return tuple(RESOURCE_TYPE_BY_INFRA_TYPE)


def is_supported(infra_type: str) -> bool:
    return infra_type in RESOURCE_TYPE_BY_INFRA_TYPE


def label_for(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def locked_fields(infra_type: str) -> tuple:
    """Fields that name the resource, or are computed from its name.

    Changing one renames or moves the real AWS resource: the terragrunt path is
    keyed on the name, so a rename writes a NEW directory and leaves the old one
    committed. Terraform then manages both and the user has two live resources.
    """
    identity = infra_config.identity_keys(infra_type)
    immutable = update_guard.IMMUTABLE_LOCATOR_FIELDS.get(infra_type, ())
    seen, ordered = set(), []
    for field in (*identity, *immutable):
        if field not in seen:
            seen.add(field)
            ordered.append(field)
    return tuple(ordered)


def editable_fields(infra_type: str) -> tuple:
    """The fields an edit may change, with a label for each.

    Cross-checked against `locked_fields` so a key that becomes part of the
    resource's identity later cannot stay quietly editable here.
    """
    locked = set(locked_fields(infra_type))
    return tuple(
        {**field, "label": label_for(field["field"])}
        for field in EDITABLE_FIELDS.get(infra_type, ())
        if field["field"] not in locked
    )


def refused_changes(changes: Optional[dict], infra_type: str) -> dict:
    """Submitted keys this tool will not write, split by reason.

    Refused rather than dropped: a caller told the write succeeded, whose
    change was then silently discarded, has been lied to.
    """
    allowed = {f["field"] for f in editable_fields(infra_type)}
    locked = set(locked_fields(infra_type))

    out = {"locked": [], "placement": [], "unknown": []}
    for key in (changes or {}):
        if key in allowed:
            continue
        if key in locked:
            out["locked"].append(key)
        elif key in PLACEMENT_KEYS:
            out["placement"].append(key)
        else:
            out["unknown"].append(key)
    return out


def build_update_config(stored_locator: Optional[dict], changes: Optional[dict]) -> dict:
    """The type_specific_config for the update call.

    The FULL stored set with the edits laid over it, not just the changed keys.
    The factory rebuilds the resolved AWS name from whatever it is handed and
    defaults anything missing — for a queue, an absent fifo_queue reads as False
    and rebuilds the name without its suffix, pointing the row at a queue that
    does not exist.

    Derived keys are dropped: the factory recomputes them, and a stale copy
    arriving here would be ignored anyway (passthrough_extra filters it).
    """
    merged = dict(stored_locator or {})
    merged.update(changes or {})
    return merged


def _comparable(value: Any) -> Any:
    """Normalise before comparing a submitted value with the stored one.

    The locator holds booleans as real booleans when the canvas wrote them and
    as "true"/"false" strings when a form did, and numbers arrive either way.
    Comparing raw would report a change on a value nobody touched.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return text


def diff_rows(stored_locator: Optional[dict], changes: Optional[dict]) -> list:
    """Human-readable before/after rows for the confirmation step."""
    rows = []
    for field, new_value in (changes or {}).items():
        old_value = (stored_locator or {}).get(field)
        if _comparable(old_value) == _comparable(new_value):
            continue
        rows.append({
            "field": field,
            "label": label_for(field),
            "from": old_value,
            "to": new_value,
        })
    return rows


def concurrent_changes(
    opened_locator: Optional[dict],
    current_locator: Optional[dict],
    changes: Optional[dict],
) -> dict:
    """What moved underneath this edit session, split by whether it collides.

    The session caches the settings as they were when it opened. Someone else
    editing the same resource in DevLift meanwhile is invisible to it, and the
    apply would put the cached copy back — silently undoing their work.

    `collisions` are fields BOTH sides changed: this save would erase a value
    the user never saw. Those need a decision. `other` moved too but is not
    being written here, so it just needs to survive — which it does once the
    apply builds on the current locator instead of the cached one.
    """
    opened = opened_locator or {}
    current = current_locator or {}
    submitted = set(changes or {})

    collisions, other = [], []
    for field in set(opened) | set(current):
        if _comparable(opened.get(field)) == _comparable(current.get(field)):
            continue
        row = {
            "field": field,
            "label": label_for(field),
            "when_opened": opened.get(field),
            "now": current.get(field),
        }
        if field in submitted:
            row["you_want"] = changes[field]
            collisions.append(row)
        else:
            other.append(row)

    return {"collisions": collisions, "other": other}


def _edit_key(user_code: str, edit_id: str) -> str:
    return f"{_EDIT_PREFIX}{user_code}:{edit_id}"


async def save_edit_state(user_code: str, edit_id: str, payload: dict) -> bool:
    return await RedisIntegration.set_json(
        _edit_key(user_code, edit_id), payload, ttl=_EDIT_TTL_SECONDS,
    )


async def load_edit_state(user_code: str, edit_id: str) -> Optional[dict]:
    blob = await RedisIntegration.get_json(_edit_key(user_code, edit_id))
    return blob if isinstance(blob, dict) else None


async def clear_edit_state(user_code: str, edit_id: str) -> bool:
    return await RedisIntegration.delete(_edit_key(user_code, edit_id))
