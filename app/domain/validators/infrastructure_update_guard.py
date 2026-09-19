"""What an UPDATE to an infrastructure record may not change.

`POST /api/v1/infrastructures` is an upsert: send `code` and it writes over the
existing row. Until now the only thing it verified was that the row belonged to
the caller's tenant, and everything else on the request was written straight
through — so one authenticated request could move a live bucket to another
product, environment or region, or rename it.

None of those are edits. The terragrunt path is built from
product / environment / region / name
(app/plugin/aspora/file_locator/aspora_file_locator.py:1036-1046), so changing
any one of them writes a NEW file and leaves the old one committed. Terraform
then keeps managing the old resource and creates a second one beside it. For a
rename that means two live resources; for a placement move the old file is
orphaned in a directory nothing reconciles.

The guard lives here rather than on the route because the route is not the only
door: the Slack handlers and the MCP dispatcher call
InfrastructureCreationService directly, as this repo's convention intends.
"""

from typing import Any, Optional

from fastapi import HTTPException, status

from app.core.enum import ResourceStatusEnum
from app.domain.validators import infra_config_validator as infra_config


# Which request keys carry the name, per type, comes from ONE shared table —
# infra_config_validator.NAME_SPECS — which the factories read too. This module
# used to keep its own copy, and it listed only `db_server_name` for aurora and
# only `identifier` for redis while the factories accepted a second key for
# each. A rename sent under the other key was invisible to the guard and went
# straight through.
def _name_field_table() -> dict:
    return {
        infra_type: (spec.stored_key, tuple(spec.request_keys))
        for infra_type, spec in infra_config.NAME_SPECS.items()
    }


NAME_FIELD_BY_TYPE: dict[str, tuple[str, tuple[str, ...]]] = _name_field_table()


# Columns the factory emits with create-time values that are meaningless on an
# update, and that BaseRepository.update would otherwise write anyway — it
# setattrs every key it is handed, nulls included (base_repository.py:94-96).
#
# The damage is not hypothetical and needs no attacker: `resource_identifier`
# holds the real AWS ARN written back by the Jenkins webhook, `gitops_workflow_id`
# links the row to the workflow that deployed it, and `infra_status` is a live
# resource's ONLINE marker. A plain settings save from the web wipes all three
# today, because the factory hands over None, None and INITIATED every time.
PRESERVED_ON_UPDATE = (
    "gitops_workflow_id",
    "resource_identifier",
    "infra_status",
    "infra_status_updated_at",
    "infra_status_updated_by",
)


# Locator values that are not the name but still decide what the real resource
# is called. `fifo_queue` feeds resolve_sqs_name (infrastructure_mst_factory.py
# :369-374), so flipping it on a deployed queue rewrites queue_name, queue_url,
# queue_arn and the DLQ trio to a queue that does not exist — the same damage as
# a rename, through a checkbox.
#
# Checked only once the resource has deployed, matching the rename rule: a DRAFT
# row is still a form, and the canvas legitimately toggles the box while the
# user is filling it in.
IMMUTABLE_LOCATOR_FIELDS: dict[str, tuple[str, ...]] = {
    "sqs_infrastructuretype_ref": ("fifo_queue",),
}


# A deploy in flight is reading the row it is deploying. Writing new settings
# underneath it produces a resource that matches neither the old config nor the
# new one, and the workflow has no way to notice. PR_RAISED is absent on
# purpose: nothing is running yet, and editing before merge is how a review
# comment gets addressed.
_DEPLOY_IN_FLIGHT_STATUSES = (
    ResourceStatusEnum.INITIALISING,
    ResourceStatusEnum.PROVISIONING,
    ResourceStatusEnum.BUILDING,
    ResourceStatusEnum.DEPLOYING,
    ResourceStatusEnum.VERIFYING,
    ResourceStatusEnum.SOFT_DELETING,
    ResourceStatusEnum.HARD_DELETING,
)


# Statuses that mean the resource has never reached infrastructure. Mirrors the
# web's own rule (isResourceDeletableStatus in the dashboard's
# types/resourceStatus.ts): a clean DRAFT with no deployment status is still
# just a form, so naming it is not a rename. Anything else — including a FAILED
# deploy — may have left a branch, a pull request or a committed terragrunt file
# carrying the old name.
_NEVER_DEPLOYED_STATUSES = (None, ResourceStatusEnum.DRAFT)


class InfrastructureUpdateError(ValueError):
    """An update tried to change something that identifies the resource."""


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def name_field_for(infra_type: str) -> tuple[str, tuple[str, ...]]:
    """(stored locator key, accepted request keys) for this type.

    Raises for a type not in the table. Failing CLOSED is the point: a type
    added later without an entry here would otherwise silently skip the rename
    check and reopen the hole this module exists to close.
    """
    entry = NAME_FIELD_BY_TYPE.get(infra_type)
    if entry is None:
        raise InfrastructureUpdateError(
            f"Updating '{infra_type}' is not supported — it has no known name "
            f"field, so a rename could not be detected. Add it to "
            f"NAME_FIELD_BY_TYPE before allowing updates for this type."
        )
    return entry


