"""
Variable Service

Two-stage flow for resource variables/secrets:

  1. save_variables (staging):
     - Metadata goes to variable_mst (the value itself is NEVER stored in DB).
     - The full item list is staged as ONE JSON file in the temporary bucket
       (settings.secret_temp_bucket), secret values KMS-encrypted, plain
       variables as-is. No oldValue, no versioning at this stage.
       Path: <workspace>/<application>/<env>/<region>/01/<user_email>/<infra_type>-<service>-service/variables.json

  2. deploy_variables (commit):
     - Reads the caller's staged file, and for each item:
       * journals versioned audit entries to the audit bucket
         (settings.secret_audit_bucket) — a value update is ONE 'update' entry
         (oldValue = replaced live value, newValue = new value); a key rename
         closes the old key with a 'delete' entry (newValue = the value it
         held) and starts the new key with an 'add' entry (oldKey = old key)
       * writes the actual value to AWS: secrets -> Secrets Manager,
         variables -> SSM Parameter Store
       * stores the resulting ARN / parameter name on the variable_mst row
     - Deletes the staged file on success.

Audit path layout:
  <workspace>/<application>/<environment>/<region>/01/<eks|ecs>/services/<service>/secretes/<key>/version-N.json

The deployed ("devlift") value is read back from the state bucket
(settings.secret_state_bucket) — a single per-service state.json holding the
latest value/version of every key — and the staged ("pending") value from the
temp bucket (read_deployed_source / read_deployed_sources_bulk / read_pending_map),
to power the eager /project-variables/with-values payload, which surfaces both
alongside the live cloud value so the UI can detect drift. (The audit bucket
still journals versioned history on deploy; it is no longer read for /with-values.)
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enum import (
    EnvironmentEnum,
    InfraVendorEnum,
    SecretProviderEnum,
    VariableScopeTypeEnum,
    VariableTypeEnum,
    WorkflowSourceTableEnum,
)
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.db.models.variable_mst_model import VariableMstModel
from app.handlers.environment_variable_handler import EnvironmentVariableHandler
from app.handlers.file_manager_handler import FileManagerHandler
from app.integrations.aws_integration import AWSIntegration
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.repository.resource_connection_repository import ResourceConnectionRepository
from app.repository.variable_mst_repository import VariableMstRepository
from app.schemas.variable_schemas import (
    CloneVariableItem,
    CloneVariableResult,
    CloneVariablesRequest,
    DeployVariableResult,
    SaveVariableItem,
    SaveVariableResult,
    StageSyncRequest,
    StageSyncResult,
)

logger = logging.getLogger(__name__)

_EKS_INFRA_TYPE_REF = "eks_infrastructuretype_ref"
_VERSION_RE = re.compile(r"version-(\d+)\.json$")
_STAGED_FILE_NAME = "variables.json"

# What a write-only value is displayed as. The listing never returns the real
# value, so a client can only ever echo this back — accepting it would silently
# overwrite the secret with literal bullets, so saves carrying it are rejected.
WRITE_ONLY_MASK = "••••••••"


# Tenant segment of the SSM / Secrets Manager paths. Tenants that share an
# AWS estate map to the owning tenant's prefix; a tenant not listed here
# defaults to its own code.
_TENANT_PATH_PREFIX_MAP = {
    "vance": "vance",
    "aspora": "vance",
}


def _tenant_path_prefix(tenant_code: str) -> str:
    return _TENANT_PATH_PREFIX_MAP.get((tenant_code or "").lower(), tenant_code)


def _to_table_enum(table_name: str) -> WorkflowSourceTableEnum:
    """Map payload strings like 'service_config' / 'infrastructure' to the enum."""
    try:
        return WorkflowSourceTableEnum(table_name.upper())
    except ValueError:
        raise ValueError(f"Unknown table_name '{table_name}'")


@dataclass
class _PathContext:
    """Resolved segments for temp-staging and audit-trail folder paths."""
    workspace_name: str
    application_code: str
    application_name: str
    environment: EnvironmentEnum
    region_name: str
    infra_type: str  # 'eks' | 'ecs'
    service_name: str

    def _base(self) -> str:
        return (
            f"{self.workspace_name}/{self.application_name}/{self.environment.value}/"
            f"{self.region_name}/01"
        )

    def _service_segment(self) -> str:
        """Deployed service segment, mirroring the k8s/AWS DNS naming.

        - the name is lowercased and underscores become hyphens
          (``Canopy`` -> ``canopy``, ``banking_service`` -> ``banking-service``),
          since cloud resource paths are DNS-style: lowercase, no underscores;
        - ``-service`` is appended only when absent, so a service already named
          ``comms-service`` stays ``comms-service`` (not ``comms-service-service``)
          and ``cron`` becomes ``cron-service``."""
        n = self.service_name.lower().replace("_", "-")
        return n if n.endswith("-service") else f"{n}-service"

    def audit_key_prefix(self, key: str, is_secret: bool) -> str:
        """Audit-trail folder for a key. Secrets and plain configs are kept in
        separate folders so the layout mirrors the AWS targets (Secrets Manager
        vs SSM /configs)."""
        folder = "secrets" if is_secret else "configs"
        return f"{self._base()}/{self.infra_type}/services/{self.service_name}/{folder}/{key}/"

    def staged_file_key(self, user_email: str) -> str:
        return (
            f"{self._base()}/{user_email}/"
            f"{self.infra_type}-{self._service_segment()}/{_STAGED_FILE_NAME}"
        )

    def _type_segment(self) -> str:
        """Path segment for the resource-type slot in the secret/parameter name.

        ECS always uses the literal 'service' here (matching the ECS Terraform
        layer's naming: ``{org}/{env}/{region}/{index}/service/secrets/{id}``),
        while other infra types (e.g. eks) use their own infra_type value."""
        return "service" if self.infra_type == "ecs" else self.infra_type

    def secret_path(self, tenant_code: str) -> str:
        """Consolidated Secrets Manager secret for the service — ONE secret
        holding all its secret keys as a JSON key/value map.
        Format: {tenant}-{app}/{env}/{region}/01/{type}/secrets/{service}-service
        where {type} is 'service' for ECS, else infra_type (e.g. 'eks')."""
        return (
            f"{_tenant_path_prefix(tenant_code)}-{self.application_name}/{self.environment.value}/"
            f"{self.region_name.lower()}/01/{self._type_segment()}/secrets/{self._service_segment()}"
        )

    def parameter_path(self, tenant_code: str, key: str) -> str:
        """SSM parameter for a plain variable (one parameter per key).
        Format: /{tenant}-{app}/{env}/{region}/01/{type}/configs/{service}-service/{KEY}
        where {type} is 'service' for ECS, else infra_type (e.g. 'eks')."""
        return (
            f"/{_tenant_path_prefix(tenant_code)}-{self.application_name}/{self.environment.value}/"
            f"{self.region_name.lower()}/01/{self._type_segment()}/configs/{self._service_segment()}/{key}"
        )

    def parameter_prefix(self, tenant_code: str) -> str:
        """SSM path prefix for all config variables of this service (without trailing key).
        Used for GetParametersByPath bulk fetch."""
        return (
            f"/{_tenant_path_prefix(tenant_code)}-{self.application_name}/{self.environment.value}/"
            f"{self.region_name.lower()}/01/{self._type_segment()}/configs/{self._service_segment()}"
        )

    def state_file_key(self) -> str:
        """State bucket key for this service's state.json (latest version per key)."""
        return f"{self._base()}/{self.infra_type}/services/{self.service_name}/state.json"


class VariableService:
    """Orchestration layer for resource variables."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.variable_repo = VariableMstRepository(db)
        self.vendor_accounts_repo = InfraVendorAccountsMstRepository(db)
        self.connection_repo = ResourceConnectionRepository(db)

    # ══════════════════════════════════════════════════════════════════════════
    # API 1 — save (stage to temp bucket)
    # ══════════════════════════════════════════════════════════════════════════

    async def save_variables(
        self,
        items: List[SaveVariableItem],
        user: UserMstModel,
        tenant: TenantsMstModel,
    ) -> Tuple[List[SaveVariableResult], Optional[str]]:
        """Save metadata to variable_mst and stage the value list in the temp bucket.

        Optimised batch flow (target: 1000 items in seconds, not minutes):
          - The frontend sends ``variable_code`` for existing variables, so we
            NEVER look up a row to classify an item — zero per-item SELECTs.
              no variable_code            → add    (INSERT a new row)
              variable_code + delete op   → delete (staged only, no DB write)
              variable_code (otherwise)   → update (bulk UPDATE by code)
          - All secret values are encrypted through ONE KMS client
            (kms_encrypt_many — one TLS handshake for the whole batch).
          - The per-item loop is pure in-memory (no awaits); DB work happens
            once after it: one bulk INSERT (adds) + one bulk UPDATE (existing).

        Returns (per-item results, staged file key).
        """
        if not items:
            return [], None

        ctx = await self._resolve_path_context(items[0].transaction_code)
        # KMS key and S3 buckets are in stage — always use the machine's own role.
        storage_auth = self._get_storage_auth_config()

        for item in items:
            if _to_table_enum(item.table_name) != WorkflowSourceTableEnum.SERVICE_CONFIG:
                raise ValueError(
                    f"Only service_config owners are supported for now, got '{item.table_name}'"
                )

        # Prior staged entries let a staged DELETE preserve a pending add/edit's
        # value for Revert (_prev_staged); one S3 GET for the whole batch.
        staged_prior = await self._read_staged_items(ctx, user, storage_auth)

        # ── Owner rows: one SELECT reused by everything below ──
        # Feeds the duplicate-key pre-check (mirrors uq_variable_mst_owner_key),
        # the deployed-ness cross-check for the caller's is_draft_value flag, and
        # the write-only key set. Read up front because the no-op detection right
        # after has to skip write-only keys.
        live_keys: Set[str] = set()
        deployed_keys: Set[str] = set()
        row_by_key: Dict[str, VariableMstModel] = {}
        write_only_keys: Set[str] = set()
        allow_draft_purge = True
        try:
            owner_rows = await self.variable_repo.get_by_owner(
                _to_table_enum(items[0].table_name), items[0].transaction_code
            )
            live_keys = {r.key for r in owner_rows}
            deployed_keys = {r.key for r in owner_rows if r.variable_cloud_identifier}
            row_by_key = {r.key: r for r in owner_rows}
            write_only_keys = {r.key for r in owner_rows if r.is_write_only}
        except Exception as exc:
            # Pre-check is best-effort; the DB constraint stays the backstop.
            # Without the rows the is_draft_value cross-check is impossible, so
            # draft purging is disabled and classification behaves as before.
            logger.warning("save: duplicate-key pre-check read failed: %s", exc)
            allow_draft_purge = False

        # Keys this save treats as write-only: already flagged on the row, or
        # being flagged by this very item.
        write_only_now: Set[str] = write_only_keys | {
            it.key for it in items if it.is_write_only
        }

        # No-op detection: a save is a no-op ONLY when the new value already
        # matches BOTH the live AWS value (cloud) AND the deployed baseline
        # (devlift / state.json) — i.e. everything is in sync and deploying it
        # would change nothing. Deploying a staged value does two things: push to
        # the provider AND update state.json. So requiring only cloud==new is
        # wrong: if AWS drifted (cloud != devlift) and the user edits to the AWS
        # value to accept the drift, deploying still updates devlift — a real
        # change that MUST stay staged and shown. Requiring both means any drift
        # keeps the edit visible. Dropping a true no-op is important: a lingering
        # draft is re-pushed by the next deploy and could clobber a concurrent
        # AWS change. Only value-only updates of an existing key qualify (not
        # renames/deletes/adds). We fetch only the exact candidate keys (their SSM
        # names are known); secrets share one consolidated Secrets Manager entry
        # (one get_map) and devlift is one state.json GET. A read failure degrades
        # to "stage normally" (safe).
        #
        # Write-only keys are excluded: the user cannot see the current value, so
        # they cannot know their save is a no-op — silently dropping it would look
        # like the save failed. The cost is one redundant redeploy of an
        # unchanged value.
        update_var_keys = [
            it.key for it in items
            if it.variable_code and it.operation != "delete" and it.type != "secret"
            and not (it.old_key and it.old_key != it.key)
            and it.key not in write_only_now
        ]
        update_secret_keys = [
            it.key for it in items
            if it.variable_code and it.operation != "delete" and it.type == "secret"
            and not (it.old_key and it.old_key != it.key)
            and it.key not in write_only_now
        ]
        cloud_values: Dict[str, Optional[str]] = {}
        devlift_values: Dict[str, Optional[str]] = {}
        if update_var_keys or update_secret_keys:
            try:
                cloud_auth = await self._get_cloud_auth_config(ctx, tenant.code)
                if update_var_keys:
                    async def _read_param(k: str) -> Tuple[str, Optional[str]]:
                        try:
                            p = await AWSIntegration.get_parameter(
                                cloud_auth, ctx.parameter_path(tenant.code, k)
                            )
                            return k, p.get("value")
                        except Exception:
                            # Not found (never deployed) / transient → treat as
                            # absent so the key is staged normally (never a no-op).
                            return k, None
                    for k, v in await asyncio.gather(
                        *[_read_param(k) for k in update_var_keys]
                    ):
                        if v is not None:
                            cloud_values[k] = v
                if update_secret_keys:
                    plain_map, _arn = await EnvironmentVariableHandler.get_component(
                        SecretProviderEnum.AWS_SECRETS_MANAGER, tenant.code
                    ).get_map(cloud_auth, ctx.secret_path(tenant.code))
                    for k, v in (plain_map or {}).items():
                        if k in update_secret_keys:
                            cloud_values[k] = v
                # Deployed baseline (devlift) — one state.json GET, secrets
                # KMS-decrypted. Used together with cloud so a drifted key is
                # never treated as a no-op.
                deployed = await self.read_deployed_sources_bulk(
                    ctx,
                    [(k, False) for k in update_var_keys]
                    + [(k, True) for k in update_secret_keys],
                    auth_config=storage_auth,
                )
                devlift_values = {k: v for k, (v, _ts) in deployed.items()}
            except Exception as exc:
                logger.warning("save: live cloud read for no-op check failed: %s", exc)
                cloud_values = {}
                devlift_values = {}
        noop_keys = {
            it.key
            for it in items
            if it.variable_code
            and it.operation != "delete"
            and not (it.old_key and it.old_key != it.key)
            and it.key in cloud_values
            and devlift_values.get(it.key) is not None
            and (it.plain_value() or "") == (cloud_values[it.key] or "")
            and (it.plain_value() or "") == (devlift_values[it.key] or "")
        }

        # ── Encrypt every secret value through ONE KMS client (one handshake).
        # Plain variables pass through untouched. Deletes carry no value.
        secret_positions = [
            i for i, it in enumerate(items)
            if it.type == "secret" and it.plain_value() and it.operation != "delete"
        ]
        staged_values: List[Any] = [it.plain_value() or None for it in items]
        if secret_positions:
            if not settings.secret_audit_kms_key_id:
                raise ValueError("SECRET_AUDIT_KMS_KEY_ID is not configured")
            ciphertexts = await AWSIntegration.kms_encrypt_many(
                storage_auth,
                settings.secret_audit_kms_key_id,
                [items[i].plain_value() for i in secret_positions],
            )
            for pos, ct in zip(secret_positions, ciphertexts):
                staged_values[pos] = ct  # may be an Exception — handled below

        # ── In-memory build loop — NO awaits, NO per-item DB round-trips ──
        # The duplicate-key pre-check (mirrors uq_variable_mst_owner_key) runs
        # off the owner rows loaded above: a new add (or a rename's new key) that
        # collides with a live row — or with another key earlier in this payload
        # — is rejected as a per-item error up front, instead of the DB unique
        # constraint failing the whole batch on INSERT.
        results: List[SaveVariableResult] = []
        staged_entries: List[dict] = []
        new_rows: List[VariableMstModel] = []
        update_dicts: List[dict] = []
        # Draft-only keys whose delete became a purge: draft entry dropped,
        # never-deployed row retired. Nothing reaches deploy.
        purge_keys: Set[str] = set()
        purge_codes: List[str] = []
        batch_keys: Set[str] = set()
        # Existing rows this save turns write-only; applied as their own UPDATE
        # batch so the flag is only ever written as True.
        write_only_codes: List[str] = []

        for item, sval in zip(items, staged_values):
            if isinstance(sval, Exception):
                logger.error("save: failed to encrypt '%s': %s", item.key, sval)
                results.append(
                    SaveVariableResult(key=item.key, status="error", error="Failed to encrypt value")
                )
                continue
            if item.operation != "delete":
                is_new = not item.variable_code
                is_rename = bool(item.old_key and item.old_key != item.key)
                if item.key in batch_keys or (is_rename and item.key in live_keys):
                    results.append(
                        SaveVariableResult(
                            key=item.key,
                            variable_code=item.variable_code,
                            status="error",
                            error="A variable with this key already exists for this resource",
                        )
                    )
                    continue
                if is_new and item.key in live_keys:
                    # Upsert-by-key: someone already created this key (their
                    # value sits in THEIR draft file; only rows are shared).
                    # Instead of erroring, stage this caller's value against
                    # the existing row — in this caller's own draft file.
                    row = row_by_key.get(item.key)
                    wanted = (
                        VariableTypeEnum.SECRET if item.type == "secret" else VariableTypeEnum.VARIABLE
                    )
                    if row is None or "is_draft_value" not in item.model_fields_set:
                        # Callers that never send is_draft_value (MCP, old
                        # clients) keep the strict duplicate error.
                        results.append(
                            SaveVariableResult(
                                key=item.key,
                                status="error",
                                error="A variable with this key already exists for this resource",
                            )
                        )
                        continue
                    if row.variable_type != wanted:
                        # secret-vs-variable decides WHICH AWS store the key
                        # deploys to — never silently merge across that line.
                        existing_kind = "secret" if row.variable_type == VariableTypeEnum.SECRET else "config variable"
                        results.append(
                            SaveVariableResult(
                                key=item.key,
                                status="error",
                                error=f"'{item.key}' already exists as a {existing_kind}",
                            )
                        )
                        continue
                    # Reuse the row (no INSERT), keep ITS reference metadata so
                    # a blind add can't strip a resource-group link, and derive
                    # the draft verdict server-side: both saves are un-deployed
                    # drafts, so the entry stays operation "add" downstream.
                    item = item.model_copy(update={
                        "variable_code": row.code,
                        "referenced_transaction_code": row.referenced_transaction_code,
                        "referenced_table_name": (
                            row.referenced_table_name.value
                            if row.referenced_table_name is not None
                            else None
                        ),
                        "is_draft_value": item.key not in deployed_keys,
                    })
                batch_keys.add(item.key)
            try:
                result, entry, new_row, update_dict, purge = self._build_save_entry(
                    item, sval, user, tenant, ctx, staged_prior,
                    deployed_keys, allow_draft_purge, write_only_keys,
                )
            except Exception as exc:
                logger.error("Failed to save variable '%s': %s", item.key, exc)
                results.append(
                    SaveVariableResult(key=item.key, status="error", error=str(exc))
                )
                continue
            results.append(result)
            if (
                item.is_write_only
                and item.variable_code
                and item.key not in write_only_keys
                and result.status == "success"
            ):
                write_only_codes.append(item.variable_code)
            if entry is not None:
                staged_entries.append(entry)
            if new_row is not None:
                new_rows.append(new_row)
            if update_dict is not None:
                update_dicts.append(update_dict)
            if purge is not None:
                purge_keys.add(purge["key"])
                purge_codes.append(purge["variable_code"])

        # Drop the no-op update entries — their value already matches what's LIVE
        # in AWS, so staging them would be a dead draft. The keys are also passed
        # to the writer as `remove_keys` so any PRIOR staged entry for the same
        # key (e.g. the earlier edit that is now reverted) is discarded too.
        if noop_keys:
            staged_entries = [
                e for e in staged_entries
                if not (
                    e.get("key") in noop_keys
                    and e.get("operation") == "update"
                    and not e.get("old_key")
                )
            ]

        # ── Batched DB writes: one INSERT batch (adds) + one UPDATE batch ──
        if new_rows:
            try:
                await self.variable_repo.bulk_add(new_rows)
            except IntegrityError:
                # Backstop for what the pre-check can't see (e.g. a concurrent
                # save inserting the same key between our SELECT and INSERT).
                # Retry per row under savepoints so only the colliding rows
                # fail — never bubble a 500 for the whole batch.
                await self.db.rollback()
                failed_codes: Set[str] = set()
                for row in new_rows:
                    try:
                        async with self.db.begin_nested():
                            self.db.add(row)
                            await self.db.flush()
                    except IntegrityError:
                        failed_codes.add(row.code)
                        logger.warning(
                            "save: duplicate key '%s' for %s:%s rejected by unique constraint",
                            row.key, row.table_name, row.transaction_code,
                        )
                if failed_codes:
                    results = [
                        r if r.variable_code not in failed_codes else SaveVariableResult(
                            key=r.key,
                            variable_code=r.variable_code,
                            operation=r.operation,
                            status="error",
                            error="A variable with this key already exists for this resource",
                        )
                        for r in results
                    ]
                    # Don't stage a draft for a row that was never created.
                    staged_entries = [
                        e for e in staged_entries
                        if e.get("variable_code") not in failed_codes
                    ]
        if update_dicts:
            await self.variable_repo.bulk_update_by_code(update_dicts)

        # Turn the flag on for existing rows. Separate batch (see _build_save_entry)
        # and never a False write, so the flag stays one-way.
        if write_only_codes:
            await self.variable_repo.bulk_update_by_code(
                [{"code": c, "is_write_only": True} for c in write_only_codes]
            )

        # Purged draft-only keys: the row was inserted by the first save and
        # never deployed — retire it so no ghost lingers in listings.
        if purge_codes:
            await self.variable_repo.bulk_update_by_code(
                [{"code": c, "is_deleted": True} for c in purge_codes]
            )

        staged_file = None
        if staged_entries or noop_keys or purge_keys:
            staged_file = await self._write_staged_file(
                ctx, user, tenant, items[0], staged_entries,
                remove_keys=(noop_keys | purge_keys),
            )

        await self.db.commit()
        return results, staged_file

    def _build_save_entry(
        self,
        item: SaveVariableItem,
        staged_value: Any,
        user: UserMstModel,
        tenant: TenantsMstModel,
        ctx: _PathContext,
        staged_prior: Dict[str, dict],
        deployed_keys: Set[str],
        allow_draft_purge: bool,
        write_only_keys: Set[str],
    ) -> Tuple[SaveVariableResult, Optional[dict], Optional[VariableMstModel], Optional[dict], Optional[dict]]:
        """Classify one item WITHOUT any per-item DB lookup and build its artifacts.

        Returns (result, staged_entry, new_row, update_dict, purge):
          - new_row      set for an add     → collected for one bulk INSERT
          - update_dict  set for an update  → collected for one bulk UPDATE by code
          - staged_entry set for everything that stages a value/change
          - purge        set for a delete of a draft-only key → drop the draft
                         entry + retire the never-deployed row; nothing staged

        Classification (no SELECT): delete op → delete; else variable_code
        present → update; else → add. Exception: a trusted is_draft_value item
        (a key that exists ONLY as the caller's un-deployed draft) stays "add"
        through edits and PURGES on delete — the draft must always be the diff
        vs the DEPLOYED baseline, not vs the previous draft.
        """
        table_enum = _to_table_enum(item.table_name)
        is_rename = bool(item.old_key and item.old_key != item.key)
        # The frontend computes is_draft_value from the same with-values
        # response that rendered the row (staged + never deployed + absent
        # from AWS). Never trusted alone: the owner rows fetched this save
        # must agree the key was never deployed (variable_cloud_identifier
        # unset), so a stale flag can never touch a deployed key.
        trusted_draft = (
            item.is_draft_value
            and allow_draft_purge
            and item.key not in deployed_keys
        )
        variable_type = (
            VariableTypeEnum.SECRET if item.type == "secret" else VariableTypeEnum.VARIABLE
        )

        # ── Write-only rules ──
        # The flag is MONOTONIC: it can only ever be turned on. An omitted field
        # leaves an existing row untouched (old clients keep working), while an
        # explicit false against a write-only row is a real attempt to expose the
        # value and is rejected outright.
        # On a rename the row is still filed under the OLD key — the DB key only
        # moves at deploy — so both names have to be consulted or a rename would
        # silently shed the flag's guards.
        was_write_only = item.key in write_only_keys or (
            is_rename and item.old_key in write_only_keys
        )
        if item.is_write_only and variable_type != VariableTypeEnum.SECRET:
            raise ValueError(
                "Write-only applies to secrets only — a config variable is stored "
                "in plaintext, so hiding its value would be misleading"
            )
        if (
            was_write_only
            and "is_write_only" in item.model_fields_set
            and not item.is_write_only
        ):
            raise ValueError(
                f"'{item.key}' is write-only and cannot be made readable again"
            )
        is_write_only = item.is_write_only or was_write_only
        if is_write_only and item.operation != "delete":
            # The client can never read the current value back, so the only value
            # it could echo is the mask — accepting it would overwrite the secret
            # with literal bullets.
            if (item.plain_value() or "") == WRITE_ONLY_MASK:
                raise ValueError(
                    f"'{item.key}' is write-only — its value is never shown. "
                    "Enter the new value in full or leave the variable untouched"
                )
            # An empty value on an existing key means "carry the deployed value
            # over" (see _deploy_one), which is how a rename or a plain flag flip
            # keeps working. On a NEW key there is nothing to carry over, so the
            # deploy would fail later with a far less obvious message.
            if not (item.plain_value() or "").strip() and not item.variable_code:
                raise ValueError(f"'{item.key}' is write-only — a value is required")

        referenced_table = (
            _to_table_enum(item.referenced_table_name) if item.referenced_table_name else None
        )
        file_path = (
            ctx.secret_path(tenant.code)
            if item.type == "secret"
            else ctx.parameter_path(tenant.code, item.key)
        )

        def _base_entry(operation: str, variable_code: str, old_key: Optional[str], new_value: Any) -> dict:
            entry = {
                "key": item.key,
                "old_key": old_key,
                # Transient, merge-only: which staged entry this one replaces.
                "_replaces_key": item.old_key if is_rename else None,
                "newValue": new_value,
                "operation": operation,
                "user_id": user.code,
                "user_mail": user.email_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "file_path": file_path,
                "type": item.type,
                "variable_code": variable_code,
            }
            if is_write_only:
                entry["write_only"] = True
            return entry

        # ── DELETE ── stage a delete op (applied on deploy); no DB write here.
        if item.operation == "delete":
            if not item.variable_code:
                # Nothing ever saved for this key → idempotent no-op.
                return (
                    SaveVariableResult(key=item.key, operation="delete", status="success"),
                    None, None, None, None,
                )
            if trusted_draft:
                # Draft-only key: deleting it means "never mind" — nothing is
                # staged; the draft entry is dropped from the bucket and the
                # never-deployed row retired. Deploy never sees the key
                # (nothing exists in AWS to remove or audit).
                return (
                    SaveVariableResult(
                        key=item.key, variable_code=item.variable_code,
                        operation="delete", status="success",
                    ),
                    None, None, None,
                    {"key": item.key, "variable_code": item.variable_code},
                )
            entry = _base_entry("delete", item.variable_code, old_key=None, new_value=None)
            # If this deletes a key that has an un-deployed staged add/edit (value
            # lives ONLY in the staged file), keep that prior entry for Revert.
            prior = staged_prior.get(item.key)
            if prior and prior.get("operation") != "delete":
                entry["_prev_staged"] = prior
            return (
                SaveVariableResult(
                    key=item.key, variable_code=item.variable_code, operation="delete", status="success"
                ),
                entry, None, None, None,
            )

        # ── UPDATE / RENAME ── existing variable (variable_code present).
        # No SELECT: update variable_type + referenced_* by code in one bulk
        # UPDATE. The row's key/name are left to deploy (mirrors AWS truth).
        # A trusted draft-only key stays operation "add" through value edits —
        # it has never deployed, so vs the deployed baseline it IS an add.
        if item.variable_code:
            op = "add" if trusted_draft else "update"
            entry = _base_entry(
                op,
                item.variable_code,
                old_key=item.old_key if is_rename else None,
                new_value=staged_value,
            )
            update_dict = {
                "code": item.variable_code,
                "variable_type": variable_type,
                "referenced_transaction_code": item.referenced_transaction_code,
                "referenced_table_name": referenced_table,
            }
            # is_write_only is deliberately NOT set here: bulk_update_by_code
            # runs one executemany, so every dict must have identical keys. The
            # flag is applied by its own batch in save_variables — which also
            # keeps it monotonic, since that batch only ever writes True.
            return (
                SaveVariableResult(
                    key=item.key, variable_code=item.variable_code, operation=op, status="success"
                ),
                entry, None, update_dict, None,
            )

        # ── ADD ── new variable (no variable_code) → INSERT a fresh row.
        new_code = f"VAR-{uuid4().hex[:12].upper()}"
        new_row = VariableMstModel(
            code=new_code,
            name=item.key,
            scope_type=VariableScopeTypeEnum.INFRA,
            table_name=table_enum,
            transaction_code=item.transaction_code,
            key=item.key,
            value=None,
            variable_type=variable_type,
            is_write_only=is_write_only,
            referenced_transaction_code=item.referenced_transaction_code,
            referenced_table_name=referenced_table,
            environments_enum=ctx.environment,
            tenants_mst_code=tenant.code,
        )
        entry = _base_entry("add", new_code, old_key=None, new_value=staged_value)
        return (
            SaveVariableResult(
                key=item.key, variable_code=new_code, operation="add", status="success"
            ),
            entry, new_row, None, None,
        )

    async def revert_staged(
        self,
        transaction_code: str,
        keys: List[str],
        environment: Optional[str],
        user: UserMstModel,
        tenant: TenantsMstModel,
    ) -> List[str]:
        """Discard the caller's un-deployed staged change for the given keys by
        removing their entries from the temp-bucket staged file (the file is
        deleted when nothing is left). Reverting a staged DELETE restores the row
        to its deployed state — the DB row and cloud value were never touched, so
        no restore is needed. Returns the keys that were actually reverted.
        """
        ctx = await self._resolve_path_context(transaction_code)
        if environment and ctx.environment.value != environment:
            raise ValueError(
                f"Environment mismatch: service_config is '{ctx.environment.value}', got '{environment}'"
            )
        if not settings.secret_temp_bucket:
            return []

        auth_config = self._get_storage_auth_config()
        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_temp_bucket
        staged_key = ctx.staged_file_key(user.email_id)

        raw = await FileManagerHandler.get_object(s3_auth, staged_key)
        if not raw:
            return []
        try:
            staged = json.loads(raw)
        except Exception:
            return []

        drop = set(keys)
        items = staged.get("items", [])
        reverted: List[str] = []
        remaining: List[dict] = []
        for it in items:
            if it.get("key") in drop:
                reverted.append(it.get("key"))
                # Restore the pre-delete staged entry (a never-deployed add/edit)
                # so its value comes back; a plain delete just drops out.
                prev = it.get("_prev_staged")
                if prev:
                    prev.pop("_prev_staged", None)
                    remaining.append(prev)
            else:
                remaining.append(it)
        if not reverted:
            return []

        if remaining:
            staged["items"] = remaining
            await FileManagerHandler.put_object(
                auth_config=s3_auth,
                key=staged_key,
                content=json.dumps(staged, indent=2),
                content_type="application/json",
            )
        else:
            await FileManagerHandler.delete_object(s3_auth, staged_key)
        return reverted

    async def _read_staged_items(
        self,
        ctx: _PathContext,
        user: UserMstModel,
        auth_config: dict,
    ) -> Dict[str, dict]:
        """The caller's staged entries keyed by staged key (empty when none)."""
        if not settings.secret_temp_bucket:
            return {}
        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_temp_bucket
        try:
            raw = await FileManagerHandler.get_object(
                s3_auth, ctx.staged_file_key(user.email_id)
            )
            if not raw:
                return {}
            return {
                e["key"]: e for e in json.loads(raw).get("items", []) if e.get("key")
            }
        except Exception as exc:
            logger.warning("staged file read failed: %s", exc)
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # LEGACY (unused): per-item save path. Replaced by the batched save_variables
    # + _build_save_entry above. Kept commented for reference; safe to remove.
    # ─────────────────────────────────────────────────────────────────────────
#     async def _save_one(
#         self,
#         item: SaveVariableItem,
#         user: UserMstModel,
#         tenant: TenantsMstModel,
#         ctx: _PathContext,
#         storage_auth: dict,
#         staged_prior: Optional[Dict[str, dict]] = None,
#     ) -> Tuple[SaveVariableResult, Optional[dict]]:
#         table_enum = _to_table_enum(item.table_name)
#         if table_enum != WorkflowSourceTableEnum.SERVICE_CONFIG:
#             raise ValueError(
#                 f"Only service_config owners are supported for now, got '{item.table_name}'"
#             )
# 
#         # ── Staged deletion ──
#         # Stage a 'delete' op (applied on deploy). The DB row is NOT soft-deleted
#         # here — deploy runs the teardown. The staged entry supersedes any pending
#         # add/edit for the same key (merge-by-key). No row → idempotent no-op.
#         if item.operation == "delete":
#             existing = await self.variable_repo.get_by_transaction_and_key(
#                 table_name=table_enum,
#                 transaction_code=item.transaction_code,
#                 key=item.key,
#                 environment=ctx.environment,
#             )
#             if not existing:
#                 return SaveVariableResult(key=item.key, operation="delete", status="success"), None
#             entry = {
#                 "key": item.key,
#                 "old_key": None,
#                 "_replaces_key": None,
#                 "newValue": None,
#                 "operation": "delete",
#                 "user_id": user.code,
#                 "user_mail": user.email_id,
#                 "timestamp": datetime.now(timezone.utc).isoformat(),
#                 "file_path": (
#                     ctx.secret_path(tenant.code)
#                     if item.type == "secret"
#                     else ctx.parameter_path(tenant.code, item.key)
#                 ),
#                 "type": item.type,
#                 "variable_code": existing.code,
#             }
#             # If this deletes a variable that has an un-deployed staged add/edit
#             # (its value lives ONLY in the staged file — no copy in the audit
#             # bucket), keep that prior entry so a Revert can restore the value.
#             prior = (staged_prior or {}).get(item.key)
#             if prior and prior.get("operation") != "delete":
#                 entry["_prev_staged"] = prior
#             return (
#                 SaveVariableResult(
#                     key=item.key, variable_code=existing.code, operation="delete", status="success"
#                 ),
#                 entry,
#             )
# 
#         variable_type = (
#             VariableTypeEnum.SECRET if item.type == "secret" else VariableTypeEnum.VARIABLE
#         )
#         referenced_table = (
#             _to_table_enum(item.referenced_table_name) if item.referenced_table_name else None
#         )
# 
#         # ── DB save (value intentionally NOT stored) ──
#         # Rename: locate the row by its previous key first
#         is_rename = bool(item.old_key and item.old_key != item.key)
#         existing = None
#         if is_rename:
#             existing = await self.variable_repo.get_by_transaction_and_key(
#                 table_name=table_enum,
#                 transaction_code=item.transaction_code,
#                 key=item.old_key,
#                 environment=ctx.environment,
#             )
#             # The visible old_key may itself be a staged (un-deployed) rename;
#             # the DB row still holds the deployed key — resolve it through the
#             # staged entry's variable_code.
#             if not existing and staged_prior:
#                 prior = staged_prior.get(item.old_key)
#                 if prior and prior.get("variable_code"):
#                     candidate = await self.variable_repo.get_by(
#                         code=prior["variable_code"]
#                     )
#                     if candidate and not candidate.is_deleted:
#                         existing = candidate
#         if not existing:
#             existing = await self.variable_repo.get_by_transaction_and_key(
#                 table_name=table_enum,
#                 transaction_code=item.transaction_code,
#                 key=item.key,
#                 environment=ctx.environment,
#             )
#         # Plain save of a key that is itself a staged rename (row still holds
#         # the deployed key) — resolve through the staged entry, else a
#         # duplicate row would be created.
#         if not existing and staged_prior:
#             prior = staged_prior.get(item.key)
#             if prior and prior.get("variable_code"):
#                 candidate = await self.variable_repo.get_by(code=prior["variable_code"])
#                 if candidate and not candidate.is_deleted:
#                     existing = candidate
# 
#         # Rename collision check moved to the frontend (canvas state has all
#         # variable names, so the check is free there — no DB round-trip needed).
#         # if is_rename and existing and existing.key != item.key:
#         #     dup = await self.variable_repo.get_by_transaction_and_key(
#         #         table_name=table_enum,
#         #         transaction_code=item.transaction_code,
#         #         key=item.key,
#         #         environment=ctx.environment,
#         #     )
#         #     if dup and dup.id != existing.id:
#         #         raise ValueError(
#         #             f"A variable named '{item.key}' already exists on this resource"
#         #         )
# 
#         deployed_key = existing.key if existing else None
#         if existing:
#             # 'update' only when the variable is live in AWS (deployed at least
#             # once — variable_cloud_identifier set). A row created by an earlier
#             # save that was never deployed is still an 'add'.
#             operation = "update" if existing.variable_cloud_identifier else "add"
#             fields = {
#                 "variable_type": variable_type,
#                 "referenced_transaction_code": item.referenced_transaction_code,
#                 "referenced_table_name": referenced_table,
#             }
#             # A deployed row keeps its deployed key until the rename is
#             # deployed — the shared row must mirror AWS truth; the rename
#             # stays private in the caller's staged file. Un-deployed rows are
#             # invisible to other users, so their key can move immediately.
#             if not existing.variable_cloud_identifier:
#                 fields["key"] = item.key
#                 fields["name"] = item.key
#             row = await self.variable_repo.update(existing, fields)
#         else:
#             operation = "add"
#             row = await self.variable_repo.create(
#                 code=f"VAR-{uuid4().hex[:12].upper()}",
#                 name=item.key,
#                 scope_type=VariableScopeTypeEnum.INFRA,
#                 table_name=table_enum,
#                 transaction_code=item.transaction_code,
#                 key=item.key,
#                 value=None,
#                 variable_type=variable_type,
#                 referenced_transaction_code=item.referenced_transaction_code,
#                 referenced_table_name=referenced_table,
#                 environments_enum=ctx.environment,
#                 tenants_mst_code=tenant.code,
#             )
# 
#         # ── Staged entry (same shape as audit JSON, no oldValue) ──
#         # Empty value on a rename = keep the live value; deploy carries it over.
#         new_value = item.value or None
#         if new_value is not None and item.type == "secret":
#             new_value = await self._encrypt(storage_auth, new_value)
# 
#         entry = {
#             "key": item.key,
#             # old_key drives the deploy-side rename (audit delete + AWS move).
#             # Only meaningful when the old key is LIVE in AWS — a rename of a
#             # never-deployed variable is just an add under the new key. Always
#             # the DEPLOYED key (rename chains collapse to deployed -> newest).
#             "old_key": (
#                 deployed_key
#                 if (operation == "update" and deployed_key and deployed_key != item.key)
#                 else None
#             ),
#             # Transient, merge-only: which staged entry this one replaces.
#             "_replaces_key": item.old_key if is_rename else None,
#             "newValue": new_value,
#             "operation": operation,
#             "user_id": user.code,
#             "user_mail": user.email_id,
#             "timestamp": datetime.now(timezone.utc).isoformat(),
#             "file_path": (
#                 ctx.secret_path(tenant.code)
#                 if item.type == "secret"
#                 else ctx.parameter_path(tenant.code, item.key)
#             ),
#             "type": item.type,
#             "variable_code": row.code,
#         }
# 
#         result = SaveVariableResult(
#             key=item.key,
#             variable_code=row.code,
#             operation=operation,
#             status="success",
#         )
#         return result, entry

    async def _write_staged_file(
        self,
        ctx: _PathContext,
        user: UserMstModel,
        tenant: TenantsMstModel,
        first_item: SaveVariableItem,
        entries: List[dict],
        remove_keys: Optional[Set[str]] = None,
    ) -> str:
        if not settings.secret_temp_bucket:
            raise ValueError("SECRET_TEMP_BUCKET is not configured")

        s3_auth = dict(self._get_storage_auth_config())
        s3_auth["bucket"] = settings.secret_temp_bucket

        staged_key = ctx.staged_file_key(user.email_id)

        # The frontend sends only the CHANGED items, so merge with any
        # existing staged (not-yet-deployed) entries instead of overwriting —
        # upsert by key, and a rename replaces its old key's pending entry.
        merged_by_key: dict = {}
        existing_raw = await FileManagerHandler.get_object(s3_auth, staged_key)
        if existing_raw:
            try:
                for old_entry in json.loads(existing_raw).get("items", []):
                    merged_by_key[old_entry.get("key")] = old_entry
            except Exception:
                logger.warning("Unparseable staged file '%s' — starting fresh", staged_key)
        for e in entries:
            replaces = e.pop("_replaces_key", None)
            if replaces:
                merged_by_key.pop(replaces, None)
            merged_by_key[e["key"]] = e

        # No-op edits (value reverted to what's live in AWS): drop the key from
        # the staged file entirely so nothing dead lingers for the next deploy.
        for k in (remove_keys or set()):
            merged_by_key.pop(k, None)

        # Nothing left staged → delete the file (mirrors revert_staged) so the
        # bucket doesn't keep an empty draft.
        if not merged_by_key:
            await FileManagerHandler.delete_object(s3_auth, staged_key)
            return staged_key

        payload = {
            "transaction_code": first_item.transaction_code,
            "table_name": first_item.table_name,
            "environment": ctx.environment.value,
            "staged_by": user.email_id,
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "items": list(merged_by_key.values()),
        }
        await FileManagerHandler.put_object(
            auth_config=s3_auth,
            key=staged_key,
            content=json.dumps(payload, indent=2),
            content_type="application/json",
        )
        return staged_key

    # ══════════════════════════════════════════════════════════════════════════
    # API — clone (assisted manual entry: stage the values the user chose to copy)
    # ══════════════════════════════════════════════════════════════════════════

    async def clone_variables(
        self,
        request: CloneVariablesRequest,
        user: UserMstModel,
        tenant: TenantsMstModel,
    ) -> Tuple[List[CloneVariableResult], Optional[str]]:
        """Clone variables onto a target resource (assisted manual entry).

        The frontend sends the values the user saw and chose to copy (WYSIWYG),
        so there is no source re-read here — same trust model as a normal save.
        Secrets are KMS-encrypted (bounded-concurrent) and staged against the
        TARGET; a variable_mst row is created/updated per key. Values reach
        SSM / Secrets Manager only when the target is deployed."""
        target_table = _to_table_enum(request.table_name)
        source_table = _to_table_enum(request.source_table_name)
        if target_table != WorkflowSourceTableEnum.SERVICE_CONFIG or source_table != WorkflowSourceTableEnum.SERVICE_CONFIG:
            raise ValueError("Clone currently supports service_config resources only")

        target_ctx = await self._resolve_path_context(request.transaction_code)

        # Variables are disabled in production — never clone them into a prod target.
        if target_ctx.environment == EnvironmentEnum.prod:
            raise ValueError("Cloning variables into a production service is not allowed")

        storage_auth = self._get_storage_auth_config()

        # Dedup by key (last wins) and drop empties — an empty value can't be cloned.
        items_by_key: Dict[str, CloneVariableItem] = {}
        results: List[CloneVariableResult] = []
        for it in request.items:
            if not it.key:
                continue
            if not it.plain_value():
                results.append(CloneVariableResult(key=it.key, status="error", error="Value is empty"))
                continue
            items_by_key[it.key] = it

        # Keys with a staged (un-deployed) rename on the target are blocked: the
        # clone would stage a second entry against the same DB row and the deploy
        # outcome would depend on entry order.
        staged_items = await self._read_staged_items(target_ctx, user, storage_auth)
        rename_by_key: Dict[str, str] = {}
        for e in staged_items.values():
            old_key = e.get("old_key")
            if old_key and old_key != e.get("key"):
                rename_by_key[old_key] = f"{old_key} → {e['key']}"
                rename_by_key[e["key"]] = f"{old_key} → {e['key']}"

        # A write-only key's value is never readable, so the value the client
        # says it copied cannot have come from the source. Reject rather than
        # stage whatever was sent. The pickers already hide these keys; this is
        # the guard that actually enforces it.
        source_write_only: Set[str] = set()
        try:
            source_rows = await self.variable_repo.get_by_owner(
                source_table, request.source_transaction_code
            )
            source_write_only = {r.key for r in source_rows if r.is_write_only}
        except Exception as exc:
            logger.warning("clone: source write-only lookup failed: %s", exc)

        to_process: List[CloneVariableItem] = []
        for key, it in items_by_key.items():
            if key in source_write_only:
                results.append(
                    CloneVariableResult(
                        key=key,
                        status="error",
                        error=f"'{key}' is write-only on the source service and cannot be cloned",
                    )
                )
                continue
            if key in rename_by_key:
                results.append(
                    CloneVariableResult(
                        key=key,
                        status="error",
                        error=(
                            f"'{key}' has a pending rename ({rename_by_key[key]}) on this "
                            f"service — deploy or discard it before cloning this key"
                        ),
                    )
                )
                continue
            to_process.append(it)

        # Encrypt all secrets through ONE KMS client (see kms_encrypt_many) —
        # per-value client construction is what makes a large clone take minutes.
        # Plain values pass through untouched. DB work stays sequential below
        # since one AsyncSession is not safe for concurrent statements.
        secret_positions = [i for i, it in enumerate(to_process) if it.type == "secret"]
        staged_values: List[Any] = [it.plain_value() for it in to_process]
        if secret_positions:
            if not settings.secret_audit_kms_key_id:
                raise ValueError("SECRET_AUDIT_KMS_KEY_ID is not configured")
            ciphertexts = await AWSIntegration.kms_encrypt_many(
                storage_auth,
                settings.secret_audit_kms_key_id,
                [to_process[i].plain_value() for i in secret_positions],
            )
            for pos, ct in zip(secret_positions, ciphertexts):
                staged_values[pos] = ct

        # One bulk SELECT for every target key (uniqueness is per table +
        # transaction_code + key + env) instead of a per-key lookup.
        existing_by_key = await self.variable_repo.get_existing_by_keys(
            target_table,
            request.transaction_code,
            target_ctx.environment,
            [it.key for it in to_process],
        )

        # Build every DB row and staged entry in memory — no awaits, no per-row
        # DB round-trips. Encrypt/build failures stay per-item (excluded from the
        # batch); the DB batch itself is atomic (all-or-nothing) below.
        staged_entries: List[dict] = []
        new_rows: List[VariableMstModel] = []
        for it, sval in zip(to_process, staged_values):
            if isinstance(sval, Exception):
                logger.error("clone: failed to encrypt '%s': %s", it.key, sval)
                results.append(CloneVariableResult(key=it.key, status="error", error="Failed to encrypt value"))
                continue
            try:
                result, entry, new_row = self._build_clone_entry(
                    item=it,
                    staged_value=sval,
                    existing=existing_by_key.get(it.key),
                    target_ctx=target_ctx,
                    target_table=target_table,
                    target_code=request.transaction_code,
                    user=user,
                    tenant=tenant,
                )
            except Exception as exc:
                logger.error("clone: failed to stage '%s': %s", it.key, exc)
                results.append(CloneVariableResult(key=it.key, status="error", error=str(exc)))
                continue
            results.append(result)
            staged_entries.append(entry)
            if new_row is not None:
                new_rows.append(new_row)

        if not staged_entries:
            return results, None

        # Atomic: one pipelined INSERT for new rows (the same flush also persists
        # the in-place updates to existing rows), then the S3 write, then commit.
        # Any failure rolls the whole batch back — no rows without staged values,
        # no partially-staged clone.
        try:
            try:
                await self.variable_repo.bulk_add(new_rows)
            except IntegrityError:
                # uq_variable_mst_owner_key: a concurrent save/clone created one
                # of the "new" keys after the existing-keys lookup above. The
                # clone batch is deliberately all-or-nothing (a partial retry
                # would also lose the in-place updates flushed alongside the
                # inserts), so abort cleanly with retryable per-item errors
                # instead of bubbling a 500. A retry finds the new row via the
                # lookup and updates it in place.
                await self.db.rollback()
                logger.warning(
                    "clone: unique-key collision on '%s' — concurrent change, batch aborted",
                    request.transaction_code,
                )
                retry_error = (
                    "A concurrent change created one of these keys — please retry the clone"
                )
                return [
                    r if r.status == "error" else CloneVariableResult(
                        key=r.key,
                        variable_code=r.variable_code,
                        operation=r.operation,
                        type=r.type,
                        status="error",
                        error=retry_error,
                    )
                    for r in results
                ], None

            # Synthetic item carries the staged-file header (target codes);
            # _write_staged_file merges these entries with any pending ones.
            header = SaveVariableItem(
                transaction_code=request.transaction_code,
                table_name=request.table_name,
                key=staged_entries[0]["key"],
                value="",
                type="variable",
            )
            staged_file = await self._write_staged_file(
                target_ctx, user, tenant, header, staged_entries
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return results, staged_file

    def _build_clone_entry(
        self,
        item: CloneVariableItem,
        staged_value: str,
        existing: Optional[VariableMstModel],
        target_ctx: _PathContext,
        target_table: WorkflowSourceTableEnum,
        target_code: str,
        user: UserMstModel,
        tenant: TenantsMstModel,
    ) -> Tuple[CloneVariableResult, dict, Optional[VariableMstModel]]:
        """Pure in-memory builder (no DB I/O). Returns the result, the staged
        entry, and a new row to insert (None when an existing row is updated
        in place). The caller flushes all new rows in one batch."""
        key = item.key
        is_secret = item.type == "secret"
        item_type = "secret" if is_secret else "variable"
        variable_type = VariableTypeEnum.SECRET if is_secret else VariableTypeEnum.VARIABLE

        # Value is intentionally NOT stored in the row. A cloned value is a
        # concrete value, so any prior reference on the key is cleared.
        new_row: Optional[VariableMstModel] = None
        if existing is not None:
            if existing.is_write_only and not is_secret:
                # Cloning a config variable over a write-only secret would leave
                # a write-only plaintext SSM parameter — hidden in the UI but
                # readable in AWS. Overwriting it with a secret stays allowed.
                raise ValueError(
                    f"'{key}' already exists as a write-only secret on the target "
                    f"service and cannot be replaced by a config variable"
                )
            operation = "update" if existing.variable_cloud_identifier else "add"
            existing.variable_type = variable_type
            existing.referenced_transaction_code = None
            existing.referenced_table_name = None
            row = existing
        else:
            operation = "add"
            row = VariableMstModel(
                code=f"VAR-{uuid4().hex[:12].upper()}",
                name=key,
                scope_type=VariableScopeTypeEnum.INFRA,
                table_name=target_table,
                transaction_code=target_code,
                key=key,
                value=None,
                variable_type=variable_type,
                referenced_transaction_code=None,
                referenced_table_name=None,
                environments_enum=target_ctx.environment,
                tenants_mst_code=tenant.code,
            )
            new_row = row

        entry = {
            "key": key,
            "old_key": None,
            "newValue": staged_value,
            "operation": operation,
            "user_id": user.code,
            "user_mail": user.email_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_path": (
                target_ctx.secret_path(tenant.code)
                if item_type == "secret"
                else target_ctx.parameter_path(tenant.code, key)
            ),
            "type": item_type,
            "variable_code": row.code,
        }
        if existing is not None and existing.is_write_only:
            entry["write_only"] = True
        result = CloneVariableResult(
            key=key,
            variable_code=row.code,
            operation=operation,
            type=item_type,
            status="success",
        )
        return result, entry, new_row

    async def _latest_audit_entry(
        self,
        audit_auth: dict,
        ctx: _PathContext,
        key: str,
    ) -> Optional[dict]:
        """Newest audit entry for a key that carries a value.

        The key may be a secret or a plain config, and the caller doesn't know
        which until the entry is read, so both audit folders are checked. Walks
        versions downward so a trailing 'delete' entry (rename/close) falls back
        to the last entry that actually had a newValue."""
        for is_secret in (True, False):
            prefix = ctx.audit_key_prefix(key, is_secret)
            objects = await FileManagerHandler.list_objects(audit_auth, prefix)
            versions = sorted(
                (int(m.group(1)) for m in (_VERSION_RE.search(k) for k in objects) if m),
                reverse=True,
            )
            for version in versions:
                raw = await FileManagerHandler.get_object(audit_auth, f"{prefix}version-{version}.json")
                if raw is None:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                # Delete entries close a key's history (rename/close) — they may
                # carry the closing value in newValue but are not a live value.
                if entry.get("operation") == "delete":
                    continue
                if entry.get("newValue") is not None:
                    return entry
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # API — stage-sync (resolve manual AWS drift through the draft + deploy flow)
    # ══════════════════════════════════════════════════════════════════════════

    async def stage_sync_values(
        self,
        request: StageSyncRequest,
        user: UserMstModel,
        tenant: TenantsMstModel,
    ) -> Tuple[List[StageSyncResult], Optional[str]]:
        """Stage a drift resolution for each key as a normal draft entry.

        restore_devlift — value comes from the latest audit entry (secrets stay
        KMS-encrypted). accept_aws — value is read live from SSM / Secrets
        Manager (secrets re-encrypted for staging). Either way nothing touches
        AWS or the audit trail here; deploy applies the staged entries and
        journals the change."""
        table = _to_table_enum(request.table_name)
        if table != WorkflowSourceTableEnum.SERVICE_CONFIG:
            raise ValueError("stage-sync currently supports service_config resources only")

        ctx = await self._resolve_path_context(request.transaction_code)
        auth_config = self._get_storage_auth_config()
        audit_auth = dict(auth_config)
        audit_auth["bucket"] = settings.secret_audit_bucket

        # A key with a staged VALUE (edit or delete) is off-limits: syncing
        # would silently replace that intent. A rename-only draft is allowed —
        # the synced value merges INTO the rename entry, otherwise deploy
        # would silently carry the drifted AWS value over to the new name.
        staged_items = await self._read_staged_items(ctx, user, auth_config)
        old_key_owner: Dict[str, dict] = {
            e["old_key"]: e for e in staged_items.values() if e.get("old_key")
        }

        # Write-only keys carry no drift verdict — /with-values never compares
        # their values — so there is nothing to sync. Both directions would also
        # move a readable value around on behalf of a key that must not be read.
        write_only_keys: Set[str] = set()
        try:
            owner_rows = await self.variable_repo.get_by_owner(table, request.transaction_code)
            write_only_keys = {r.key for r in owner_rows if r.is_write_only}
        except Exception as exc:
            logger.warning("stage-sync: write-only lookup failed: %s", exc)

        results: List[StageSyncResult] = []
        staged_entries: List[dict] = []
        for key in request.keys:
            try:
                if key in write_only_keys:
                    raise ValueError(
                        f"'{key}' is write-only — its value is not tracked for drift. "
                        "Set a new value instead"
                    )
                prior = staged_items.get(key)
                if prior is None and key in old_key_owner:
                    raise ValueError(
                        f"'{key}' has a pending rename "
                        f"({key} → {old_key_owner[key]['key']}) — sync it under the new name"
                    )
                rename_only = (
                    prior is not None
                    and prior.get("old_key")
                    and prior.get("newValue") is None
                    and prior.get("operation") != "delete"
                )
                if prior is not None and not rename_only:
                    raise ValueError(
                        f"'{key}' already has a pending draft — deploy or discard it first"
                    )
                entry = await self._stage_sync_one(
                    key, request.direction, ctx, table,
                    request.transaction_code, user, tenant, auth_config, audit_auth,
                    rename_entry=prior if rename_only else None,
                )
                staged_entries.append(entry)
                results.append(StageSyncResult(key=key, status="success"))
            except Exception as exc:
                logger.error("Failed to stage sync for '%s': %s", key, exc)
                results.append(StageSyncResult(key=key, status="error", error=str(exc)))

        staged_file = None
        if staged_entries:
            header = SaveVariableItem(
                transaction_code=request.transaction_code,
                table_name=request.table_name,
                key=staged_entries[0]["key"],
                value="",
                type="variable",
            )
            staged_file = await self._write_staged_file(
                ctx, user, tenant, header, staged_entries
            )
        return results, staged_file

    async def _stage_sync_one(
        self,
        key: str,
        direction: str,
        ctx: _PathContext,
        table: WorkflowSourceTableEnum,
        transaction_code: str,
        user: UserMstModel,
        tenant: TenantsMstModel,
        auth_config: dict,
        audit_auth: dict,
        rename_entry: Optional[dict] = None,
    ) -> dict:
        row = await self.variable_repo.get_by_transaction_and_key(
            table_name=table,
            transaction_code=transaction_code,
            key=key,
            environment=ctx.environment,
        )
        # A staged rename shows the NEW name while the DB row still holds the
        # deployed key — resolve through the rename entry's variable_code.
        if not row and rename_entry and rename_entry.get("variable_code"):
            candidate = await self.variable_repo.get_by(code=rename_entry["variable_code"])
            if candidate and not candidate.is_deleted:
                row = candidate
        if not row or row.is_deleted:
            raise ValueError(f"'{key}' does not exist on this resource")

        # Audit history and the live AWS value both live under the DEPLOYED
        # key, which differs from `key` while a rename is staged.
        deployed_key = row.key
        is_secret = row.variable_type == VariableTypeEnum.SECRET
        item_type = "secret" if is_secret else "variable"

        if direction == "restore_devlift":
            latest = await self._latest_audit_entry(audit_auth, ctx, deployed_key)
            if latest is None:
                raise ValueError(
                    f"'{key}' has no deployed value in DevLift to restore"
                )
            item_type = latest.get("type", item_type)
            new_value = latest["newValue"]  # secrets: audit ciphertext as-is
        else:  # accept_aws
            if not row.variable_cloud_identifier:
                raise ValueError(f"'{key}' is not live in AWS — nothing to accept")
            component = EnvironmentVariableHandler.get_component(
                row.secret_provider
                or (
                    SecretProviderEnum.AWS_SECRETS_MANAGER
                    if is_secret else SecretProviderEnum.AWS_SSM
                ),
                tenant.code,
            )
            live = await component.get_value(
                auth_config, row.variable_cloud_identifier, key=deployed_key
            )
            if live is None:
                raise ValueError(
                    f"'{key}' has no live value in AWS — nothing to accept"
                )
            new_value = await self._encrypt(auth_config, live) if is_secret else live

        # Rename-only draft: merge the synced value into the rename entry so
        # deploy applies the rename and the chosen value in one operation.
        if rename_entry is not None:
            return {
                **rename_entry,
                "newValue": new_value,
                "type": item_type,
                "user_id": user.code,
                "user_mail": user.email_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        return {
            "key": key,
            "old_key": None,
            "newValue": new_value,
            "operation": "update" if row.variable_cloud_identifier else "add",
            "user_id": user.code,
            "user_mail": user.email_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_path": (
                ctx.secret_path(tenant.code)
                if item_type == "secret"
                else ctx.parameter_path(tenant.code, key)
            ),
            "type": item_type,
            "variable_code": row.code,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # API 2 — deploy (audit trail + Secrets Manager / SSM)
    # ══════════════════════════════════════════════════════════════════════════

    # ── DISABLED IN OBS_TOOL ─────────────────────────────────────────────────
    # Variable deploy EXECUTES ONLY in devlift-secret-config-manager (the
    # auditable service): the Temporal deploy_variables_activity calls its
    # POST /api/v1/internal/variable-deploy, and the frontend's direct
    # fallback calls its /resource-variable/deploy. The whole method is
    # commented out so obs_tool structurally cannot run the secret-handling
    # deploy — kept below for reference against the service's copy.
#     async def deploy_variables(
#         self,
#         transaction_code: str,
#         environment: Optional[str],
#         user: UserMstModel,
#         tenant: TenantsMstModel,
#         table_name: Optional[str] = None,
#         progress: Optional[Callable[[str], Awaitable[None]]] = None,
#     ) -> Tuple[List[DeployVariableResult], Optional[str]]:
#         """Deploy staged variables in an optimised batch flow.
#
#         progress: optional async hook awaited at milestone boundaries with one of
#           'audit_trail_saved' | 'starting_secrets_deployment' | 'secrets_deployed'
#           | 'starting_config_deployment' | 'configs_deployed'.
#         The Temporal deploy activity uses it to record pipeline stages; the plain
#         HTTP deploy passes None. Hook failures are logged and swallowed — progress
#         reporting must never fail a deploy.
#
#         Setup (done once upfront, zero per-item AWS/DB calls in the loop):
#           1.  Read staged file from temp bucket.
#           2.  Read state.json from state bucket (latest audit version per key).
#           3.  Bulk-fetch current AWS values:
#                 secrets   → one SM  GetSecretValue  (consolidated map)
#                 variables → one SSM GetParametersByPath (all keys under prefix)
#           4.  KMS-decrypt new secret values — parallel batches of 100.
#           5.  KMS-encrypt old secret values for audit — parallel batches of 100.
#           6.  Pre-encrypt rename-carry-over secret audit values — parallel.
#
#         Loop (pure in-memory):
#           - Build audit task per key from pre-fetched maps.
#           - Apply secret changes to in-memory SM map.
#           - Collect SSM puts/deletes, DB updates, state updates.
#
#         Flush (after loop):
#           7.  Parallel S3 PUTs — all audit entries (before AWS writes).
#           8.  SM  — one PutSecretValue (full updated in-memory map).
#           9.  SSM — parallel PutParameter + parallel deletes.
#           10. Bulk DB update (identifier, key, provider, type, refs).
#           11. Soft-delete DB rows for staged deletions.
#           12. Upsert canvas edges for referenced variables.
#           13. Write updated state.json to state bucket.
#           14. db.commit().
#           15. Delete staged file (only on full success).
#         """
#         ctx = await self._resolve_path_context(transaction_code)
#         if environment and ctx.environment.value != environment:
#             raise ValueError(
#                 f"Environment mismatch: service_config is '{ctx.environment.value}', got '{environment}'"
#             )
#
#         async def _progress(stage: str) -> None:
#             if progress is None:
#                 return
#             try:
#                 await progress(stage)
#             except Exception as exc:
#                 logger.warning("deploy progress hook '%s' failed: %s", stage, exc)
#
#         storage_auth = self._get_storage_auth_config()
#         cloud_auth   = await self._get_cloud_auth_config(ctx, tenant.code)
#         temp_auth    = {**storage_auth, "bucket": settings.secret_temp_bucket}
#         state_auth   = {**storage_auth, "bucket": settings.secret_state_bucket}
#
#         # ── 1. Read staged file ───────────────────────────────────────────────
#         staged_key = ctx.staged_file_key(user.email_id)
#         raw = await FileManagerHandler.get_object(temp_auth, staged_key)
#         if raw is None:
#             raise ValueError(f"No staged variables found at '{staged_key}' — run save first")
#         staged     = json.loads(raw)
#         table_enum = _to_table_enum(table_name or staged.get("table_name", "service_config"))
#         entries: List[dict] = staged.get("items", [])
#
#         secret_entries   = [e for e in entries if e.get("type") == "secret"]
#         variable_entries = [e for e in entries if e.get("type") != "secret"]
#
#         # One batched fetch of the DB rows (replaces N per-item get_by(code=...)).
#         # Only needed for referenced_* (canvas edge) — operation comes from the
#         # staged entry. Kept as a fallback for operation inference on old files.
#         row_codes = [e.get("variable_code") for e in entries if e.get("variable_code")]
#         rows = await self.variable_repo.get_by_codes(row_codes)
#         row_map: Dict[str, VariableMstModel] = {r.code: r for r in rows}
#
#         # DB reads are done; the DB isn't touched again until step 10. End the
#         # read transaction so the connection returns to the pool instead of
#         # sitting idle-in-transaction through the AWS phase (KMS/S3/SM/SSM can
#         # take minutes) and getting reset by an idle timeout. Step 10 then
#         # checks out a fresh pre-pinged connection. No writes exist yet, and
#         # expire_on_commit=False keeps row_map attributes loaded.
#         await self.db.commit()
#
#         # ── 2. Read state.json (latest audit version per key) ─────────────────
#         state_key = ctx.state_file_key()
#         state_raw = await FileManagerHandler.get_object(state_auth, state_key)
#         state_map: Dict[str, dict] = {}
#         if state_raw:
#             try:
#                 for item in json.loads(state_raw).get("items", []):
#                     if item.get("key"):
#                         state_map[item["key"]] = item
#             except Exception:
#                 logger.warning("state.json parse failed at '%s' — starting fresh", state_key)
#
#         # ── 3. Bulk-fetch current AWS values ──────────────────────────────────
#         # new_secret_map  {key: plain}      — KMS-decrypted values to push to SM
#         # old_secret_map  {key: encrypted}  — KMS-encrypted current SM values for audit
#         # old_parameter_map {key: plain}    — current SSM values for audit + carry-over
#         current_secret_plain: Dict[str, str] = {}
#         secret_arn: Optional[str] = None
#         if secret_entries:
#             plain_map, secret_arn = await EnvironmentVariableHandler.get_component(
#                 SecretProviderEnum.AWS_SECRETS_MANAGER, tenant.code
#             ).get_map(cloud_auth, ctx.secret_path(tenant.code))
#             current_secret_plain = plain_map or {}
#             # Terraform (infra deploy) owns creating the secret — fail here,
#             # BEFORE any write (audit / SM / SSM / DB), so a retry after the
#             # infra deploy picks up the intact staged file.
#             if not secret_arn:
#                 raise RuntimeError(
#                     f"Secrets Manager secret '{ctx.secret_path(tenant.code)}' not found"
#                 )
#
#         old_parameter_map: Dict[str, str] = {}
#         if variable_entries:
#             params = await AWSIntegration.get_parameters_by_path(
#                 cloud_auth, ctx.parameter_prefix(tenant.code)
#             )
#             old_parameter_map = {
#                 p["name"].rsplit("/", 1)[-1]: p["value"]
#                 for p in (params or []) if p.get("name")
#             }
#
#         # ── 4. KMS-decrypt new staged secret values over ONE client ──────────
#         # kms_decrypt_bulk reuses a single KMS connection for every blob.
#         new_secret_map: Dict[str, str] = {}
#         to_decrypt = {
#             e["key"]: e["newValue"]
#             for e in secret_entries
#             if e.get("newValue") and e.get("operation") != "delete"
#         }
#         if to_decrypt:
#             decrypted = await AWSIntegration.kms_decrypt_bulk(storage_auth, to_decrypt)
#             new_secret_map = {k: v for k, v in decrypted.items() if v is not None}
#
#         # ── 5+6. KMS-encrypt audit values over ONE client (kms_encrypt_many) ──
#         # Two groups, encrypted in a single call (one shared KMS connection):
#         #   old values  → old_secret_map  (keyed by source key: old_key or key;
#         #                 deletes included so the 'delete' entry keeps its value)
#         #   carry-over  → carry_audit_map (rename with no newValue, keyed by new key)
#         old_secret_map:  Dict[str, str] = {}
#         carry_audit_map: Dict[str, str] = {}
#         # (target_map, result_key, plaintext) — positionally encrypted together.
#         encrypt_specs: List[Tuple[Dict[str, str], str, str]] = []
#         for e in secret_entries:
#             src = e.get("old_key") or e["key"]
#             plain = current_secret_plain.get(src)
#             if plain:
#                 encrypt_specs.append((old_secret_map, src, plain))
#             if (
#                 not e.get("newValue")
#                 and e.get("operation") != "delete"
#                 and plain
#             ):
#                 encrypt_specs.append((carry_audit_map, e["key"], plain))
#         if encrypt_specs:
#             if not settings.secret_audit_kms_key_id:
#                 raise ValueError("SECRET_AUDIT_KMS_KEY_ID is not configured")
#             ciphertexts = await AWSIntegration.kms_encrypt_many(
#                 storage_auth,
#                 settings.secret_audit_kms_key_id,
#                 [plain for _, _, plain in encrypt_specs],
#             )
#             for (target_map, result_key, _), ct in zip(encrypt_specs, ciphertexts):
#                 if isinstance(ct, Exception):
#                     logger.error("deploy: failed to encrypt audit value '%s': %s", result_key, ct)
#                     continue
#                 target_map[result_key] = ct
#
#         # ── Loop: build per-key data — zero AWS / DB calls ────────────────────
#         results:         List[DeployVariableResult] = []
#         audit_tasks:     List[dict]                  = []
#         ssm_puts:        List[Tuple[str, str]]       = []
#         ssm_deletes:     List[str]                   = []
#         db_updates:      List[dict]                  = []
#         db_soft_deletes: List[str]                   = []
#         working_secret_map: Dict[str, str]           = dict(current_secret_plain)
#
#         for entry in entries:
#             key           = entry["key"]
#             old_key       = entry.get("old_key")
#             is_secret     = entry.get("type") == "secret"
#             operation     = entry.get("operation")  # from staged data only
#             is_rename     = bool(old_key and old_key != key)
#             item_type     = entry.get("type", "variable")
#             variable_code = entry.get("variable_code", "")
#             row           = row_map.get(variable_code)  # only for referenced_*
#             sm_path       = ctx.secret_path(tenant.code)
#             ssm_path      = ctx.parameter_path(tenant.code, key)
#             old_ssm_path  = ctx.parameter_path(tenant.code, old_key) if old_key else ssm_path
#             identifier    = sm_path if is_secret else ssm_path
#
#             try:
#                 # ── Staged deletion ────────────────────────────────────────
#                 if operation == "delete":
#                     audit_old = old_secret_map.get(key) if is_secret else old_parameter_map.get(key)
#                     if is_secret:
#                         working_secret_map.pop(key, None)
#                     else:
#                         ssm_deletes.append(ssm_path)
#
#                     next_ver = (state_map.get(key, {}).get("latest_version") or 0) + 1
#                     audit_tasks.append({
#                         "key": key, "item_type": item_type, "op": "delete",
#                         "old_value": audit_old, "new_value": None,
#                         "file_path": identifier, "version": next_ver,
#                     })
#                     state_map[key] = {
#                         "key": key, "type": item_type, "latest_version": next_ver,
#                         "operation": "delete", "newValue": None,
#                         "timestamp": datetime.now(timezone.utc).isoformat(),
#                     }
#                     db_soft_deletes.append(variable_code)
#                     results.append(DeployVariableResult(
#                         key=key, operation="delete",
#                         cloud_identifier=identifier, status="success",
#                     ))
#                     continue
#
#                 # ── Resolve plain value to push to AWS ────────────────────
#                 if is_secret:
#                     plain_value = new_secret_map.get(key)
#                     if plain_value is None:
#                         # Rename carry-over: no new value staged → use live old
#                         plain_value = current_secret_plain.get(old_key if is_rename else key)
#                         if plain_value is None:
#                             raise ValueError(
#                                 f"No value for '{key}' — nothing staged and no live value to carry over"
#                             )
#                 else:
#                     plain_value = entry.get("newValue")
#                     if plain_value is None:
#                         # Variable rename carry-over
#                         plain_value = old_parameter_map.get(old_key if is_rename else key)
#                         if plain_value is None:
#                             raise ValueError(
#                                 f"No value for '{key}' — nothing staged and no live value to carry over"
#                             )
#
#                 # ── Audit values — all from pre-built maps, zero KMS calls ─
#                 if is_secret:
#                     audit_old = old_secret_map.get(old_key if is_rename else key)
#                     # newValue in staged entry is already KMS-encrypted from save.
#                     # carry_audit_map covers rename-with-no-value edge case.
#                     audit_new = entry.get("newValue") or carry_audit_map.get(key)
#                 else:
#                     audit_old = old_parameter_map.get(old_key if is_rename else key)
#                     audit_new = plain_value  # variables are always plain text
#
#                 # ── Collect audit tasks ────────────────────────────────────
#                 if is_rename:
#                     old_ver = (state_map.get(old_key, {}).get("latest_version") or 0) + 1
#                     new_ver = (state_map.get(key,     {}).get("latest_version") or 0) + 1
#                     audit_tasks.append({
#                         "key": old_key, "item_type": item_type, "op": "delete",
#                         "old_value": None, "new_value": audit_old,
#                         "file_path": old_ssm_path if not is_secret else sm_path,
#                         "version": old_ver,
#                     })
#                     audit_tasks.append({
#                         "key": key, "item_type": item_type, "op": "add",
#                         "old_value": None, "new_value": audit_new,
#                         "file_path": identifier, "version": new_ver,
#                         "old_key": old_key,
#                     })
#                     state_map[old_key] = {
#                         **state_map.get(old_key, {}),
#                         "key": old_key, "latest_version": old_ver, "operation": "delete",
#                     }
#                     state_map[key] = {
#                         "key": key, "type": item_type, "latest_version": new_ver,
#                         "operation": "add", "newValue": audit_new,
#                         "timestamp": datetime.now(timezone.utc).isoformat(),
#                     }
#                 else:
#                     # Original _write_audit_entries forces oldValue=None for a
#                     # non-update ('add') op — only 'update' carries the old value.
#                     next_ver = (state_map.get(key, {}).get("latest_version") or 0) + 1
#                     audit_tasks.append({
#                         "key": key, "item_type": item_type, "op": operation,
#                         "old_value": audit_old if operation == "update" else None,
#                         "new_value": audit_new,
#                         "file_path": identifier, "version": next_ver,
#                     })
#                     state_map[key] = {
#                         "key": key, "type": item_type, "latest_version": next_ver,
#                         "operation": operation, "newValue": audit_new,
#                         "timestamp": datetime.now(timezone.utc).isoformat(),
#                     }
#
#                 # ── In-memory AWS mutations ────────────────────────────────
#                 if is_secret:
#                     if is_rename:
#                         working_secret_map.pop(old_key, None)
#                     working_secret_map[key] = plain_value
#                 else:
#                     if is_rename:
#                         ssm_deletes.append(old_ssm_path)
#                     ssm_puts.append((ssm_path, plain_value))
#
#                 # ── Collect DB update ──────────────────────────────────────
#                 # referenced_* come from the ROW (set at save; not in the staged
#                 # file) so they are never clobbered to NULL. variable_type is
#                 # derived from the entry type — authoritative, writes same value.
#                 db_updates.append({
#                     "code":                      variable_code,
#                     "key":                       key,
#                     "name":                      key,
#                     "secret_provider":           SecretProviderEnum.AWS_SECRETS_MANAGER if is_secret else SecretProviderEnum.AWS_SSM,
#                     "variable_cloud_identifier": identifier,
#                     "variable_type":             VariableTypeEnum.SECRET if is_secret else VariableTypeEnum.VARIABLE,
#                     "_is_secret":                is_secret,  # patched to ARN after PutSecretValue
#                     "referenced_transaction_code": row.referenced_transaction_code if row else None,
#                     "referenced_table_name":     row.referenced_table_name if row else None,
#                 })
#
#                 results.append(DeployVariableResult(
#                     key=key, operation=operation,
#                     cloud_identifier=identifier, status="success",
#                 ))
#
#             except Exception as exc:
#                 logger.error("Failed to process variable '%s': %s", key, exc)
#                 results.append(DeployVariableResult(key=key, status="error", error=str(exc)))
#
#         # ── 7. Audit S3 PUTs over ONE shared client — before any AWS write ────
#         # All N audit entries write through a single S3 connection (one handshake
#         # / credential resolution) instead of a client-per-put.
#         if audit_tasks:
#             audit_auth = {**storage_auth, "bucket": settings.secret_audit_bucket}
#             audit_objects: Dict[str, str] = {}
#             for t in audit_tasks:
#                 prefix  = ctx.audit_key_prefix(t["key"], t["item_type"] == "secret")
#                 s3_key  = f"{prefix}version-{t['version']}.json"
#                 payload: dict = {
#                     "key":       t["key"],
#                     "newValue":  t.get("new_value"),
#                     "oldValue":  t.get("old_value"),
#                     "operation": t["op"],
#                     "version":   t["version"],
#                     "user_id":   user.code,
#                     "user_mail": user.email_id,
#                     "timestamp": datetime.now(timezone.utc).isoformat(),
#                     "file_path": t.get("file_path"),
#                     "type":      t["item_type"],
#                 }
#                 if t.get("old_key"):
#                     payload["oldKey"] = t["old_key"]
#                 audit_objects[s3_key] = json.dumps(payload, indent=2)
#
#             put_results = await FileManagerHandler.put_objects_bulk(
#                 auth_config=audit_auth, items=audit_objects,
#                 content_type="application/json",
#             )
#             failed = [k for k, ok in put_results.items() if not ok]
#             if failed:
#                 raise RuntimeError(f"Failed to write {len(failed)} audit entries: {failed[:5]}")
#             await _progress("audit_trail_saved")
#
#         # ── 8. SM — one PutSecretValue ────────────────────────────────────────
#         # Capture the ARN so secret rows store it (original stored the ARN
#         # returned by upsert_key, not the path).
#         secret_result_arn = secret_arn
#         if secret_entries:
#             await _progress("starting_secrets_deployment")
#             await _progress("deploying_secrets")
#             # No create fallback: the secret's existence was verified upfront
#             # (step 3) — Terraform owns creating it.
#             resp = await AWSIntegration.update_secret(
#                 auth_config=cloud_auth,
#                 secret_name=secret_arn,
#                 secret_value=working_secret_map,
#             )
#             secret_result_arn = (resp or {}).get("arn") or ctx.secret_path(tenant.code)
#             await _progress("secrets_deployed")
#
#         # Patch secret rows to the resolved ARN; strip the transient marker.
#         for u in db_updates:
#             if u.pop("_is_secret", False):
#                 u["variable_cloud_identifier"] = secret_result_arn
#
#         # ── 9. SSM — PutParameter + deletes over ONE shared client ───────────
#         # One SSM connection (one handshake / assume-role) serves every write.
#         if ssm_puts or ssm_deletes:
#             await _progress("starting_config_deployment")
#             await _progress("deploying_configs")
#             put_res, del_res = await AWSIntegration.ssm_write_bulk(
#                 cloud_auth,
#                 puts=dict(ssm_puts),
#                 deletes=ssm_deletes,
#                 parameter_type="String",
#             )
#             ssm_failed = [k for k, ok in {**put_res, **del_res}.items() if not ok]
#             if ssm_failed:
#                 raise RuntimeError(f"Failed {len(ssm_failed)} SSM writes: {ssm_failed[:5]}")
#             await _progress("configs_deployed")
#
#         # ── 10. Bulk DB update ────────────────────────────────────────────────
#         valid_updates = [u for u in db_updates if u.get("code")]
#         if valid_updates:
#             await self.variable_repo.bulk_update_by_code(valid_updates)
#
#         # ── 11. Soft-delete rows for staged deletions (rows already in row_map) ─
#         delete_ids = [row_map[c].id for c in filter(None, db_soft_deletes) if c in row_map]
#         if delete_ids:
#             await self.variable_repo.soft_delete_by_ids(delete_ids)
#
#         # ── 12. Upsert canvas edges ───────────────────────────────────────────
#         for upd in db_updates:
#             ref_code  = upd.get("referenced_transaction_code")
#             ref_table = upd.get("referenced_table_name")
#             if ref_code and ref_table:
#                 await self._ensure_connection_direct(
#                     source_table=table_enum,
#                     source_code=transaction_code,
#                     ref_transaction_code=ref_code,
#                     ref_table_name=ref_table,
#                     environment=ctx.environment,
#                     tenant_code=tenant.code,
#                 )
#
#         # ── 13. Write updated state.json ──────────────────────────────────────
#         if settings.secret_state_bucket and state_map:
#             state_payload = {
#                 "service":     ctx.service_name,
#                 "environment": ctx.environment.value,
#                 "updated_at":  datetime.now(timezone.utc).isoformat(),
#                 "items":       list(state_map.values()),
#             }
#             await FileManagerHandler.put_object(
#                 auth_config=state_auth, key=state_key,
#                 content=json.dumps(state_payload, indent=2),
#                 content_type="application/json",
#             )
#
#         # ── 14. Commit ────────────────────────────────────────────────────────
#         await self.db.commit()
#
#         # ── 15. Delete staged file on full success ────────────────────────────
#         if all(r.status == "success" for r in results):
#             await FileManagerHandler.delete_object(temp_auth, staged_key)
#
#         return results, staged_key

    async def _teardown_variable(
        self,
        key: str,
        row: VariableMstModel,
        is_secret: bool,
        ctx: _PathContext,
        tenant: TenantsMstModel,
        user: UserMstModel,
        storage_auth: dict,
        cloud_auth: dict,
    ) -> str:
        """Remove a variable from its store, journal a 'delete' audit entry, and
        soft-delete the row. Shared by the immediate delete API and the deploy-
        time (staged) delete. Returns the audit-entry key."""
        item_type = "secret" if is_secret else "variable"
        identifier = row.variable_cloud_identifier

        # ── 1. Cloud teardown via the provider plugin (only if ever deployed) ──
        old_value: Optional[str] = None
        if identifier:
            provider  = row.secret_provider or (
                SecretProviderEnum.AWS_SECRETS_MANAGER if is_secret else SecretProviderEnum.AWS_SSM
            )
            component = EnvironmentVariableHandler.get_component(provider, tenant.code)
            try:
                old_value = await component.get_value(cloud_auth, identifier, key)
            except Exception as exc:
                logger.warning("Could not read current value of '%s' for audit: %s", key, exc)
            # Idempotent on the provider side — absent keys are a no-op.
            await component.delete_key(cloud_auth, identifier, key=key)

        audit_old = old_value
        if is_secret and audit_old is not None:
            audit_old = await self._encrypt(storage_auth, audit_old)
        file_path = ctx.secret_path(tenant.code) if is_secret else ctx.parameter_path(tenant.code, key)
        audit_file = await self._write_audit_entry(
            ctx, key, item_type, "delete", audit_old, None, file_path, user, storage_auth,
            write_only=bool(row.is_write_only),
        )
        await self.variable_repo.soft_delete(row.id)
        return audit_file

    async def _deploy_one(
        self,
        entry: dict,
        table_enum: WorkflowSourceTableEnum,
        transaction_code: str,
        ctx: _PathContext,
        tenant: TenantsMstModel,
        user: UserMstModel,
        storage_auth: dict,
        cloud_auth: dict,
    ) -> DeployVariableResult:
        key = entry["key"]
        is_secret = entry.get("type") == "secret"

        variable_code = entry.get("variable_code")
        if variable_code:
            row = await self.variable_repo.get_by(code=variable_code)
        else:
            # Older staged files without variable_code: fall back to key lookup
            row = await self.variable_repo.get_by_transaction_and_key(
                table_name=table_enum,
                transaction_code=transaction_code,
                key=key,
                environment=ctx.environment,
            )
        if not row:
            raise ValueError(f"No variable_mst row for key '{key}' — save it first")

        # The row is the authority; the staged entry is a fallback for a file
        # written before the row update landed.
        write_only = bool(row.is_write_only) or bool(entry.get("write_only"))

        # Staged deletion → shared teardown (cloud + 'delete' audit entry +
        # soft-delete); no AWS upsert.
        if entry.get("operation") == "delete":
            audit_file = await self._teardown_variable(
                key, row, is_secret, ctx, tenant, user, storage_auth, cloud_auth
            )
            return DeployVariableResult(
                key=key,
                operation="delete",
                cloud_identifier=row.variable_cloud_identifier,
                audit_files=[audit_file],
                status="success",
            )

        old_key = entry.get("old_key")
        is_rename = bool(old_key and old_key != key)

        # Trust the operation recorded in the staged file at save time
        operation = entry.get("operation") or ("update" if row.variable_cloud_identifier else "add")

        # Storage component for this item's provider (secrets → Secrets
        # Manager, plain variables → SSM Parameter Store).
        provider = (
            SecretProviderEnum.AWS_SECRETS_MANAGER if is_secret else SecretProviderEnum.AWS_SSM
        )
        component = EnvironmentVariableHandler.get_component(provider, tenant.code)

        # Current live value (the audit oldValue). Secrets live as one
        # consolidated JSON map per service — the old value is this key's
        # entry in that map. Variables are one SSM parameter per key.
        # On a rename the old value lives under the OLD key / old parameter.
        old_value = None
        if is_rename or operation == "update":
            if is_secret:
                old_value = await component.get_value(
                    cloud_auth,
                    row.variable_cloud_identifier or ctx.secret_path(tenant.code),
                    key=old_key if is_rename else key,
                )
            else:
                old_value = await self._fetch_current_value(row, cloud_auth, tenant.code)

        # ── Resolve the value to push ──
        staged_value = entry.get("newValue")
        if staged_value is not None:
            plain_value = staged_value
            if is_secret:
                # KMS key lives in stage — always decrypt with storage_auth
                plain_value = await AWSIntegration.kms_decrypt(storage_auth, plain_value)
        else:
            # Rename without a re-entered value: carry the live value over
            if old_value is None:
                raise ValueError(
                    f"No value for '{key}' — nothing staged and no live value to carry over"
                )
            plain_value = old_value

        # ── Audit trail (values stay encrypted for secrets) ──
        item_type = entry.get("type", "variable")
        audit_old = old_value
        if is_secret and audit_old is not None:
            audit_old = await self._encrypt(storage_auth, audit_old)
        audit_new = staged_value
        if audit_new is None:
            audit_new = await self._encrypt(storage_auth, plain_value) if is_secret else plain_value

        # file_path in audit entries is the cloud location of the value —
        # always populated, even before the first deploy sets the identifier.
        if is_secret:
            old_file_path = new_file_path = ctx.secret_path(tenant.code)
        else:
            new_file_path = (
                ctx.parameter_path(tenant.code, key)
                if is_rename
                else (row.variable_cloud_identifier or ctx.parameter_path(tenant.code, key))
            )
            old_file_path = row.variable_cloud_identifier or ctx.parameter_path(
                tenant.code, old_key or key
            )

        if is_rename:
            # Close the old key's history with a delete entry carrying the value
            # it held; start the new key's history with an add that points back
            # to the old key via oldKey.
            audit_files = [
                await self._write_audit_entry(
                    ctx, old_key, item_type, "delete", None, audit_old,
                    old_file_path, user, storage_auth, write_only=write_only,
                ),
                await self._write_audit_entry(
                    ctx, key, item_type, "add", None, audit_new,
                    new_file_path, user, storage_auth, old_key=old_key,
                    write_only=write_only,
                ),
            ]
        else:
            audit_files = await self._write_audit_entries(
                ctx=ctx,
                key=key,
                item_type=item_type,
                operation=operation,
                old_value=audit_old,
                new_value=audit_new,
                file_path=new_file_path,
                user=user,
                auth_config=storage_auth,
                write_only=write_only,
            )

        # ── Push the real value to the provider (cross-account via cloud_auth) ──
        if is_secret:
            # All secrets of a service live under ONE consolidated store; the
            # component merges this key in (a rename drops the old key and
            # sets the new one in one write).
            identifier = await component.upsert_key(
                cloud_auth,
                row.variable_cloud_identifier or ctx.secret_path(tenant.code),
                key,
                plain_value,
                drop_key=old_key if is_rename else None,
                description=f"Secrets for {ctx.service_name} ({ctx.environment.value})",
                create_identifier=ctx.secret_path(tenant.code),
            )
        else:
            # A renamed variable gets a fresh parameter at the new path; the
            # old parameter is deleted after the new one is in place.
            if is_rename:
                parameter_name = ctx.parameter_path(tenant.code, key)
            else:
                parameter_name = row.variable_cloud_identifier or ctx.parameter_path(tenant.code, key)
            identifier = await component.upsert_key(
                cloud_auth, parameter_name, key, plain_value
            )
            if is_rename and row.variable_cloud_identifier and row.variable_cloud_identifier != parameter_name:
                await component.delete_key(cloud_auth, row.variable_cloud_identifier)

        # The rename reaches the shared row only now — save keeps a deployed
        # row's key untouched so other users never see a half-renamed state.
        await self.variable_repo.update(
            row,
            {
                "key": key,
                "name": key,
                "secret_provider": provider,
                "variable_cloud_identifier": identifier,
            },
        )

        # ── Canvas edge: variable references another resource → ensure a
        # resource_connection_mst row exists (service → referenced resource) ──
        await self._ensure_connection(table_enum, transaction_code, row, ctx, tenant)

        return DeployVariableResult(
            key=key,
            operation=operation,
            cloud_identifier=identifier,
            audit_files=audit_files,
            status="success",
        )

    async def _ensure_connection_direct(
        self,
        source_table: WorkflowSourceTableEnum,
        source_code: str,
        ref_transaction_code: str,
        ref_table_name: WorkflowSourceTableEnum,
        environment: EnvironmentEnum,
        tenant_code: str,
    ) -> None:
        """Upsert the canvas edge using ref fields from the staged entry directly,
        without needing the variable row object."""
        existing = await self.connection_repo.get_by_edge(
            source_table_name=source_table,
            source_transaction_code=source_code,
            target_table_name=ref_table_name,
            target_transaction_code=ref_transaction_code,
            environment=environment,
        )
        if existing:
            if existing.is_deleted:
                await self.connection_repo.update(
                    existing, {"is_deleted": False, "is_active": True}
                )
            return

        await self.connection_repo.create(
            code=f"CONN-{uuid4().hex[:12].upper()}",
            name=f"{source_code} -> {ref_transaction_code}",
            source_transaction_code=source_code,
            source_table_name=source_table,
            target_transaction_code=ref_transaction_code,
            target_table_name=ref_table_name,
            environments_enum=environment,
            tenants_mst_code=tenant_code,
        )
        logger.info(
            "Created resource connection %s:%s -> %s:%s (%s)",
            source_table.value, source_code,
            ref_table_name.value, ref_transaction_code,
            environment.value,
        )

    async def _ensure_connection(
        self,
        source_table: WorkflowSourceTableEnum,
        source_code: str,
        row: VariableMstModel,
        ctx: _PathContext,
        tenant: TenantsMstModel,
    ) -> None:
        """Legacy row-based canvas edge upsert — kept for _teardown_variable."""
        if not row.referenced_transaction_code or not row.referenced_table_name:
            return
        await self._ensure_connection_direct(
            source_table=source_table,
            source_code=source_code,
            ref_transaction_code=row.referenced_transaction_code,
            ref_table_name=row.referenced_table_name,
            environment=ctx.environment,
            tenant_code=tenant.code,
        )

    # ── Path context ──────────────────────────────────────────────────────────

    async def _resolve_path_context(self, transaction_code: str) -> _PathContext:
        """service_config → services_mst → applications_mst → workspace_mst (+ geo_loc)."""
        from app.db.models.workspace_mst_model import WorkspaceMstModel

        stmt = (
            select(
                WorkspaceMstModel.name.label("workspace_name"),
                ApplicationsMstModel.code.label("application_code"),
                ApplicationsMstModel.name.label("application_name"),
                ServiceConfigModel.environment.label("environment"),
                GeoLocMstModel.name.label("region_name"),
                ServiceConfigModel.infrastructuretype_ref_code.label("infra_type_ref"),
                ServicesMstModel.name.label("service_name"),
            )
            .join(ServicesMstModel, ServiceConfigModel.services_mst_code == ServicesMstModel.code)
            .join(ApplicationsMstModel, ServicesMstModel.applications_mst_code == ApplicationsMstModel.code)
            .join(WorkspaceMstModel, ApplicationsMstModel.workspace_code == WorkspaceMstModel.code)
            .join(GeoLocMstModel, ServiceConfigModel.geo_loc_mst_code == GeoLocMstModel.code)
            .where(
                ServiceConfigModel.code == transaction_code,
                ServiceConfigModel.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        rec = result.first()
        if not rec:
            raise ValueError(f"No service_config found for transaction_code '{transaction_code}'")

        return _PathContext(
            workspace_name=rec.workspace_name,
            application_code=rec.application_code,
            application_name=rec.application_name,
            environment=rec.environment,
            region_name=rec.region_name,
            infra_type="eks" if rec.infra_type_ref == _EKS_INFRA_TYPE_REF else "ecs",
            service_name=rec.service_name,
        )

    # ── AWS helpers ───────────────────────────────────────────────────────────

    def _get_storage_auth_config(self) -> dict:
        """S3 buckets and KMS key live in the stage account — always use the
        machine's own IAM role, never a cross-account assumed role."""
        return {"authentication_type": "iam_role", "region": settings.aws_region}

    async def _get_cloud_auth_config(self, ctx: _PathContext, tenant_code: str) -> dict:
        """Secrets Manager / SSM — hierarchical vendor account lookup.
        May return a config with assume_role_arn for cross-account environments.
        Falls back to the machine's own IAM role when no DB row is found."""
        vendor_account = await self.vendor_accounts_repo.get_by_hierarchy(
            infra_vendor_enum=InfraVendorEnum.aws,
            environments_enum=ctx.environment,
            application_code=ctx.application_code,
            tenant_code=tenant_code,
        )
        if vendor_account and vendor_account.auth_config:
            return vendor_account.auth_config
        logger.warning(
            "No AWS vendor account for application '%s' (%s); using default IAM role",
            ctx.application_code, ctx.environment.value,
        )
        return {"authentication_type": "iam_role", "region": settings.aws_region}

    async def _fetch_current_value(
        self,
        row: VariableMstModel,
        cloud_auth: dict,
        tenant_code: str = "default",
    ) -> Optional[str]:
        """Read the live value from the row's provider before it gets replaced."""
        if not row.variable_cloud_identifier:
            return None
        try:
            component = EnvironmentVariableHandler.get_component(
                row.secret_provider or SecretProviderEnum.AWS_SECRETS_MANAGER,
                tenant_code,
            )
        except ValueError as exc:
            # e.g. LOCAL — no external store to read from
            logger.warning(
                "Could not fetch current value for '%s' from %s: %s",
                row.key, row.variable_cloud_identifier, exc,
            )
            return None
        return await component.get_value(cloud_auth, row.variable_cloud_identifier)

    # ── Audit-trail writing ───────────────────────────────────────────────────

    async def _write_audit_entries(
        self,
        ctx: _PathContext,
        key: str,
        item_type: str,
        operation: str,
        old_value: Optional[str],
        new_value: str,
        file_path: Optional[str],
        user: UserMstModel,
        auth_config: dict,
        write_only: bool = False,
    ) -> List[str]:
        """Write versioned audit JSON(s). Update = ONE 'update' entry carrying
        both oldValue (replaced live value) and newValue."""
        entries = []
        if operation == "update":
            entries.append(("update", old_value, new_value))
        else:
            entries.append(("add", None, new_value))

        written: List[str] = []
        for op, old_v, new_v in entries:
            written.append(
                await self._write_audit_entry(
                    ctx, key, item_type, op, old_v, new_v, file_path, user, auth_config,
                    write_only=write_only,
                )
            )
        return written

    async def _write_audit_entry(
        self,
        ctx: _PathContext,
        key: str,
        item_type: str,
        op: str,
        old_value: Optional[str],
        new_value: Optional[str],
        file_path: Optional[str],
        user: UserMstModel,
        auth_config: dict,
        old_key: Optional[str] = None,
        write_only: bool = False,
    ) -> str:
        """Write ONE versioned audit entry under the key's folder."""
        if not settings.secret_audit_bucket:
            raise ValueError("SECRET_AUDIT_BUCKET is not configured")

        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_audit_bucket

        prefix = ctx.audit_key_prefix(key, item_type == "secret")
        version = await self._next_version(s3_auth, prefix)
        s3_key = f"{prefix}version-{version}.json"
        payload = {
            "key": key,
            "newValue": new_value,
            "oldValue": old_value,
            "operation": op,
            "version": version,
            "user_id": user.code,
            "user_mail": user.email_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_path": file_path,
            "type": item_type,
        }
        if old_key:
            payload["oldKey"] = old_key
        if write_only:
            # Forensic marker: the value was written under the write-only policy
            # and was never readable through DevLift. Survives deletion of the
            # variable_mst row, which is otherwise the only record of the flag.
            payload["writeOnly"] = True
        await FileManagerHandler.put_object(
            auth_config=s3_auth,
            key=s3_key,
            content=json.dumps(payload, indent=2),
            content_type="application/json",
        )
        return s3_key

    async def _next_version(self, s3_auth: dict, prefix: str) -> int:
        existing = await FileManagerHandler.list_objects(s3_auth, prefix)
        versions = [
            int(m.group(1))
            for m in (_VERSION_RE.search(k) for k in existing)
            if m
        ]
        return max(versions, default=0) + 1

    async def _encrypt(self, auth_config: dict, plaintext: str) -> str:
        if not settings.secret_audit_kms_key_id:
            raise ValueError("SECRET_AUDIT_KMS_KEY_ID is not configured")
        return await AWSIntegration.kms_encrypt(
            auth_config=auth_config,
            key_id=settings.secret_audit_kms_key_id,
            plaintext=plaintext,
        )

    async def _decrypt(self, auth_config: dict, ciphertext_b64: str) -> str:
        """Inverse of _encrypt — used when reading back audit/temp secret values.
        The KMS key is identified from metadata embedded in the ciphertext blob."""
        return await AWSIntegration.kms_decrypt(
            auth_config=auth_config,
            ciphertext_b64=ciphertext_b64,
        )

    # ── Source readers (for the eager /with-values payload) ───────────────────
    # These read back what deploy_variables wrote, so the UI can show the three
    # value sources side-by-side and detect drift:
    #   cloud   -> Secrets Manager / SSM (live provider)          [_resolve_single_value]
    #   devlift -> state bucket, state.json item newValue         [read_deployed_source]
    #   pending -> temp bucket, this user's staged variables.json  [read_pending_map]

    async def try_build_path_context(self, transaction_code: str) -> Optional[_PathContext]:
        """Best-effort _PathContext for a resource. Returns None when the resource
        isn't a service_config (audit/temp layout is service-scoped) or can't be
        resolved — callers then treat devlift/pending as unavailable."""
        try:
            return await self._resolve_path_context(transaction_code)
        except Exception as exc:
            logger.info("No path context for '%s' (%s) — audit/temp unavailable",
                        transaction_code, exc)
            return None

    async def read_deployed_source(
        self,
        ctx: _PathContext,
        key: str,
        is_secret: bool,
        auth_config: dict,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Latest DEPLOYED value for a key from the state bucket ("devlift").

        Reads the service's single ``state.json`` (latest value/version per key,
        written by ``deploy_variables``) and returns ``(value, timestamp)`` from
        the matching item's ``newValue`` (KMS-decrypted for secrets). Returns
        ``(None, None)`` when nothing was ever deployed, the key isn't in the
        state file, or the last recorded operation was a delete (``newValue`` is
        null).
        """
        if not settings.secret_state_bucket:
            return None, None
        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_state_bucket
        state_key = ctx.state_file_key()
        try:
            raw = await FileManagerHandler.get_object(s3_auth, state_key)
        except Exception as exc:
            logger.warning("state read failed for '%s': %s", state_key, exc)
            return None, None
        if not raw:
            return None, None

        try:
            items = json.loads(raw).get("items", [])
        except Exception as exc:
            logger.warning("state parse failed for '%s': %s", state_key, exc)
            return None, None

        entry = next((it for it in items if it.get("key") == key), None)
        if entry is None:
            return None, None

        value = entry.get("newValue")
        timestamp = entry.get("timestamp")
        # Nothing deployed right now: last op was a delete (rename/close entries
        # carry the closing value in newValue, so check the operation too).
        if value is None or entry.get("operation") == "delete":
            return None, timestamp
        if is_secret:
            try:
                value = await self._decrypt(auth_config, value)
            except Exception as exc:
                logger.warning("state decrypt failed for key '%s': %s", key, exc)
                return None, timestamp
        return value, timestamp

    async def read_deployed_sources_bulk(
        self,
        ctx: _PathContext,
        items: List[Tuple[str, bool]],
        auth_config: dict,
        concurrency: int = 32,
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """Batch form of :meth:`read_deployed_source`.

        ONE get of the service's ``state.json`` (latest value/version per key)
        yields every requested key's deployed value in memory; secrets are then
        KMS-decrypted over a single shared KMS client. Returns
        ``{key: (value, timestamp)}`` with ``(None, None)`` for keys never
        deployed, absent from the state file, or whose last op was a delete.

        Semantically identical to calling ``read_deployed_source`` per key, but
        with a single S3 GET instead of N list+get round-trips. ``items`` is
        ``(key, is_secret)`` pairs.
        """
        result: Dict[str, Tuple[Optional[str], Optional[str]]] = {
            key: (None, None) for key, _ in items
        }
        if not settings.secret_state_bucket or not items:
            return result
        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_state_bucket

        import time as _t
        _s = _t.perf_counter()
        # 1) ONE get of the per-service state.json.
        state_key = ctx.state_file_key()
        try:
            raw = await FileManagerHandler.get_object(s3_auth, state_key)
        except Exception as exc:
            # A batch-level read failure zeroes EVERY key. Don't silently return
            # all-(None,None) — that's indistinguishable from "nothing was ever
            # deployed" and makes the caller fabricate a false cloud_only/drift
            # verdict for every row. Raise so the caller can mark the deployed
            # baseline "unavailable" (status=unknown) instead. Per-key decrypt
            # failures below are still isolated to their own key.
            logger.warning("state bulk read failed for '%s': %s", state_key, exc)
            raise
        _get_ms = (_t.perf_counter() - _s) * 1000.0
        # A missing state.json (never deployed) is NOT a failure — it means every
        # key is genuinely undeployed, so return the all-(None,None) baseline.
        if not raw:
            return result

        # 2) Parse state.json into {key: item}. A parse failure is treated like a
        #    read failure (raise) — a corrupt/truncated file must not masquerade
        #    as "nothing deployed".
        try:
            state_items = json.loads(raw).get("items", [])
        except Exception as exc:
            logger.warning("state parse failed for '%s': %s", state_key, exc)
            raise
        item_by_key = {it["key"]: it for it in state_items if it.get("key")}

        # 3) Resolve each requested key's newValue/timestamp (in memory), then
        #    KMS-decrypt EVERY secret over ONE shared KMS client — a per-key
        #    client-per-call decrypt was the slow path. Same delete/decrypt
        #    semantics as read_deployed_source.
        _s = _t.perf_counter()
        to_decrypt: Dict[str, str] = {}           # key -> ciphertext
        ts_by_key: Dict[str, Optional[str]] = {}  # key -> timestamp (secrets pending decrypt)
        for key, is_secret in items:
            entry = item_by_key.get(key)
            if entry is None:
                continue
            value = entry.get("newValue")
            timestamp = entry.get("timestamp")
            if value is None or entry.get("operation") == "delete":
                result[key] = (None, timestamp)
                continue
            if is_secret:
                to_decrypt[key] = value
                ts_by_key[key] = timestamp
            else:
                result[key] = (value, timestamp)

        if to_decrypt:
            plaintexts = await AWSIntegration.kms_decrypt_bulk(
                auth_config, to_decrypt, concurrency=concurrency,
            )  # {key: plaintext}
            for key, pt in plaintexts.items():
                result[key] = (pt, ts_by_key[key]) if pt is not None else (None, ts_by_key[key])
        _dec_ms = (_t.perf_counter() - _s) * 1000.0

        logger.info(
            "[with-values][step6] state_get=%.0fms  decrypt=%.0fms (%d secrets, %d keys)",
            _get_ms, _dec_ms, len(to_decrypt), len(item_by_key),
        )
        return result

    async def read_pending_map(
        self,
        ctx: _PathContext,
        user_email: str,
        auth_config: dict,
    ) -> Tuple[Dict[str, dict], Dict[str, str]]:
        """Un-deployed staged state for THIS user: (pending entries, renames).

        Pending entries are keyed by the STAGED (new) variable key; each is
        ``{"value": <pending value or None>, "operation": <op>}`` where
        ``operation`` is ``"delete"`` for a staged deletion, else the add/update
        op recorded at save time (may be None for legacy entries). A staged
        deletion of a never-deployed draft still carries the draft's value
        (recovered from ``_prev_staged``) so the UI can show what is being
        deleted; a deletion of a deployed key has value None (cloud/devlift
        cover the display). A staged secret
        whose value couldn't be KMS-decrypted this request carries
        ``"value_unavailable": True`` (value None) — the entry is still returned so
        the change stays staged/deployable, just shown masked. ``renames`` maps the
        deployed DB key -> staged new key, so the listing can overlay the caller's
        un-deployed rename onto the shared row.

        Reads the user's single staged ``variables.json`` from the temp bucket
        (secrets KMS-decrypted). Empty dicts when nothing is staged.
        """
        if not settings.secret_temp_bucket or not user_email:
            return {}, {}
        s3_auth = dict(auth_config)
        s3_auth["bucket"] = settings.secret_temp_bucket
        staged_key = ctx.staged_file_key(user_email)
        try:
            raw = await FileManagerHandler.get_object(s3_auth, staged_key)
        except Exception as exc:
            logger.info("no staged file at '%s' (%s)", staged_key, exc)
            return {}, {}
        if not raw:
            return {}, {}
        try:
            staged = json.loads(raw)
        except Exception as exc:
            logger.warning("staged file parse failed at '%s': %s", staged_key, exc)
            return {}, {}

        # None value = staged rename-only OR delete entry — kept so callers can
        # tell "staged" from "nothing" and read the operation. `renames` maps a
        # deployed DB key -> staged new key for the caller's un-deployed rename.
        pending: Dict[str, dict] = {}
        renames: Dict[str, str] = {}
        to_decrypt: Dict[str, str] = {}  # key -> ciphertext, decrypted in one bulk pass
        for entry in staged.get("items", []):
            key = entry.get("key")
            if key is None:
                continue
            operation = entry.get("operation")
            old_key = entry.get("old_key")
            if old_key and old_key != key:
                renames[old_key] = key
            value = entry.get("newValue")
            value_type = entry.get("type")
            if operation == "delete" and value is None:
                # A staged delete carries no value. If it replaced a
                # never-deployed add/edit, that draft's value exists ONLY in
                # _prev_staged (kept for Revert) — surface it so the listing
                # can still show what is being deleted instead of an empty
                # value (there's no cloud/devlift fallback for pure drafts).
                prev = entry.get("_prev_staged") or {}
                value = prev.get("newValue")
                value_type = prev.get("type") or value_type
            if value is not None and value_type == "secret":
                # Decrypt below over ONE shared KMS client instead of a
                # client-per-call decrypt per staged secret.
                pending[key] = {"value": None, "operation": operation}
                to_decrypt[key] = value
            else:
                pending[key] = {"value": value, "operation": operation}

        if to_decrypt:
            plaintexts = await AWSIntegration.kms_decrypt_bulk(auth_config, to_decrypt)  # {key: plaintext}
            for key, pt in plaintexts.items():
                if pt is None:
                    # Decrypt failed (transient KMS blip). KEEP the entry so the
                    # row stays flagged as staged/deployable — dropping it would
                    # make the user's un-deployed secret edit silently vanish from
                    # the redeploy diff. Mark the value unavailable so callers show
                    # it masked rather than as a real (empty) value.
                    pending[key]["value"] = None
                    pending[key]["value_unavailable"] = True
                else:
                    pending[key]["value"] = pt
        return pending, renames