def submitted_name(config: dict, infra_type: str) -> Optional[str]:
    """The name in the request, or None when the request does not carry one.

    Resolved in the same order the factory resolves it, so a payload carrying
    two name keys is judged on the one that will actually be stored.

    None and "not sent" are the same thing here, and both mean "leave the stored
    name alone" — the canvas creates a row before the user has typed anything
    and its first saves legitimately omit it.
    """
    name_field_for(infra_type)  # fail closed on an unknown type
    return infra_config.submitted_name(config, infra_type)


def has_deployed(existing) -> bool:
    """True once the resource has been through, or into, a deployment."""
    if getattr(existing, "deployment_status", None) is not None:
        return True
    return getattr(existing, "status", None) not in _NEVER_DEPLOYED_STATUSES


def check_placement_unchanged(request, existing) -> None:
    """Product, environment and region are the resource's address. Refuse a move.

    Not silently ignored: a caller told the write succeeded, whose resource then
    did not move, has been lied to. The web never trips this — it sends the
    placement of the canvas the node is already on.
    """
    checks = (
        ("infrastructuretype_ref_code", "resource type",
         request.infrastructuretype_ref_code, existing.infrastructuretype_ref_code),
        ("application_code", "product",
         request.application_code, existing.applications_mst_code),
        ("geo_loc_mst_code", "region",
         request.geo_loc_mst_code, existing.geo_loc_mst_code),
    )

    changed = [
        (label, old, new)
        for _, label, new, old in checks
        if _clean(new) and _clean(new) != _clean(old)
    ]

    request_env = getattr(request.environment, "value", request.environment)
    existing_env = getattr(existing.environments_enum, "value", existing.environments_enum)
    if _clean(request_env) and _clean(request_env) != _clean(existing_env):
        changed.append(("environment", existing_env, request_env))

    if not changed:
        return

    detail = "; ".join(f"{label} ({old} -> {new})" for label, old, new in changed)
    raise InfrastructureUpdateError(
        f"Cannot change where a resource lives: {detail}. Product, environment, "
        f"region and resource type decide its path in the infrastructure "
        f"repository — changing one would destroy this resource and build a new "
        f"one. Create a new resource in the target location instead."
    )


def check_name_unchanged(request, existing) -> None:
    """Refuse a rename once the resource has been deployed.

    Allowed before that, matching the web: the canvas writes the row when the
    node is dropped, before any name exists, so the first save that names it is
    not a rename. A row that has only ever been a DRAFT has no committed file
    and no pull request to leave behind.
    """
    infra_type = request.infrastructuretype_ref_code
    name_field_for(infra_type)  # fail closed on an unknown type

    new_name = submitted_name(request.type_specific_config or {}, infra_type)
    if new_name is None:
        return

    old_name = _clean(infra_config.stored_name(existing.locator, infra_type))
    if not old_name or old_name == new_name:
        return

    if not has_deployed(existing):
        return

    raise InfrastructureUpdateError(
        f"Cannot rename '{old_name}' to '{new_name}' — it has already been "
        f"deployed. The name decides the resource's path in the infrastructure "
        f"repository, so renaming it would leave '{old_name}' running and "
        f"create a second resource. Create a new resource instead."
    )


def strip_preserved_columns(factory_data: dict) -> dict:
    """Drop the create-time columns so an update cannot clear them.

    Removed rather than re-supplied from the existing row: absent means the ORM
    leaves the column alone, which is exactly the intent, and re-supplying would
    rewrite a value the update has no opinion about.
    """
    return {k: v for k, v in (factory_data or {}).items() if k not in PRESERVED_ON_UPDATE}


def _as_bool(value: Any) -> Optional[bool]:
    """Normalise a locator boolean.

    The same field arrives as a real bool from the canvas and as "true"/"false"
    from a form, so comparing raw values would report a change nobody made and
    refuse a legitimate save.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return _clean(value).lower() in ("true", "1", "yes")


def check_immutable_locator_fields(request, existing) -> None:
    """Refuse a change to a locator value that renames the real resource."""
    fields = IMMUTABLE_LOCATOR_FIELDS.get(request.infrastructuretype_ref_code, ())
    if not fields or not has_deployed(existing):
        return

    config = request.type_specific_config or {}
    locator = existing.locator or {}

    for field in fields:
        if field not in config:
            continue
        submitted = _as_bool(config[field])
        if submitted is None:
            continue
        # A row written before this key existed has no stored value to compare
        # against. Refusing then would block an ordinary save on every queue
        # predating the field, over a value the user did not change.
        if field not in locator or locator.get(field) is None:
            continue
        if submitted == _as_bool(locator.get(field)):
            continue
        raise InfrastructureUpdateError(
            f"Cannot change '{field}' on a resource that has already been "
            f"deployed — it decides the resource's real name, so changing it "
            f"would leave the existing one running and create a second. Create "
            f"a new resource instead."
        )


def check_not_deploying(existing) -> None:
    """Refuse a write while a deployment is reading this row."""
    current = getattr(existing, "status", None)
    if current in _DEPLOY_IN_FLIGHT_STATUSES:
        label = getattr(current, "value", current)
        raise InfrastructureUpdateError(
            f"This resource is {label} — a deployment is in progress and is "
            f"reading its configuration. Wait for it to finish, then save again."
        )


def guard_update(request, existing) -> None:
    """Every identity check for an update, in one call. Raises on refusal."""
    check_not_deploying(existing)
    check_placement_unchanged(request, existing)
    check_name_unchanged(request, existing)
    check_immutable_locator_fields(request, existing)


def as_http_error(exc: InfrastructureUpdateError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
