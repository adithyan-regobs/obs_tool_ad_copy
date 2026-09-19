"""
Service Configuration Service
Business logic for service configuration operations
"""
import asyncio
import json
import logging
import hashlib
import re
from typing import Optional, List, Dict, Any
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select as sql_select

from app.core.config import settings
from app.utils.service_config_chat.config_vectorizer import ConfigVectorizer
from app.repository.service_config_repository import ServiceConfigRepository, _DELETED_STATUSES
from app.repository.sidecar_config_repository import SidecarConfigRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.language_ref_repository import LanguageRefRepository
from app.repository.gitops_workflow_detail_repository import GitopsWorkflowDetailRepository
from app.repository.service_config_dockerfile_workflow_repository import ServiceConfigDockerfileWorkflowRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.domain.factories.gitops_workflow_detail_factory import make_gitops_workflow_detail
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.utils.language_helpers import is_go_language
from app.utils.service_routing import display_service_path, fill_routing_defaults
from app.utils.service_urls import build_health_url, build_service_url
from app.schemas.service_config_schemas import (
    ServiceConfigCreate,
    ServiceConfigUpdate,
    build_path_for_language,
    ServiceConfigResponse,
    ServiceConfigEnvGeoOption,
    ServiceConfigEnvGeoOptionsResponse,
    ServiceNameResolution,
    ServiceNameResolveResponse,
    SidecarOverrideSchema,
    SidecarConfigResponse,
    DockerfilePRInfo
)
from app.schemas.gitops_workflow_schemas import GitopsWorkflowDetailInfo
from app.services.terragrunt_sync_service import TerragruntSyncService
from app.utils.tenant_config import get_tenant_config
from app.domain.validators.listener_priority_validator import (
    ListenerPriorityValidator,
    ListenerPriorityValidationError
)

logger = logging.getLogger(__name__)


# ingress_group_order slot pool for EKS service ingresses, per
# tenant/environment/geo_loc/cluster. 55 is the floor because the low numbers
# belong to platform ingresses, and 1000 is the ALB controller's ceiling for
# `alb.ingress.kubernetes.io/group.order`.
_INGRESS_ORDER_MIN = 55
_INGRESS_ORDER_MAX = 999

# Orders emitted by generators that write group.order directly rather than
# going through this allocator, so no service_configs row ever reports them as
# used: kustomize_generator_service, default_eks_deploy_script_gen_component
# and eks_generators all hardcode '100' for a service ingress
# (see eks_config.DEFAULT_ALB_GROUP_ORDER_SERVICE). Handing 100 to a service
# would collide with those on the shared ALB group.
_INGRESS_ORDER_RESERVED = frozenset({100})

# Warn while there is still a couple of weeks of headroom at the observed
# create rate, so the pool is never discovered empty.
_INGRESS_ORDER_LOW_WATERMARK = 25


async def _assign_ingress_group_order(
    session: AsyncSession,
    tenant_code: str,
    environment: str,
    geo_loc: str,
    infrastructure_mst_code: Optional[str] = None,
    exclude_service_code: Optional[str] = None,
) -> int:
    """
    Atomically assign the next available ingress_group_order (55 … 999) for an
    EKS service in the given tenant/environment/geo_loc/cluster scope.

    Every number in the range is a candidate. Earlier versions stepped by ten
    — first 50, 60, … 990, then 55, 65, … 995 after 4feb0ad3 shifted the grid
    to escape an exhausted pool — which spent 90% of the range on gaps that
    nothing ever used: every consumer writes the value straight through to
    `alb.ingress.kubernetes.io/group.order` and none reserves the numbers in
    between. Stepping by one turns 95 slots into ~945 and subsumes the old
    grids, since their values simply read as occupied.

    Two things keep the pool from draining the way it did twice before:

    - Only configs that still hold an ingress occupy a slot. A config in
      SOFT_DELETED / HARD_DELETED has no ingress on the cluster, so its order
      returns to the pool.
    - `_INGRESS_ORDER_RESERVED` holds back the orders that other generators
      write directly, which no service_configs row would ever report as used.

    A PostgreSQL transaction-level advisory lock serialises concurrent calls
    for the same scope so two simultaneous creates never pick the same value.
    The lock is released automatically when the surrounding transaction
    commits or rolls back.
    """
    from sqlalchemy import text, select, and_

    # Deterministic 62-bit lock key for this scope (include cluster)
    scope = f"eks_ingress_order:{tenant_code}:{environment}:{geo_loc}:{infrastructure_mst_code or ''}"
    lock_key = int(hashlib.sha256(scope.encode()).hexdigest()[:15], 16) % (2 ** 62)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    filters = [
        ServiceConfigModel.tenant_mst_code == tenant_code,
        ServiceConfigModel.environment == environment,
        ServiceConfigModel.geo_loc_mst_code == geo_loc,
        ServiceConfigModel.infrastructuretype_ref_code == "eks_infrastructuretype_ref",
        ServiceConfigModel.is_deleted == False,
        ServiceConfigModel.status.notin_(_DELETED_STATUSES),
    ]
    if infrastructure_mst_code:
        filters.append(ServiceConfigModel.infrastructure_mst_code == infrastructure_mst_code)
    if exclude_service_code:
        filters.append(ServiceConfigModel.services_mst_code != exclude_service_code)

    result = await session.execute(
        select(ServiceConfigModel.config["ingress_group_order"]).where(and_(*filters))
    )
    used = set()
    for (val,) in result.fetchall():
        if val is not None:
            try:
                used.add(int(val))
            except (ValueError, TypeError):
                pass

    grid = [
        order
        for order in range(_INGRESS_ORDER_MIN, _INGRESS_ORDER_MAX + 1)
        if order not in _INGRESS_ORDER_RESERVED
    ]
    free = [order for order in grid if order not in used]
    if free:
        # Warn early: both previous exhaustions were only noticed once the pool
        # was already empty and every EKS create in the scope was 500ing.
        if len(free) <= _INGRESS_ORDER_LOW_WATERMARK:
            logger.warning(
                "[INGRESS_ORDER] Only %s of %s slots left for %s/%s/%s/%s",
                len(free), len(grid), tenant_code, environment, geo_loc,
                infrastructure_mst_code,
            )
        return free[0]

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"All {len(grid)} ingress group orders "
            f"({_INGRESS_ORDER_MIN}-{_INGRESS_ORDER_MAX}) are in use for "
            f"tenant={tenant_code} env={environment} geo={geo_loc} "
            f"cluster={infrastructure_mst_code}. Delete unused services in this "
            f"cluster to free a slot."
        ),
    )


# ── EFS Model Storage helpers (Phase 1) ──────────────────────────────────────

async def _resolve_hf_revision(model_id: str, hf_token: str | None = None) -> str:
    """Call HuggingFace API to resolve 'main' -> actual commit SHA.

    Falls back to 'main' on any error (private model, network issue, rate limit).
    """
    import httpx
    url = f"https://huggingface.co/api/models/{model_id}"
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            sha = resp.json().get("sha", "main")
            return sha[:40] if sha and sha != "main" else "main"
    except Exception:
        pass
    return "main"


def build_efs_path(model_id: str, revision: str, tenant_code: str | None = None) -> str:
    """Build PVC-root-relative path: shared/{org}--{model}/{sha} or {tenant}/{org}--{model}/{sha}."""
    slug = model_id.replace("/", "--")
    prefix = tenant_code if tenant_code else "shared"
    return f"{prefix}/{slug}/{revision}"


def _make_download_job_name(model_id: str, revision: str) -> str:
    """Generate a DNS-safe K8s Job name (max 63 chars, unique via hash suffix)."""
    import re
    slug = model_id.lower().replace("/", "--")
    # sanitize to DNS label chars
    slug = re.sub(r"[^a-z0-9-]", "-", slug).strip("-")
    short_sha = revision[:12] if revision else "main"
    # hash for uniqueness (avoids collision on truncation)
    raw = f"dl-{slug}-{short_sha}"
    if len(raw) <= 63:
        return raw
    # truncate slug, keep short_sha + hash suffix
    suffix = hashlib.md5(raw.encode()).hexdigest()[:6]
    max_slug = 63 - len("dl-") - len(f"-{short_sha}-{suffix}")
    truncated_slug = slug[:max(1, max_slug)].rstrip("-")
    return f"dl-{truncated_slug}-{short_sha}-{suffix}"


class ServiceConfigService:
    """Service for service configuration operations"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.service_config_repository = ServiceConfigRepository(session)
        self.sidecar_config_repository = SidecarConfigRepository(session)
        self.services_repository = ServicesMstRepository(session)
        self.language_ref_repository = LanguageRefRepository(session)
        self.gitops_workflow_repository = GitopsWorkflowDetailRepository(session)
        self.dockerfile_workflow_repository = ServiceConfigDockerfileWorkflowRepository(session)
        from app.repository.transaction_queue_repository import TransactionQueueRepository
        self.queue_repository = TransactionQueueRepository(session)

    @staticmethod
    def _dotted_get(cfg: dict, dotted: str):
        cur = cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    # EKS request/limit fields the frontend unit-normalizes on save. Old
    # snapshots may hold the raw number ("2048") while current config holds the
    # normalized form ("2048Mi") — same value, so normalize both before diffing.
    _CPU_MILLI_FIELDS = {"cpu_requested", "cpu_limit"}
    _MEMORY_FIELDS = {"memory_requested", "memory_limit"}

    @staticmethod
    def _norm(v):
        # Unset collapses to None so "absent" and "off" compare equal: None/""
        # and a falsy boolean or the string "false" all become None (an older
        # snapshot usually just omits a disabled toggle). "true" and every
        # other value compare by string form so 2 (int) == "2" (str).
        #
        # EDGE CASE — this is generic, so it assumes false == unset for ANY
        # field. That is correct only while the compared fields are the compute
        # settings whose only booleans are enable-toggles (hpa/ebs/autoscaling).
        # If a future field is added where false is a real value distinct from
        # missing, its "false" change would be hidden here — scope it out then.
        # Consequences today: a real toggle-off (true -> false) still shows but
        # its new value renders as unset ("—"), and a disabled toggle is left
        # out of the first-deploy "show all" list.
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return "true" if v else None
        if isinstance(v, str) and v.strip().lower() == "false":
            return None
        # Lists (other_paths, secret_keys, branches…) compare by CONTENT, in the
        # same shape the preview renders them: items stringified, trimmed, empties
        # dropped. str(v) alone made repr artifacts read as changes — a JSON
        # string '["a"]' vs the list ['a'], an item with stray whitespace, an
        # empty element, 1 vs "1" — and every one of them showed the reviewer a
        # diff whose two sides look identical (the FE's readable() strips exactly
        # this noise). A stored JSON-array string is parsed first so a row saved
        # in that older shape compares equal to the list the form sends now.
        if isinstance(v, str) and v.lstrip().startswith("["):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        if isinstance(v, list):
            items = [s for s in (str(x).strip() for x in v) if s]
            return None if not items else "[" + ", ".join(items) + "]"
        return str(v)

    @staticmethod
    def _norm_cpu_millicores(t: str) -> str:
        # Mirrors the frontend: plain number → append "m".
        t = t.strip()
        if not t or t.endswith("m"):
            return t
        try:
            return f"{t}m" if float(t) > 0 else t
        except ValueError:
            return t

    @staticmethod
    def _norm_memory(t: str) -> str:
        # Mirrors the frontend: keep Gi/Mi casing, plain number → append "Mi".
        t = t.strip()
        if not t:
            return t
        low = t.lower()
        if low.endswith("gi"):
            return t[:-2] + "Gi"
        if low.endswith("mi"):
            return t[:-2] + "Mi"
        try:
            return f"{t}Mi" if float(t) > 0 else t
        except ValueError:
            return t

    def _norm_field(self, dotted: str, v):
        n = self._norm(v)
        if n is None:
            return None
        if dotted in self._CPU_MILLI_FIELDS:
            return self._norm_cpu_millicores(n)
        if dotted in self._MEMORY_FIELDS:
            return self._norm_memory(n)
        return n

    @staticmethod
    def _truthy(v) -> bool:
        return v in (True, "true", "True", 1, "1")

    # Keys never surfaced in the settings diff: placement/identity (owned by the
    # target env/geo/cluster), runtime-derived endpoints, snapshot plumbing, and
    # payloads tracked by their own flow. Every OTHER key that differs from the
    # deployed snapshot is shown, so no deployable change is silently missed.
    _DIFF_EXCLUDE_KEYS = frozenset({
        # placement / identity
        "namespace", "cluster_arn", "cluster_name", "cloud_region_id", "region",
        "subnet_ids", "vpc_id", "alb_selection", "alb_schema", "alb_url",
        "ingress_group_order", "listener_rule_priority", "host_ip", "endpoint_url",
        "service_name", "service_type", "resolved_service_path",
        # snapshot plumbing (not user settings)
        "id", "code", "services_mst_code", "tenant_code", "geo_loc_mst_code",
        "environment", "infrastructuretype_ref_code", "infrastructure_mst_code",
        "applications_mst_code", "product_name",
        # tracked by their own flow / non-scalar
        "env_variables", "dockerfile_workflows",
        # snapshot-only fields never stored in the live config JSONB (sent into
        # the deploy payload or a separate column) — they'd always show as a
        # phantom "removed" diff. sidecar_config is a column, not config.
        "ci_provider", "pendingChanges", "pendingchanges", "pending_changes", "sidecar_config",
        # language handled explicitly (a column, not config JSONB). The
        # language_name/language_version keys are denormalized copies of the
        # same selection, so they'd duplicate the single "Language / Version"
        # row — exclude them.
        "language_ref_code", "language_name", "language_version",
    })

    # Language/version is a column (language_ref_code), diffed explicitly against
    # the value enriched into the deployed snapshot root.
    _LANGUAGE_FIELD = "language_ref_code"

    # Nested config groups whose sub-fields are toggle-gated so an inactive group
    # never produces a phantom diff.
    _NESTED_GROUP_KEYS = frozenset({"autoscaling", "hpa", "ebs"})

    _KNOWN_LABELS = {
        "cpu": "CPU", "ram": "RAM", "port": "Port", "health": "Health Check Path",
        "cpu_requested": "CPU Request", "cpu_limit": "CPU Limit",
        "memory_requested": "Memory Request", "memory_limit": "Memory Limit",
        "compute": "Compute Type", "enable_ulimits": "Enable Ulimits",
        "http_scaling_enabled": "HTTP Scaling", "http_scaling_target_value": "HTTP Scaling Target",
        "xms": "JVM Xms", "xmx": "JVM Xmx",
        "create_ecr": "Create ECR", "create_secrets": "Create Secrets Manager",
        "create_ssm": "Create SSM Parameters", "create_argo": "Create Argo CD",
        "auth_mode": "Auth Mode", "replica_count": "Replicas",
        "autoscaling.enabled": "Autoscaling", "autoscaling.min": "Min Task Count",
        "autoscaling.max": "Max Task Count", "autoscaling.desired": "Desired Count",
        "hpa.enabled": "Autoscaling (HPA)", "hpa.min_replicas": "HPA Min Replicas",
        "hpa.max_replicas": "HPA Max Replicas", "hpa.cpu_threshold": "HPA CPU Threshold",
        "hpa.memory_threshold": "HPA Memory Threshold",
        "ebs_enabled": "EBS Storage", "ebs.volume": "EBS Volume", "ebs.size": "EBS Size",
        "ebs.type": "EBS Type",
        # repo / build / docker
        "repository": "Repository", "branches": "Branch", "selected_branches": "Selected Branches",
        "build_path": "Build Path", "service_path": "Service Path", "other_paths": "Other Paths",
        "dockerfile_path": "Dockerfile Path", "dockerfile_content": "Dockerfile Content",
        "generate_dockerfile": "Generate Dockerfile", "build_args": "Build Args",
        "eks_build_config": "EKS Build Config", "java_version": "Java Version",
        "runtime": "Runtime", "go_config_path": "Go Config Path",
        "go_use_aws_secrets": "Go Use AWS Secrets", "wire_enabled": "Wire Enabled",
        "wire_path": "Wire Path", "pipeline_steps": "Pipeline Steps",
        "launch_type": "Launch Type", "additional_args": "Additional Args",
        "container_port": "Container Port",
        # secrets / provisioning / storage
        "secrets_enabled": "Secrets Enabled", "secret_keys": "Secret Keys",
        "custom_iam_policies": "Custom IAM Policies", "slack_channel_id": "Slack Channel",
        "efs_path": "EFS Path",
        # language (pseudo-field)
        "language_ref_code": "Language / Version",
    }

    def _label(self, dotted: str) -> str:
        return self._KNOWN_LABELS.get(dotted) or dotted.split(".")[-1].replace("_", " ").title()

    def _diff_field_list(self, current: dict, deployed: dict):
        """(dotted, label) fields to diff = every key present in current or the
        deployed snapshot, minus the exclude set, with the nested scaling/storage
        groups expanded only while their toggle is on (avoids phantom diffs)."""
        keys = (set(current.keys()) | set(deployed.keys())) - self._DIFF_EXCLUDE_KEYS

        fields = [(k, self._label(k)) for k in sorted(keys - self._NESTED_GROUP_KEYS)]

        # replica_count is meaningful only when HPA is off.
        hpa_on = self._truthy(self._dotted_get(current, "hpa.enabled"))
        if hpa_on:
            fields = [(d, l) for d, l in fields if d != "replica_count"]

        if "autoscaling" in keys:
            fields.append(("autoscaling.enabled", self._label("autoscaling.enabled")))
            if self._truthy(self._dotted_get(current, "autoscaling.enabled")):
                fields += [
                    ("autoscaling.min", self._label("autoscaling.min")),
                    ("autoscaling.max", self._label("autoscaling.max")),
                    ("autoscaling.desired", self._label("autoscaling.desired")),
                ]

        if "hpa" in keys:
            fields.append(("hpa.enabled", self._label("hpa.enabled")))
            if hpa_on:
                fields += [
                    ("hpa.min_replicas", self._label("hpa.min_replicas")),
                    ("hpa.max_replicas", self._label("hpa.max_replicas")),
                    ("hpa.cpu_threshold", self._label("hpa.cpu_threshold")),
                    ("hpa.memory_threshold", self._label("hpa.memory_threshold")),
                ]

        if "ebs" in keys and self._truthy(self._dotted_get(current, "ebs_enabled")):
            fields += [
                ("ebs.volume", self._label("ebs.volume")),
                ("ebs.size", self._label("ebs.size")),
                ("ebs.type", self._label("ebs.type")),
            ]

        return fields

    def diff_config_dicts(self, proposed: dict, live: dict) -> dict:
        """{field: {"from": live, "to": proposed}} between two config dicts.

        THE PROPOSAL'S KEYS DEFINE WHAT IS IN PLAY — the JSON Merge Patch rule
        (RFC 7386), and the way Kubernetes server-side apply treats an applied
        manifest: a key absent from the request is a field the request does not
        speak to, not a change. So this walks the proposal and nothing else.

        That one rule is what keeps this free of field-name lists. Keys that
        exist only in the live row — another shape's fields, cluster-derived
        plumbing written at deploy — never surface, because no proposal carries
        them; and a new form field joins the diff by existing in the payload,
        with no registry to update. Which is also why the caller must hand in
        the PAYLOAD, not the whole snapshot: the snapshot's root carries queue
        identity, and identity keys would all read as additions here.

        The approval flow needs this shape: a change is parked in
        transaction_queue.config_snapshot and NOTHING is written to
        service_configs until deploy, so the live row still holds the real
        running values and is the right baseline to diff against.

        Kept here, beside the normalisation helpers it shares with
        get_settings_diff, so there is one engine for what values MEAN — units,
        booleans, empties — even though the two screens pick fields differently
        (that one compares two deployed states, so it unions both sides).
        """
        proposed, live = proposed or {}, live or {}

        def shown(raw, normalised):
            """What a reader should see, which is not what we compare on.

            _norm_field folds False into None so that "absent" and "off" compare
            equal — without it, a toggle merely missing from an older snapshot
            reads as a change. Right for the comparison, wrong for display: it
            renders a real true -> false as "true -> —". So compare on the
            normalised value and show the raw one.
            """
            if isinstance(raw, bool):
                return "true" if raw else "false"
            return normalised

        out: dict = {}

        def walk(p: dict, l, prefix: str = "") -> None:
            for key, raw_to in p.items():
                dotted = f"{prefix}.{key}" if prefix else key
                raw_frm = l.get(key) if isinstance(l, dict) else None
                if isinstance(raw_to, dict):
                    # Nested groups (autoscaling, hpa, ebs, whatever comes
                    # next) recurse on the same rule: only the sub-keys the
                    # proposal carries. A request with scaling off sends
                    # {"enabled": false} and nothing else, so min/max/desired
                    # are not in play and cannot show as phantom removals.
                    walk(raw_to, raw_frm if isinstance(raw_frm, dict) else {}, dotted)
                    continue
                to = self._norm_field(dotted, raw_to)
                frm = self._norm_field(dotted, raw_frm)
                if to != frm:
                    out[dotted] = {"from": shown(raw_frm, frm), "to": shown(raw_to, to)}

        walk(proposed, live)
        return out

    async def get_settings_diff(self, code: str, tenant_code: str) -> "SettingsDiffResponse":
        """Diff current settings config against the last DEPLOYED snapshot."""
        from app.schemas.service_config_schemas import SettingsDiffItem, SettingsDiffResponse

        config_row = await self.service_config_repository.get_by_code_and_tenant(code, tenant_code)
        if not config_row:
            raise HTTPException(status_code=404, detail="Service config not found")

        current = config_row.config or {}

        # A clone-settings copy sets the config but never queues; such a service
        # isn't deployable yet (repo/build not filled), so it must not offer a
        # settings diff / redeploy. Gate on a real Save having queued once.
        if not await self.queue_repository.has_settings_queue_item(code, tenant_code):
            return SettingsDiffResponse(
                service_config_code=code, has_deployed_baseline=False, items=[]
            )

        latest = await self.queue_repository.get_latest_deployed_by_transaction_code(
            code, tenant_code
        )
        has_baseline = bool(latest and latest.config_snapshot)

        # Snapshots come in two shapes: the settings-tab deploy spreads the
        # config fields flat at the snapshot ROOT, while the relations builder
        # nests them under snapshot["config"]. Support both. language_ref_code is
        # a column, enriched at the snapshot ROOT — read it from snap, not config.
        snap = (latest.config_snapshot or {}) if latest else {}
        nested = snap.get("config")
        deployed = nested if isinstance(nested, dict) else snap

        fields = self._diff_field_list(current, deployed if has_baseline else {})
        cur_lang = self._norm(config_row.language_ref_code)

        # No deploy yet → no baseline: every set field is "pending first deploy",
        # so list them all (deployed_value = None). Once deployed, only fields
        # that differ from the snapshot show — emptying out after each deploy.
        if not has_baseline:
            items = [
                SettingsDiffItem(
                    field=dotted, label=label,
                    current_value=cur, deployed_value=None,
                )
                for dotted, label in fields
                if (cur := self._norm_field(dotted, self._dotted_get(current, dotted))) is not None
            ]
            if cur_lang is not None:
                items.append(SettingsDiffItem(
                    field=self._LANGUAGE_FIELD, label=self._label(self._LANGUAGE_FIELD),
                    current_value=cur_lang, deployed_value=None,
                ))
            return SettingsDiffResponse(
                service_config_code=code, has_deployed_baseline=False, items=items
            )

        items = []
        for dotted, label in fields:
            cur = self._norm_field(dotted, self._dotted_get(current, dotted))
            dep = self._norm_field(dotted, self._dotted_get(deployed, dotted))
            if cur != dep:
                items.append(
                    SettingsDiffItem(
                        field=dotted, label=label,
                        current_value=cur, deployed_value=dep,
                    )
                )
        dep_lang = self._norm(snap.get(self._LANGUAGE_FIELD))
        if cur_lang != dep_lang:
            items.append(SettingsDiffItem(
                field=self._LANGUAGE_FIELD, label=self._label(self._LANGUAGE_FIELD),
                current_value=cur_lang, deployed_value=dep_lang,
            ))
        return SettingsDiffResponse(
            service_config_code=code, has_deployed_baseline=True, items=items
        )

    async def has_pending_settings_diff(self, code: str, tenant_code: str) -> bool:
        """Raw "is there a real settings change to deploy?" check — current live
        config vs the latest DEPLOYED snapshot, using the SAME field/normalize
        logic as get_settings_diff but WITHOUT the queue-row gate. Used to stop a
        deploy that would push nothing (e.g. another user already deployed the
        change). Returns True when no baseline exists yet (first deploy).
        """
        config_row = await self.service_config_repository.get_by_code_and_tenant(code, tenant_code)
        if not config_row:
            return False
        current = config_row.config or {}

        latest = await self.queue_repository.get_latest_deployed_by_transaction_code(code, tenant_code)
        if not (latest and latest.config_snapshot):
            # No deployed baseline yet → first deploy → treat any set config as pending.
            return True

        snap = latest.config_snapshot or {}
        nested = snap.get("config")
        deployed = nested if isinstance(nested, dict) else snap

        for dotted, _ in self._diff_field_list(current, deployed):
            if self._norm_field(dotted, self._dotted_get(current, dotted)) != \
               self._norm_field(dotted, self._dotted_get(deployed, dotted)):
                return True
        if self._norm(config_row.language_ref_code) != self._norm(snap.get(self._LANGUAGE_FIELD)):
            return True
        return False

    async def _settle_routing(
        self,
        config_dict: Optional[dict],
        *,
        tenant_code: str,
        services_mst_code: str,
        language_ref_code: Optional[str],
        infrastructuretype_ref_code: Optional[str],
        alb_selection: Optional[str],
        existing: Optional[ServiceConfigModel] = None,
        service=None,
    ) -> Optional[dict]:
        """Settle service_path / health on a config about to be written and drop
        the backend-owned keys a client may have echoed back (fill_routing_defaults).

        Every write path — create, update, upsert — funnels through here so the
        row, the queue snapshot and the rendered manifests agree on the path.
        """
        if config_dict is None:
            return None
        if service is None:
            service = await self.services_repository.get_by_code(services_mst_code)
        language_name = None
        if language_ref_code:
            language_ref = await self.language_ref_repository.get_by_code(language_ref_code)
            language_name = language_ref.name if language_ref else None
        stored = existing.config if existing is not None and isinstance(existing.config, dict) else {}
        deployed = False
        if existing is not None and not (stored.get("service_path") and stored.get("health")):
            deployed = bool(await self.queue_repository.get_latest_deployed_by_transaction_code(
                existing.code, tenant_code, settings_rows_only=True
            ))
        return fill_routing_defaults(
            config_dict,
            service_name=service.name if service else None,
            language_name=language_name,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            service_type=service.service_type if service else None,
            alb_selection=alb_selection,
            stored=stored,
            deployed=deployed,
        )

    async def _display_urls(self, config: ServiceConfigModel):
        """(config for the response, health_url).

        The row stores the ALB base alone in `alb_url`; the response carries
        the full service URL under the same key, so no client needs to know
        the split. Derived here so every client shows the same URL.
        """
        cfg = config.config
        if not isinstance(cfg, dict):
            return cfg, None
        alb_url = str(cfg.get("alb_url") or "").strip()
        if not alb_url:
            return cfg, None
        service_path = display_service_path(cfg)
        shown = {**cfg, "alb_url": build_service_url(alb_url, service_path)}
        health_url = build_health_url(alb_url, cfg.get("health") or "", service_path) or None
        return shown, health_url

    async def _build_service_config_response(self, config: ServiceConfigModel) -> ServiceConfigResponse:
        """Build response with manually fetched gitops_workflow to avoid SQLAlchemy lazy loading issues."""
        workflow_info = None
        if config.gitops_workflow_id:
            workflow = await self.gitops_workflow_repository.get_by_id(config.gitops_workflow_id)
            if workflow:
                workflow_info = GitopsWorkflowDetailInfo.model_validate(workflow)

        # Get dockerfile workflows from junction table (NEW: proper relational approach)
        dockerfile_workflows = []
        try:
            junction_links = await self.dockerfile_workflow_repository.get_workflows_for_config(config.id)
            logger.info(f"[DEBUG] Found {len(junction_links)} dockerfile workflow links in junction table")
            for link in junction_links:
                wf = link.gitops_workflow
                if wf:
                    dockerfile_workflows.append(DockerfilePRInfo(
                        branch=link.branch,
                        feature_branch=wf.git_branch or "",
                        pr_number=wf.pr_number,
                        pr_url=wf.pr_url,
                        pr_status=wf.pr_status.value if wf.pr_status else None,
                        commit_sha=wf.git_commit_sha,
                        git_repository=link.repository,
                        status="success" if wf.pr_number else "pending",
                        error=None,
                        created_at=wf.created_at.isoformat() if wf.created_at else None,
                        dockerfile_workflow_code=link.code
                    ))
        except Exception as e:
            logger.warning(f"Failed to fetch dockerfile workflows from junction table: {e}")

        # FALLBACK 1: Check config JSONB (for backward compatibility during migration)
        if not dockerfile_workflows and config.config and config.config.get("dockerfile_workflows"):
            raw_workflows = config.config.get("dockerfile_workflows", [])
            logger.info(f"[DEBUG] Fallback: Found {len(raw_workflows)} dockerfile workflows in config JSONB")
            for wf in raw_workflows:
                try:
                    dockerfile_workflows.append(DockerfilePRInfo(**wf))
                except Exception as e:
                    logger.warning(f"Failed to parse dockerfile workflow: {e}")

        # FALLBACK 2: Check old dockerfile_gitops_workflow_id FK
        if not dockerfile_workflows and config.dockerfile_gitops_workflow_id:
            logger.info(f"[DEBUG] Fallback: Reading from dockerfile_gitops_workflow_id={config.dockerfile_gitops_workflow_id}")
            old_workflow = await self.gitops_workflow_repository.get_by_id(config.dockerfile_gitops_workflow_id)
            if old_workflow:
                feature_branch = old_workflow.git_branch or ""
                base_branch = "main"
                if feature_branch:
                    parts = feature_branch.replace("datadog/", "").split("-")
                    if len(parts) >= 2:
                        base_branch = parts[1]

                dockerfile_workflows.append(DockerfilePRInfo(
                    branch=base_branch,
                    feature_branch=feature_branch,
                    pr_number=old_workflow.pr_number,
                    pr_url=old_workflow.pr_url,
                    pr_status=old_workflow.pr_status.value if old_workflow.pr_status else None,
                    commit_sha=old_workflow.git_commit_sha,
                    git_repository=old_workflow.git_repository,
                    status="success" if old_workflow.pr_number else "pending",
                    error=None,
                    created_at=old_workflow.created_at.isoformat() if old_workflow.created_at else None
                ))

        display_config, health_url = await self._display_urls(config)
        return ServiceConfigResponse(
            id=config.id,
            code=config.code,
            name=config.name,
            tenant_mst_code=config.tenant_mst_code,
            services_mst_code=config.services_mst_code,
            infrastructuretype_ref_code=config.infrastructuretype_ref_code,
            infra_vendor_enum=config.infra_vendor_enum.value if hasattr(config.infra_vendor_enum, 'value') else config.infra_vendor_enum,
            infrastructure_mst_code=config.infrastructure_mst_code,
            environment=config.environment.value if hasattr(config.environment, 'value') else config.environment,
            geo_loc_mst_code=config.geo_loc_mst_code,
            alb_selection=config.alb_selection,
            language_ref_code=config.language_ref_code,
            namespace=config.config.get("namespace") if config.config else None,
            config=display_config,
            health_url=health_url,
            sidecar_config=config.sidecar_config or [],
            deployment_strategy=config.deployment_strategy,
            gitops_workflow=workflow_info,
            dockerfile_gitops_workflows=dockerfile_workflows,
            sync_status=config.sync_status,
            created_at=config.created_at,
            updated_at=config.updated_at
        )

    async def get_service_config(
        self,
        tenant_code: str,
        service_code: str,
        environment: str,
        geo_loc_code: str,
        alb_selection: str = "existing_alb",
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
        infrastructure_mst_code: Optional[str] = None
    ) -> Optional[ServiceConfigResponse]:
        """
        Get service configuration by tenant, service code, environment, geo location, ALB type,
        and optionally infra vendor, infrastructure type, and infrastructure instance.
        Automatically enriches sidecar_config array with names from sidecar_configs table.

        Args:
            tenant_code: Tenant code (from JWT)
            service_code: Service code
            environment: Environment (dev/staging/prod)
            geo_loc_code: Geographic location code
            alb_selection: ALB type (no_alb/existing_alb/create_new_alb)
            infra_vendor: Infrastructure vendor (aws/azure/gcp/on_prem) - optional filter
            infrastructure_type: Infrastructure type code - optional filter
            infrastructure_mst_code: Infrastructure instance (cluster) code - optional filter

        Returns:
            ServiceConfigResponse with enriched sidecar names or None if not found
        """
        logger.info(f"Getting service config for tenant={tenant_code}, service={service_code} in {environment}, geo_loc {geo_loc_code}, alb={alb_selection}, infra_vendor={infra_vendor}, infra_type={infrastructure_type}, infra_mst={infrastructure_mst_code}")
        config = await self.service_config_repository.get_by_tenant_service_env_geo_loc(
            tenant_code,
            service_code,
            environment,
            geo_loc_code,
            alb_selection,
            infra_vendor=infra_vendor,
            infrastructure_type=infrastructure_type,
            infrastructure_mst_code=infrastructure_mst_code
        )

        if not config:
            return None

        if config.sidecar_config:
            # Enrich sidecar_config with names and default_advanced_options from sidecar_configs table
            enriched_sidecars = []
            for sidecar_override in config.sidecar_config:
                sidecar_code = sidecar_override.get('sidecar_config_code')
                if sidecar_code:
                    # Fetch sidecar details from sidecar_configs table
                    sidecar_config = await self.sidecar_config_repository.get_by(
                        code=sidecar_code,
                        is_deleted=False
                    )
                    if sidecar_config:
                        # Add name and default_advanced_options to the override dict
                        enriched_override = {
                            **sidecar_override,
                            'name': sidecar_config.name,
                            'default_advanced_options': sidecar_config.config.get('advanced_options')
                        }
                        enriched_sidecars.append(enriched_override)
                    else:
                        # Keep original if sidecar not found
                        enriched_sidecars.append(sidecar_override)
                else:
                    enriched_sidecars.append(sidecar_override)

            # Update config with enriched sidecars
            config.sidecar_config = enriched_sidecars

        return await self._build_service_config_response(config)

    async def get_env_geo_options(
        self,
        tenant_code: str,
        service_code: str,
    ) -> ServiceConfigEnvGeoOptionsResponse:
        """
        All (environment, geo loc, cluster) combinations a service has active
        service_config rows for. Drives the resource-detail-panel context bar:
        the frontend derives environments → geo locs (per env) → clusters
        (per env + geo) from these combos.

        Args:
            tenant_code: Tenant code (from JWT)
            service_code: services_mst.code

        Returns:
            ServiceConfigEnvGeoOptionsResponse with one option per combination
        """
        combos = await self.service_config_repository.get_env_geo_options_for_service(
            tenant_code=tenant_code,
            service_code=service_code,
        )
        options = [
            ServiceConfigEnvGeoOption(
                environment=str(getattr(row["environment"], "value", row["environment"])),
                geo_loc_code=row["geo_loc_code"],
                geo_loc_name=row.get("geo_loc_name"),
                infra_vendor_enum=str(getattr(row["infra_vendor_enum"], "value", row["infra_vendor_enum"])) if row.get("infra_vendor_enum") else None,
                config_code=row["config_code"],
                infrastructure_mst_code=row.get("infrastructure_mst_code"),
                infrastructuretype_ref_code=row.get("infrastructuretype_ref_code"),
                cluster_name=row.get("cluster_name"),
            )
            for row in combos
        ]
        return ServiceConfigEnvGeoOptionsResponse(
            service_mst_code=service_code,
            options=options,
        )

    async def resolve_service_names(
        self,
        tenant_code: str,
        config_codes: List[str],
    ) -> ServiceNameResolveResponse:
        """Name a batch of service_configs by joining through to services_mst.

        The authz console needs this: it holds one store object per
        service_config and has no other way to label them. It used to derive the
        services_mst uuid from the config code with a regex and look it up in a
        client-side copy of the whole service catalog — which broke for the older
        service-config-<uuid>-… codes and needed several paged requests to build.

        Codes that resolve to nothing come back in `unresolved` rather than
        being dropped silently, so the caller can tell "no such config" apart
        from "this config has no name".

        Args:
            tenant_code: Tenant code (from JWT) — scopes the lookup
            config_codes: service_configs.code values

        Returns:
            ServiceNameResolveResponse with one entry per resolved code
        """
        rows = await self.service_config_repository.resolve_service_names(
            tenant_code=tenant_code,
            config_codes=config_codes,
        )
        resolved = [
            ServiceNameResolution(
                config_code=row["config_code"],
                service_mst_code=row["service_mst_code"],
                service_name=row["service_name"],
                resource_group_code=row.get("resource_group_code"),
                environment=(
                    str(getattr(row["environment"], "value", row["environment"]))
                    if row.get("environment")
                    else None
                ),
                geo_loc_code=row.get("geo_loc_code"),
                is_deleted=bool(row.get("is_deleted")),
            )
            for row in rows
        ]
        found = {r.config_code for r in resolved}
        # de-duplicated, and in the order the caller asked, so a diff of
        # request vs response reads cleanly in logs
        seen: set = set()
        unresolved = [
            c for c in config_codes
            if c not in found and not (c in seen or seen.add(c))
        ]
        return ServiceNameResolveResponse(resolved=resolved, unresolved=unresolved)

    async def _map_config_to_resource_group_fga(
        self, service, config_code: str, tenant_code: str, user_code: str = None
    ) -> None:
        """Parent a freshly created config under its resource group in OpenFGA
        so it inherits the group's roles/permissions from day one.

        Also places the creator in a per-service admin group so they can act on
        what they just made, whether or not they hold a role on the group.

        Non-fatal by design: the config row is already created, and an
        unreachable authz service must not fail the save — the mapping can be
        healed from Access Control. The service lands under the group that was
        REQUESTED; scm no longer substitutes another when this one holds no
        tuples yet (that fallback is disabled — see authz_mapping.py), so a
        brand-new group is used as chosen and simply has no members until a
        role is granted on it.
        """
        if not (service and service.resource_group_mst_code):
            logger.warning(
                "[FGA] Skipping resource_group mapping for %s — service or its "
                "resource_group_mst_code not found", config_code,
            )
            return
        import httpx as _httpx

        # The creator is put in a per-service admin group so they can act on the
        # service immediately — without it, creating one in a group you hold no
        # role on leaves it unreachable to you the moment it exists.
        #
        # Only the SERVICE CODE is sent. scm derives the group id from it: the
        # id format, its sanitisation and its collision rules are OpenFGA
        # concerns, and every FGA write already lives there. obs_tool naming a
        # `service:` object it wants CHECKED is unavoidable; minting a new group
        # name for a WRITE is not, and would have to be kept in step by hand.
        admin_service_code = getattr(service, "code", None)
        if not admin_service_code or not user_code:
            logger.info(
                "[FGA] No admin group for %s (service=%r user=%r) — parent tuple only",
                config_code, admin_service_code, user_code,
            )
            admin_service_code = None

        logger.info(
            "[FGA] Requesting resource_group mapping: config=%s rg=%s tenant=%s url=%s",
            config_code,
            service.resource_group_mst_code,
            tenant_code,
            settings.secret_service_url.rstrip("/")
            + "/api/v1/internal/authz/map-service-resource-group",
        )
        try:
            from app.integrations.secret_config_client import SecretConfigClient
            mapping = await SecretConfigClient(timeout=10).map_service_to_resource_group(
                resource_group_code=service.resource_group_mst_code,
                service_config_code=config_code,
                tenant_code=tenant_code,
                admin_service_code=admin_service_code,
                admin_user_code=user_code,
            )
            logger.info(
                "[FGA] Mapping response for %s: %s", config_code, mapping,
            )
            logger.info(
                "[FGA] Mapped %s under resource_group %s (requested=%s, outcome=%s, "
                "admin_group=%s granted=%s member=%s skipped=%s)",
                config_code,
                mapping.get("mapped_resource_group_code"),
                service.resource_group_mst_code,
                mapping.get("outcome"),
                mapping.get("admin_group"),
                mapping.get("admin_group_granted"),
                mapping.get("admin_member_added"),
                # granted=False with skipped=True is scm declining to duplicate
                # a grant the creator already holds via the resource group —
                # not a failed write.
                mapping.get("admin_grant_skipped"),
            )
        except _httpx.HTTPStatusError as e:
            # The upstream status + body carry the real reason (404 route not
            # deployed, 403 key mismatch, 404 no rg in FGA) — log them, not
            # just the exception repr.
            logger.error(
                "[FGA] service->resource_group mapping failed for %s (rg=%s): "
                "upstream_status=%s body=%r url=%s",
                config_code, service.resource_group_mst_code,
                e.response.status_code, e.response.text, e.request.url,
            )
        except Exception as e:
            logger.exception(
                "[FGA] service->resource_group mapping failed for %s (rg=%s): %r",
                config_code, service.resource_group_mst_code, e,
            )

    async def create_service_config(
        self,
        tenant_code: str,
        service_config_data: ServiceConfigCreate,
        user_email: str = None,
        user_code: str = None
    ) -> ServiceConfigResponse:
        """
        Create a new service configuration.

        Args:
            tenant_code: Tenant code (from JWT)
            service_config_data: Service configuration creation data
            user_email: User email (from JWT) for PR attribution
            user_code: User code (from JWT) for workflow tracking

        Returns:
            Created ServiceConfigResponse

        Raises:
            HTTPException: If config already exists or validation fails
        """
        logger.info(f"Creating service config for tenant={tenant_code}, service={service_config_data.services_mst_code}")

        # Get alb_selection from config (default to existing_alb)
        alb_selection = "existing_alb"
        if service_config_data.config:
            alb_selection = service_config_data.config.alb_selection or "existing_alb"

        # Check if configuration already exists for this tenant + ALB type + infra type
        existing_config = await self.service_config_repository.get_by_tenant_service_env_geo_loc(
            tenant_code,
            service_config_data.services_mst_code,
            service_config_data.environment.value,
            service_config_data.geo_loc_mst_code,
            alb_selection,
            infra_vendor=service_config_data.infra_vendor_enum.value if service_config_data.infra_vendor_enum else None,
            infrastructure_type=service_config_data.infrastructuretype_ref_code,
            infrastructure_mst_code=service_config_data.infrastructure_mst_code
        )

        if existing_config:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": f"Configuration already exists for service {service_config_data.services_mst_code} in {service_config_data.environment.value} environment, geo_loc {service_config_data.geo_loc_mst_code}, ALB type {alb_selection}, infra type {service_config_data.infrastructuretype_ref_code}",
                    "config_code": existing_config.code
                }
            )

        # Validate sidecar codes if provided
        if service_config_data.sidecar_config:
            await self._validate_sidecar_codes(service_config_data.sidecar_config)

        # Handle listener priority - check availability and clear if conflict (e.g., during clone)
        if service_config_data.config and service_config_data.config.listener_rule_priority:
            # Get service to find application_code and service_type for validation scope
            service_for_validation = await self.services_repository.get_by_code(service_config_data.services_mst_code)
            # Get service_type for ALB scoping (OPS_TOOLS uses different ALB)
            svc_type = service_for_validation.service_type.value if hasattr(service_for_validation.service_type, 'value') else str(service_for_validation.service_type)
            # Check if priority is available (don't raise error, just check)
            priority_check = await self.check_listener_priority_availability(
                tenant_code=tenant_code,
                listener_priority=service_config_data.config.listener_rule_priority,
                environment=service_config_data.environment.value,
                application_code=service_for_validation.applications_mst_code,
                geo_loc_mst_code=service_config_data.geo_loc_mst_code,
                service_type=svc_type
            )
            # If priority is not available (conflict), clear it so user can set it manually
            if not priority_check.get("valid", True):
                service_config_data.config.listener_rule_priority = None

        # build_path by language: Python has none; Go stores `./cmd`; others `cmd`
        # (build_path_for_language is the single rule, shared with the queue route).
        if service_config_data.config and service_config_data.config.build_path and service_config_data.language_ref_code:
            language_ref = await self.language_ref_repository.get_by_code(service_config_data.language_ref_code)
            if language_ref and language_ref.name.lower().startswith("python"):
                service_config_data.config.build_path = None
            elif language_ref:
                service_config_data.config.build_path = build_path_for_language(
                    service_config_data.config.build_path, is_go_language(language_ref.name)
                )

        # Auto-generate code and name (include geo_loc, ALB type, and infra type)
        # Use hash suffix to ensure uniqueness while keeping code under 100 chars
        infra_type_ref = service_config_data.infrastructuretype_ref_code
        if infra_type_ref:
            infra_type_short = infra_type_ref.replace("_infrastructuretype_ref", "")
        else:
            infra_type_short = "unknown"
        infra_mst_code = service_config_data.infrastructure_mst_code or ""

        # Generate a short hash from the full unique key to ensure uniqueness
        # Include infrastructure_mst_code since it's part of the unique constraint
        svc_code = service_config_data.services_mst_code
        env_val = service_config_data.environment.value
        geo_loc = service_config_data.geo_loc_mst_code
        unique_key = (
            f"{svc_code}-{env_val}-{geo_loc}-"
            f"{alb_selection}-{infra_type_short}-{infra_mst_code}"
        )
        hash_suffix = hashlib.md5(unique_key.encode()).hexdigest()[:8]

        # Truncate service code if needed to keep total under 100 chars
        # Format: sc-{service}-{env}-{hash} = max ~60 chars
        service_code_truncated = svc_code[:50]
        code = f"sc-{service_code_truncated}-{env_val}-{hash_suffix}"
        name = (
            f"Config for {svc_code} - {env_val} - "
            f"{geo_loc} - {alb_selection} - {infra_type_short}"
        )

        # Convert config to dict for JSONB storage
        config_dict = service_config_data.config.model_dump() if service_config_data.config else None

        # Extract dockerfile_content from config for EKS
        # Only save to dockerfile column when generate_dockerfile is FALSE (disabled)
        # If generate_dockerfile is TRUE, the Dockerfile will be generated dynamically
        dockerfile_content = None
        if config_dict and service_config_data.infrastructuretype_ref_code == "eks_infrastructuretype_ref":
            generate_dockerfile = config_dict.get("generate_dockerfile", False)
            dockerfile_content_from_request = config_dict.get("dockerfile_content")

            # Only save dockerfile_content if generate_dockerfile is False (disabled) AND dockerfile_content is provided
            if not generate_dockerfile and dockerfile_content_from_request:
                dockerfile_content = config_dict.pop("dockerfile_content")
                logger.info(f"[DOCKERFILE_SAVE] Saving custom Dockerfile to DB (generate_dockerfile=False)")
            else:
                # Remove dockerfile_content from config (don't save in JSONB)
                config_dict.pop("dockerfile_content", None)
                if generate_dockerfile:
                    logger.info(f"[DOCKERFILE_SAVE] Not saving Dockerfile (generate_dockerfile=True, will generate dynamically)")
                else:
                    logger.info(f"[DOCKERFILE_SAVE] No dockerfile_content provided in request")

            # Auto-assign ingress_group_order if not already set (new service)
            if not config_dict.get("ingress_group_order"):
                config_dict["ingress_group_order"] = await _assign_ingress_group_order(
                    session=self.session,
                    tenant_code=tenant_code,
                    environment=env_val,
                    geo_loc=geo_loc,
                    infrastructure_mst_code=service_config_data.infrastructure_mst_code,
                    exclude_service_code=service_config_data.services_mst_code,
                )
                logger.info(
                    "[INGRESS_ORDER] Auto-assigned ingress_group_order=%s for %s/%s/%s/%s",
                    config_dict["ingress_group_order"], tenant_code, env_val, geo_loc,
                    service_config_data.infrastructure_mst_code,
                )

        # Get service to find app_code and rg_code for sidecar mapping
        service = await self.services_repository.get_by_code(service_config_data.services_mst_code)

        # Map sidecars to correct environment codes (prevents wrong codes when cloning)
        sidecar_config_list = await self._map_sidecars_to_environment(
            service_config_data.sidecar_config,
            service.applications_mst_code,
            service.resource_group_mst_code,
            service_config_data.environment.value
        )

        config_dict = await self._settle_routing(
            config_dict,
            tenant_code=tenant_code,
            services_mst_code=service_config_data.services_mst_code,
            language_ref_code=service_config_data.language_ref_code,
            infrastructuretype_ref_code=service_config_data.infrastructuretype_ref_code,
            alb_selection=alb_selection,
            service=service,
        )

        # Convert deployment_strategy to dict for JSONB storage
        deployment_strategy_dict = service_config_data.deployment_strategy.model_dump() if service_config_data.deployment_strategy else None

        # ── EFS Model Storage: resolve HF revision + stamp config for downstream generators ──
        if config_dict and config_dict.get("service_type") == "MODEL_SERVING":
            model_id = config_dict.get("model_name", "")
            logger.info(f"[EFS] Processing MODEL_SERVING config for model_id: {model_id}")
            if model_id:
                revision = await _resolve_hf_revision(model_id, hf_token=None)
                efs_path = build_efs_path(model_id, revision)
                job_name = _make_download_job_name(model_id, revision)
                config_dict["model_revision"] = revision
                config_dict["efs_path"] = efs_path
                config_dict["download_job_name"] = job_name
                logger.info(
                    "[EFS] Resolved model %s: revision=%s, efs_path=%s, job=%s",
                    model_id, revision, efs_path, job_name,
                )
            logger.info(f"[EFS] Final config dict for MODEL_SERVING: {efs_path}")

        # Create new service config
        new_config = ServiceConfigModel(
            code=code,
            name=name,
            tenant_mst_code=tenant_code,
            services_mst_code=service_config_data.services_mst_code,
            infrastructuretype_ref_code=service_config_data.infrastructuretype_ref_code,
            infra_vendor_enum=service_config_data.infra_vendor_enum,
            infrastructure_mst_code=service_config_data.infrastructure_mst_code,
            environment=service_config_data.environment,
            geo_loc_mst_code=service_config_data.geo_loc_mst_code,
            alb_selection=alb_selection,  # Set column value
            language_ref_code=service_config_data.language_ref_code,
            config=config_dict,
            sidecar_config=sidecar_config_list,
            deployment_strategy=deployment_strategy_dict,
            dockerfile=dockerfile_content,  # Save Dockerfile content for EKS
            is_active=True,
            is_deleted=False
        )

        # Set initial sync_status based on sync_to_github flag
        new_config.sync_status = "NEVER_SYNCED"

        # Add to session and commit
        self.service_config_repository.session.add(new_config)
        await self.service_config_repository.session.flush()

        logger.info(f"Successfully created service config: {new_config.code}")

        await self._map_config_to_resource_group_fga(
            service, new_config.code, tenant_code, user_code
        )

        # ── EFS Model Storage: create registry record if none exists ──
        if config_dict and config_dict.get("service_type") == "MODEL_SERVING":
            model_id = config_dict.get("model_name", "")
            revision = config_dict.get("model_revision", "")
            efs_path_val = config_dict.get("efs_path", "")
            if model_id and revision and efs_path_val:
                from app.repository.model_registry_repository import ModelRegistryRepository
                registry_repo = ModelRegistryRepository(self.session)
                existing = await registry_repo.get_shared(model_id, revision)
                if not existing:
                    await registry_repo.create_shared(model_id, revision, efs_path_val)
                    logger.info("[EFS] Created model_registry record: %s@%s", model_id, revision)

        # Only trigger Terragrunt sync if sync_to_github is True
        if service_config_data.sync_to_github:
            # Note: PR blocking removed - we now use smart PR replacement strategy
            # If existing open PR exists, sync will compare content and either:
            # - Skip if no changes (return existing PR info)
            # - Create new PR and close old one if content changed

            try:
                # Get service with application relationship loaded (relationships are loaded by default in get_by_code)
                service = await self.services_repository.get_by_code(
                    service_config_data.services_mst_code
                )

                if service:
                    # Route based on infrastructure type (EKS vs ECS)
                    infra_type_code = service_config_data.infrastructuretype_ref_code or ""

                    if infra_type_code == "eks_infrastructuretype_ref":
                        # EKS: Use EKS pipeline service (no Terragrunt sync, no Dockerfile sync)
                        print('************************  infrastructure type is eks (create)**************************************')
                        await self._sync_eks_pipeline(
                            service_config=new_config,
                            service=service,
                            tenant_code=tenant_code,
                            user_code=user_code
                        )
                        # Check if all PRs are merged before setting SYNCED
                        all_merged = await self._check_all_prs_merged(new_config)
                        logger.info(f"[{new_config.code}] CREATE EKS _check_all_prs_merged returned: {all_merged}")
                        if all_merged:
                            new_config.sync_status = "SYNCED"
                            logger.info(f"[{new_config.code}] CREATE EKS Set sync_status to SYNCED")
                        else:
                            new_config.sync_status = "PENDING_SYNC"
                            logger.info(f"[{new_config.code}] CREATE EKS Set sync_status to PENDING_SYNC")
                        logger.info(f"EKS pipeline sync completed for {new_config.code}")
                    else:
                        # ECS: Existing Terragrunt sync + pipeline sync + Dockerfile sync flow
                        print('************************  infrastructure type is ecs (create)**************************************')
                        terragrunt_service = TerragruntSyncService()

                        # Get GitHub repository from tenant config
                        tenant_cfg = await get_tenant_config(tenant_code, self.session)
                        github_repository = tenant_cfg.github_infra_repository
                        github_branch = tenant_cfg.github_infra_branch

                        # Load relationships for terragrunt sync (language_ref for template, infrastructure_type for branch naming)
                        relationships_to_load = ['infrastructure_type']
                        if new_config.language_ref_code:
                            relationships_to_load.append('language_ref')
                        await self.service_config_repository.session.refresh(new_config, relationships_to_load)

                        # Get GitHub token for PR validation
                        github_token = await terragrunt_service._get_github_token()

                        # Smart PR replacement: Look up existing open PRs (validates on GitHub)
                        existing_dockerfile_prs = await self._get_existing_dockerfile_prs_by_branch(
                            new_config, tenant_code, github_token=github_token
                        )
                        existing_terragrunt_pr = await self._get_existing_terragrunt_pr(
                            new_config, tenant_code, github_token=github_token, terragrunt_repo=github_repository
                        )

                        sync_result = await terragrunt_service.sync_config_to_hcl(
                            new_config,
                            service,  # Pass full service model instead of just name
                            tenant=tenant_code,
                            github_repository=github_repository if github_repository else None,
                            github_branch=github_branch,
                            push_to_github=True,  # Enable GitHub push
                            user_email=user_email,
                            existing_dockerfile_prs=existing_dockerfile_prs,
                            existing_terragrunt_pr=existing_terragrunt_pr
                        )
                        logger.info(f"Terragrunt sync result: {sync_result}")

                        # Update sync_status based on whether all PRs are merged
                        all_merged = await self._check_all_prs_merged(new_config)
                        logger.info(f"[{new_config.code}] CREATE ECS _check_all_prs_merged returned: {all_merged}")
                        if all_merged:
                            new_config.sync_status = "SYNCED"
                            logger.info(f"[{new_config.code}] CREATE ECS Set sync_status to SYNCED")
                        else:
                            new_config.sync_status = "PENDING_SYNC"
                            logger.info(f"[{new_config.code}] CREATE ECS Set sync_status to PENDING_SYNC")

                        # Create/Update GitOps workflow tracking if PR was created
                        # Note: sync_result has nested github_sync with PR details
                        github_sync = sync_result.get("github_sync") if sync_result else {}
                        if github_sync and github_sync.get("pr_number"):
                            try:
                                pr_number = github_sync.get("pr_number")

                                # Smart PR replacement: Mark old workflow as closed if replaced
                                old_terragrunt_workflow_id = github_sync.get("old_workflow_id")
                                if old_terragrunt_workflow_id:
                                    await self.gitops_workflow_repository.update_pr_status(
                                        workflow_id=old_terragrunt_workflow_id,
                                        new_status=PRStatusEnum.PR_CLOSED
                                    )
                                    logger.info(f"Marked old Terragrunt workflow {old_terragrunt_workflow_id} as PR_CLOSED (superseded)")

                                # Check if workflow already exists for this PR (avoid duplicates)
                                existing_workflow = await self.gitops_workflow_repository.get_by_transaction_and_pr(
                                    transaction_code=new_config.code,
                                    table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                                    pr_number=pr_number,
                                    tenant_code=tenant_code
                                )

                                if existing_workflow:
                                    # Update existing workflow with new commit SHA
                                    existing_workflow.git_commit_sha = github_sync.get("commit_sha")
                                    existing_workflow.git_branch = github_sync.get("feature_branch")
                                    self.session.add(existing_workflow)
                                    await self.session.commit()
                                    await self.session.refresh(new_config)
                                    logger.info(f"GitOps workflow updated: {existing_workflow.code} (PR #{pr_number})")
                                else:
                                    # Create new workflow record
                                    workflow_data = make_gitops_workflow_detail(
                                        git_repository=github_repository,
                                        git_branch=github_sync.get("feature_branch"),
                                        git_commit_sha=github_sync.get("commit_sha"),
                                        pr_number=pr_number,
                                        pr_url=github_sync.get("pr_url"),
                                        tenant_mst_code=tenant_code,
                                        user_mst_code=user_code,
                                        workflow_name=f"ECS Service Config: {service_config_data.services_mst_code}",
                                        transaction_code=new_config.code,
                                        table_name=WorkflowSourceTableEnum.SERVICE_CONFIG
                                    )
                                    workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                    # Link service config to workflow
                                    await self.service_config_repository.link_to_gitops_workflow(
                                        service_config_ids=[new_config.id],
                                        workflow_id=workflow.id
                                    )
                                    await self.session.commit()
                                    # Refresh to reload attributes after commit (prevents MissingGreenlet error)
                                    await self.session.refresh(new_config)
                                    logger.info(f"GitOps workflow created and linked: {workflow.code}")
                            except Exception as e:
                                logger.error(f"Failed to create workflow tracking: {e}")
                                # Non-fatal - config exists, just tracking failed

                        # Save Dockerfile PR results to gitops_workflow_detail table and link via junction table
                        dockerfile_sync = github_sync.get("dockerfile_modifications", {})

                        # Always update dockerfile_path when generate_dockerfile is enabled
                        # This ensures pipeline path filters are correct even if Dockerfile already existed
                        config_dict = new_config.config or {}
                        if config_dict.get("generate_dockerfile", False):
                            from app.utils.language_helpers import get_dockerfile_path, is_java_language
                            from app.repository.language_ref_repository import LanguageRefRepository
                            # Compute the expected dockerfile_path
                            # Use language_ref_code to look up language name (avoids MissingGreenlet error)
                            language_name = None
                            if new_config.language_ref_code:
                                lang_repo = LanguageRefRepository(self.session)
                                lang_ref = await lang_repo.get_by_code(new_config.language_ref_code)
                                language_name = lang_ref.name if lang_ref else None
                            is_java = is_java_language(language_name) if language_name else False
                            expected_dockerfile_path = get_dockerfile_path(
                                service_name=service.name,
                                build_path=config_dict.get("build_path"),
                                is_java=is_java
                            )
                            new_config.config["dockerfile_path"] = expected_dockerfile_path
                            logger.info(f"Set dockerfile_path for generate_dockerfile: {expected_dockerfile_path}")
                            # Commit the dockerfile_path update immediately (ensures pipeline sync has correct path)
                            await self.session.commit()
                            await self.session.refresh(new_config)
                        elif dockerfile_sync and dockerfile_sync.get("dockerfile_path"):
                            # Fallback: use dockerfile_path from sync result
                            if new_config.config is None:
                                new_config.config = {}
                            new_config.config["dockerfile_path"] = dockerfile_sync.get("dockerfile_path")
                            logger.info(f"Updated dockerfile_path from sync result: {dockerfile_sync.get('dockerfile_path')}")
                            # Commit the dockerfile_path update immediately
                            await self.session.commit()
                            await self.session.refresh(new_config)

                        if dockerfile_sync and dockerfile_sync.get("branches"):
                            try:
                                service_repo = new_config.config.get("repository", "") if new_config.config else ""
                                linked_count = 0
                                updated_count = 0
                                for branch_result in dockerfile_sync.get("branches", []):
                                    # Only create/update workflow if PR was actually created
                                    if branch_result.get("pr_number"):
                                        # Generate unique code for dockerfile workflow using hash
                                        branch_name = branch_result.get("branch")
                                        unique_key = f"{new_config.code}_{branch_name}_{service_repo}"
                                        dockerfile_workflow_code = f"SCDF_{hashlib.md5(unique_key.encode()).hexdigest()[:8]}"
                                        dockerfile_workflow_name = f"Dockerfile: {service_config_data.services_mst_code} - {branch_name}"
                                        pr_number = branch_result.get("pr_number")

                                        # Smart PR replacement: Mark old workflow as closed if replaced
                                        old_workflow_id = branch_result.get("old_workflow_id")
                                        if old_workflow_id:
                                            await self.gitops_workflow_repository.update_pr_status(
                                                workflow_id=old_workflow_id,
                                                new_status=PRStatusEnum.PR_CLOSED
                                            )
                                            logger.info(f"Marked old workflow {old_workflow_id} as PR_CLOSED (superseded)")

                                        # Check if workflow already exists for this PR (avoid duplicates)
                                        existing_workflow = await self.gitops_workflow_repository.get_by_transaction_and_pr(
                                            transaction_code=dockerfile_workflow_code,
                                            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                                            pr_number=pr_number,
                                            tenant_code=tenant_code
                                        )

                                        if existing_workflow:
                                            # Update existing workflow with new commit SHA
                                            existing_workflow.git_commit_sha = branch_result.get("commit_sha")
                                            existing_workflow.git_branch = branch_result.get("feature_branch") or branch_name
                                            self.session.add(existing_workflow)
                                            updated_count += 1
                                        else:
                                            # Create gitops_workflow_detail record
                                            workflow_data = make_gitops_workflow_detail(
                                                git_repository=service_repo,
                                                git_branch=branch_result.get("feature_branch") or branch_name,
                                                git_commit_sha=branch_result.get("commit_sha"),
                                                pr_number=pr_number,
                                                pr_url=branch_result.get("pr_url"),
                                                tenant_mst_code=tenant_code,
                                                user_mst_code=user_code,
                                                workflow_name=dockerfile_workflow_name,
                                                transaction_code=dockerfile_workflow_code,
                                                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
                                            )
                                            workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                            # Link via junction table
                                            await self.dockerfile_workflow_repository.link_workflow(
                                                service_config_id=new_config.id,
                                                gitops_workflow_id=workflow.id,
                                                branch=branch_name,
                                                repository=service_repo,
                                                code=dockerfile_workflow_code,
                                                name=dockerfile_workflow_name
                                            )
                                            linked_count += 1

                                await self.session.commit()
                                await self.session.refresh(new_config)
                                logger.info(f"Dockerfile workflows: {linked_count} created, {updated_count} updated")
                            except Exception as e:
                                logger.error(f"Failed to save Dockerfile workflows: {e}")
                                # Non-fatal - config exists, just tracking failed

                        # Pipeline sync for each branch in config.branches
                        # Pass token from Terragrunt sync to avoid duplicate GitHub App token requests
                        if new_config.config and new_config.config.get("branches"):
                            try:
                                terragrunt_github_token = github_sync.get("github_token") if github_sync else None
                                pipeline_sync_result = await self._sync_pipelines_for_branches(
                                    service_config=new_config,
                                    service=service,
                                    tenant_code=tenant_code,
                                    user_code=user_code,
                                    github_token=terragrunt_github_token
                                )
                                logger.info(f"Pipeline sync result: {pipeline_sync_result.get('successful', 0)} successful, {pipeline_sync_result.get('errors', 0)} errors")
                            except Exception as e:
                                logger.error(f"Pipeline sync failed: {e}")
                                # Non-fatal - service config already created
                else:
                    logger.warning(f"Service not found for Terragrunt sync: {service_config_data.services_mst_code}")
            except Exception as e:
                logger.error(f"Terragrunt sync failed: {e}")
                # Don't fail the save operation if Terragrunt sync fails
        else:
            logger.info(f"Skipping Terragrunt sync for {new_config.code} (sync_to_github=False)")

        # Refresh config to ensure all attributes are loaded (prevents MissingGreenlet on expired objects)
        await self.session.refresh(new_config)

        # Fire-and-forget: Index config to Qdrant for semantic search
        # Extract service_name before creating task to avoid accessing expired SQLAlchemy objects
        service_name = service.name if service else None
        asyncio.create_task(self._index_config_to_vector_db(new_config, service_name))

        return await self._build_service_config_response(new_config)

    async def upsert_service_config(
        self,
        tenant_code: str,
        service_config_data: ServiceConfigCreate,
        user_email: str = None,
        user_code: str = None
    ) -> ServiceConfigResponse:
        """
        Upsert a service configuration (create if not exists, update if exists).

        This endpoint saves the configuration to database WITHOUT triggering:
        - GitHub operations (PR creation)
        - Terragrunt HCL generation
        - GitOps workflow records
        - Pipeline sync
        - Dockerfile workflow

        This is essentially a "Save Draft" functionality.

        Args:
            tenant_code: Tenant code (from JWT)
            service_config_data: Service configuration data (uses ServiceConfigCreate schema)
            user_email: User email (from JWT) for logging
            user_code: User code (from JWT) for logging

        Returns:
            Created or updated ServiceConfigResponse

        Raises:
            HTTPException: If validation fails
        """
        logger.info(f"Upserting service config for tenant={tenant_code}, service={service_config_data.services_mst_code}")

        # Get alb_selection from config (default to existing_alb)
        alb_selection = "existing_alb"
        if service_config_data.config:
            alb_selection = service_config_data.config.alb_selection or "existing_alb"

        # Check if configuration already exists
        existing_config = await self.service_config_repository.get_by_tenant_service_env_geo_loc(
            tenant_code,
            service_config_data.services_mst_code,
            service_config_data.environment.value,
            service_config_data.geo_loc_mst_code,
            alb_selection,
            infra_vendor=service_config_data.infra_vendor_enum.value if service_config_data.infra_vendor_enum else None,
            infrastructure_type=service_config_data.infrastructuretype_ref_code,
            infrastructure_mst_code=service_config_data.infrastructure_mst_code
        )

        # Common validations for both create and update
        # Validate sidecar codes if provided
        if service_config_data.sidecar_config:
            await self._validate_sidecar_codes(service_config_data.sidecar_config)

        # build_path by language: Python has none; Go stores `./cmd`; others `cmd`
        # (build_path_for_language is the single rule, shared with the queue route).
        if service_config_data.config and service_config_data.config.build_path and service_config_data.language_ref_code:
            language_ref = await self.language_ref_repository.get_by_code(service_config_data.language_ref_code)
            if language_ref and language_ref.name.lower().startswith("python"):
                service_config_data.config.build_path = None
            elif language_ref:
                service_config_data.config.build_path = build_path_for_language(
                    service_config_data.config.build_path, is_go_language(language_ref.name)
                )

        # Convert config to dict for JSONB storage (exclude_unset preserves cluster context on updates)
        config_dict = service_config_data.config.model_dump(exclude_unset=True) if service_config_data.config else None

        # Extract dockerfile_content from config for EKS
        dockerfile_content = None
        if config_dict and service_config_data.infrastructuretype_ref_code == "eks_infrastructuretype_ref":
            generate_dockerfile = config_dict.get("generate_dockerfile", False)
            dockerfile_content_from_request = config_dict.get("dockerfile_content")

            if not generate_dockerfile and dockerfile_content_from_request:
                dockerfile_content = config_dict.pop("dockerfile_content")
                logger.info(f"[DOCKERFILE_SAVE] Saving custom Dockerfile to DB (generate_dockerfile=False)")
            else:
                config_dict.pop("dockerfile_content", None)
                if generate_dockerfile:
                    logger.info(f"[DOCKERFILE_SAVE] Not saving Dockerfile (generate_dockerfile=True, will generate dynamically)")

            # Auto-assign ingress_group_order only when not already persisted.
            # Use service_config_data directly — env_val/geo_loc are not yet
            # in scope at this point in upsert_service_config.
            _env_val = service_config_data.environment.value
            _geo_loc = service_config_data.geo_loc_mst_code
            existing_order = (
                existing_config.config.get("ingress_group_order") if existing_config and existing_config.config else None
            )
            if not existing_order and not config_dict.get("ingress_group_order"):
                config_dict["ingress_group_order"] = await _assign_ingress_group_order(
                    session=self.session,
                    tenant_code=tenant_code,
                    environment=_env_val,
                    geo_loc=_geo_loc,
                    infrastructure_mst_code=service_config_data.infrastructure_mst_code,
                    exclude_service_code=service_config_data.services_mst_code,
                )
                logger.info(
                    "[INGRESS_ORDER] Auto-assigned ingress_group_order=%s for %s/%s/%s/%s",
                    config_dict["ingress_group_order"], tenant_code, _env_val, _geo_loc,
                    service_config_data.infrastructure_mst_code,
                )

        if existing_config:
            # UPDATE PATH: Update existing configuration
            logger.info(f"Updating existing service config: {existing_config.code}")

            # ALB Lock: Check if ALB selection is being changed (not allowed)
            if existing_config.config and config_dict:
                existing_alb = existing_config.config.get("alb_selection")
                new_alb = service_config_data.config.alb_selection
                if existing_alb and new_alb and existing_alb != new_alb:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="ALB selection cannot be changed after initial configuration"
                    )

            # Get service for sidecar mapping
            service = await self.services_repository.get_by_code(existing_config.services_mst_code)

            # Map sidecars to correct environment codes
            sidecar_config_list = await self._map_sidecars_to_environment(
                service_config_data.sidecar_config,
                service.applications_mst_code,
                service.resource_group_mst_code,
                existing_config.environment.value
            )

            # Convert deployment_strategy to dict
            deployment_strategy_dict = service_config_data.deployment_strategy.model_dump() if service_config_data.deployment_strategy else None

            config_dict = await self._settle_routing(
                config_dict,
                tenant_code=tenant_code,
                services_mst_code=existing_config.services_mst_code,
                language_ref_code=service_config_data.language_ref_code or existing_config.language_ref_code,
                infrastructuretype_ref_code=(
                    service_config_data.infrastructuretype_ref_code or existing_config.infrastructuretype_ref_code
                ),
                alb_selection=alb_selection,
                existing=existing_config,
                service=service,
            )

            # ── EFS Model Storage: re-resolve revision on upsert update ──
            if config_dict and config_dict.get("service_type") == "MODEL_SERVING":
                model_id = config_dict.get("model_name", "")
                if model_id:
                    revision = await _resolve_hf_revision(model_id, hf_token=None)
                    efs_path = build_efs_path(model_id, revision)
                    job_name = _make_download_job_name(model_id, revision)
                    config_dict["model_revision"] = revision
                    config_dict["efs_path"] = efs_path
                    config_dict["download_job_name"] = job_name
                    # Pre-calculate KServe URL: https://{tenant}-{service}-predictor.models.devlift.ai
                    # Prefer service_name (human-readable canvas label) over services_mst_code (UUID).
                    import re as _re
                    _svc = config_dict.get("service_name") or existing_config.config.get("service_name") or ""
                    if _svc.lower().endswith("-service"):
                        _svc = _svc[:-8]
                    _safe = _re.sub(r"[^a-z0-9-]", "-", _svc.lower()).strip("-")
                    config_dict["alb_url"] = f"https://{tenant_code}-{_safe}-predictor.models.devlift.ai"
                    logger.info(
                        "[EFS] Upsert update resolved model %s: revision=%s, efs_path=%s, url=%s",
                        model_id, revision, efs_path, config_dict["alb_url"],
                    )

            # Call repository update method
            updated_config = await self.service_config_repository.update_service_config_fields(
                config=existing_config,
                infrastructuretype_ref_code=service_config_data.infrastructuretype_ref_code,
                infra_vendor_enum=service_config_data.infra_vendor_enum.value if service_config_data.infra_vendor_enum else None,
                infrastructure_mst_code=service_config_data.infrastructure_mst_code,
                language_ref_code=service_config_data.language_ref_code,
                config_dict=config_dict,
                sidecar_config=sidecar_config_list,
                deployment_strategy=deployment_strategy_dict,
                dockerfile=dockerfile_content,
                sync_status="SAVED"
            )

            logger.info(f"Successfully updated service config: {updated_config.code}")

            # Refresh config to ensure all attributes are loaded
            await self.session.refresh(updated_config)

            # Fire-and-forget: Index config to Qdrant for semantic search
            service_name = service.name if service else None
            asyncio.create_task(self._index_config_to_vector_db(updated_config, service_name))

            return await self._build_service_config_response(updated_config)

        else:
            # CREATE PATH: Create new configuration
            logger.info(f"Creating new service config for service={service_config_data.services_mst_code}")

            # Handle listener priority - check availability and clear if conflict
            if service_config_data.config and service_config_data.config.listener_rule_priority:
                service_for_validation = await self.services_repository.get_by_code(service_config_data.services_mst_code)
                svc_type = service_for_validation.service_type.value if hasattr(service_for_validation.service_type, 'value') else str(service_for_validation.service_type)
                priority_check = await self.check_listener_priority_availability(
                    tenant_code=tenant_code,
                    listener_priority=service_config_data.config.listener_rule_priority,
                    environment=service_config_data.environment.value,
                    application_code=service_for_validation.applications_mst_code,
                    geo_loc_mst_code=service_config_data.geo_loc_mst_code,
                    service_type=svc_type
                )
                if not priority_check.get("valid", True):
                    service_config_data.config.listener_rule_priority = None

            # Auto-generate code and name
            infra_type_ref = service_config_data.infrastructuretype_ref_code
            infra_type_short = infra_type_ref.replace("_infrastructuretype_ref", "") if infra_type_ref else "unknown"
            infra_mst_code = service_config_data.infrastructure_mst_code or ""

            svc_code = service_config_data.services_mst_code
            env_val = service_config_data.environment.value
            geo_loc = service_config_data.geo_loc_mst_code
            unique_key = (
                f"{svc_code}-{env_val}-{geo_loc}-"
                f"{alb_selection}-{infra_type_short}-{infra_mst_code}"
            )
            hash_suffix = hashlib.md5(unique_key.encode()).hexdigest()[:8]
            service_code_truncated = svc_code[:50]
            code = f"sc-{service_code_truncated}-{env_val}-{hash_suffix}"
            name = (
                f"Config for {svc_code} - {env_val} - "
                f"{geo_loc} - {alb_selection} - {infra_type_short}"
            )

            # Get service for sidecar mapping
            service = await self.services_repository.get_by_code(service_config_data.services_mst_code)

            # Map sidecars to correct environment codes
            sidecar_config_list = await self._map_sidecars_to_environment(
                service_config_data.sidecar_config,
                service.applications_mst_code,
                service.resource_group_mst_code,
                service_config_data.environment.value
            )

            # Convert deployment_strategy to dict
            deployment_strategy_dict = service_config_data.deployment_strategy.model_dump() if service_config_data.deployment_strategy else None

            config_dict = await self._settle_routing(
                config_dict,
                tenant_code=tenant_code,
                services_mst_code=service_config_data.services_mst_code,
                language_ref_code=service_config_data.language_ref_code,
                infrastructuretype_ref_code=service_config_data.infrastructuretype_ref_code,
                alb_selection=alb_selection,
            )

            # ── EFS Model Storage: resolve HF revision + stamp config for MODEL_SERVING ──
            if config_dict and config_dict.get("service_type") == "MODEL_SERVING":
                model_id = config_dict.get("model_name", "")
                if model_id:
                    revision = await _resolve_hf_revision(model_id, hf_token=None)
                    efs_path = build_efs_path(model_id, revision)
                    job_name = _make_download_job_name(model_id, revision)
                    config_dict["model_revision"] = revision
                    config_dict["efs_path"] = efs_path
                    config_dict["download_job_name"] = job_name
                    # Pre-calculate KServe URL: https://{tenant}-{service}-predictor.models.devlift.ai
                    import re as _re
                    _svc = config_dict.get("service_name") or service_config_data.services_mst_code or ""
                    if _svc.lower().endswith("-service"):
                        _svc = _svc[:-8]
                    _safe = _re.sub(r"[^a-z0-9-]", "-", _svc.lower()).strip("-")
                    config_dict["alb_url"] = f"https://{tenant_code}-{_safe}-predictor.models.devlift.ai"
                    logger.info(
                        "[EFS] Upsert resolved model %s: revision=%s, efs_path=%s, job=%s, url=%s",
                        model_id, revision, efs_path, job_name, config_dict["alb_url"],
                    )

            # Call repository save method
            new_config = await self.service_config_repository.save_service_config(
                code=code,
                name=name,
                tenant_mst_code=tenant_code,
                services_mst_code=service_config_data.services_mst_code,
                infrastructuretype_ref_code=service_config_data.infrastructuretype_ref_code,
                infra_vendor_enum=service_config_data.infra_vendor_enum.value if service_config_data.infra_vendor_enum else None,
                infrastructure_mst_code=service_config_data.infrastructure_mst_code,
                environment=service_config_data.environment,
                geo_loc_mst_code=service_config_data.geo_loc_mst_code,
                alb_selection=alb_selection,
                language_ref_code=service_config_data.language_ref_code,
                config=config_dict,
                sidecar_config=sidecar_config_list,
                deployment_strategy=deployment_strategy_dict,
                dockerfile=dockerfile_content,
                sync_status="SAVED"
            )

            logger.info(f"Successfully created service config: {new_config.code}")

            # Refresh config to ensure all attributes are loaded
            await self.session.refresh(new_config)

            # Same mapping create_service_config does — this is the OTHER path
            # that creates a service_configs row (the draft-save upsert and the
            # MCP dispatcher land here). Without it a config created this way
            # has no parent tuple and no admin group, so every FGA-carded route
            # denies it while the row sits happily in the DB.
            await self._map_config_to_resource_group_fga(
                service, new_config.code, tenant_code, user_code
            )

            # Fire-and-forget: Index config to Qdrant for semantic search
            service_name = service.name if service else None
            asyncio.create_task(self._index_config_to_vector_db(new_config, service_name))

            return await self._build_service_config_response(new_config)

    async def update_service_config(
        self,
        tenant_code: str,
        code: str,
        service_config_data: ServiceConfigUpdate,
        user_email: str = None,
        user_code: str = None
    ) -> ServiceConfigResponse:
        """
        Update an existing service configuration.

        Args:
            tenant_code: Tenant code (from JWT)
            code: Service configuration code
            service_config_data: Service configuration update data
            user_email: User email (from JWT) for PR attribution
            user_code: User code (from JWT) for workflow tracking

        Returns:
            Updated ServiceConfigResponse

        Raises:
            HTTPException: If config not found or validation fails
        """
        logger.info(f"Updating service config: {code} for tenant={tenant_code}")

        # Find existing configuration with tenant filtering
        existing_config = await self.service_config_repository.get_by_code_and_tenant(code, tenant_code)

        if not existing_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service configuration with code {code} not found"
            )

        # ALB Lock: Check if ALB selection is being changed (not allowed after initial config)
        if existing_config.config and service_config_data.config:
            existing_alb = existing_config.config.get("alb_selection")
            new_alb = service_config_data.config.alb_selection
            if existing_alb and new_alb and existing_alb != new_alb:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="ALB selection cannot be changed after initial configuration"
                )

        # Validate listener priority uniqueness (scoped by tenant + environment + application + region + ALB type)
        if service_config_data.config and service_config_data.config.listener_rule_priority:
            # Get service to find application_code and service_type for validation scope
            service_for_validation = await self.services_repository.get_by_code(existing_config.services_mst_code)
            # Use existing config's environment since we're updating
            env_value = existing_config.environment.value if hasattr(existing_config.environment, 'value') else str(existing_config.environment)
            # Get service_type for ALB scoping (OPS_TOOLS uses different ALB)
            svc_type = service_for_validation.service_type.value if hasattr(service_for_validation.service_type, 'value') else str(service_for_validation.service_type)
            await self._validate_listener_priority(
                tenant_code=tenant_code,
                listener_priority=service_config_data.config.listener_rule_priority,
                environment=env_value,
                application_code=service_for_validation.applications_mst_code,
                geo_loc_mst_code=existing_config.geo_loc_mst_code,
                exclude_service_code=existing_config.services_mst_code,
                service_type=svc_type
            )

        # Validate sidecar codes if provided
        if service_config_data.sidecar_config is not None:
            await self._validate_sidecar_codes(service_config_data.sidecar_config)

        # build_path by language: Python has none; Go stores `./cmd`; others `cmd`
        # (build_path_for_language is the single rule, shared with the queue route).
        language_ref_code = service_config_data.language_ref_code or existing_config.language_ref_code
        if service_config_data.config and service_config_data.config.build_path and language_ref_code:
            language_ref = await self.language_ref_repository.get_by_code(language_ref_code)
            if language_ref and language_ref.name.lower().startswith("python"):
                service_config_data.config.build_path = None
            elif language_ref:
                service_config_data.config.build_path = build_path_for_language(
                    service_config_data.config.build_path, is_go_language(language_ref.name)
                )

        # Update infrastructure type if provided
        if service_config_data.infrastructuretype_ref_code is not None:
            existing_config.infrastructuretype_ref_code = service_config_data.infrastructuretype_ref_code

        # Update infra_vendor_enum if provided
        if service_config_data.infra_vendor_enum is not None:
            existing_config.infra_vendor_enum = service_config_data.infra_vendor_enum

        # Update infrastructure_mst_code if provided
        if service_config_data.infrastructure_mst_code is not None:
            existing_config.infrastructure_mst_code = service_config_data.infrastructure_mst_code

        # Update language_ref_code if provided
        if service_config_data.language_ref_code is not None:
            existing_config.language_ref_code = service_config_data.language_ref_code

        # Update config if provided
        if service_config_data.config is not None:
            config_dict = service_config_data.config.model_dump()
            config_dict = await self._settle_routing(
                config_dict,
                tenant_code=tenant_code,
                services_mst_code=existing_config.services_mst_code,
                language_ref_code=existing_config.language_ref_code,
                infrastructuretype_ref_code=existing_config.infrastructuretype_ref_code,
                alb_selection=existing_config.alb_selection,
                existing=existing_config,
            )

            # Extract dockerfile_content from config for EKS
            # Only save to dockerfile column when generate_dockerfile is FALSE (disabled)
            if existing_config.infrastructuretype_ref_code == "eks_infrastructuretype_ref":
                generate_dockerfile = config_dict.get("generate_dockerfile", False)
                dockerfile_content_from_request = config_dict.get("dockerfile_content")

                # Only save dockerfile_content if generate_dockerfile is False (disabled) AND dockerfile_content is provided
                if not generate_dockerfile and dockerfile_content_from_request:
                    dockerfile_content = config_dict.pop("dockerfile_content")
                    existing_config.dockerfile = dockerfile_content
                    logger.info(f"[DOCKERFILE_SAVE] Updating custom Dockerfile in DB (generate_dockerfile=False)")
                else:
                    # Remove dockerfile_content from config (don't save in JSONB)
                    config_dict.pop("dockerfile_content", None)
                    if generate_dockerfile:
                        logger.info(f"[DOCKERFILE_SAVE] Not saving Dockerfile (generate_dockerfile=True, will generate dynamically)")
                    # Note: We don't clear existing dockerfile column if generate_dockerfile=True
                    # to preserve previously saved custom Dockerfiles

            # ── EFS Model Storage: re-resolve revision when model config changes ──
            if config_dict.get("service_type") == "MODEL_SERVING":
                model_id = config_dict.get("model_name", "")
                if model_id:
                    revision = await _resolve_hf_revision(model_id, hf_token=None)
                    efs_path = build_efs_path(model_id, revision)
                    job_name = _make_download_job_name(model_id, revision)
                    config_dict["model_revision"] = revision
                    config_dict["efs_path"] = efs_path
                    config_dict["download_job_name"] = job_name
                    logger.info(
                        "[EFS] Update resolved model %s: revision=%s, efs_path=%s",
                        model_id, revision, efs_path,
                    )

            existing_config.config = config_dict

        # Update sidecar_config if provided - map to correct environment codes
        if service_config_data.sidecar_config is not None:
            # Get service to find app_code and rg_code for sidecar mapping
            service = await self.services_repository.get_by_code(existing_config.services_mst_code)

            # Map sidecars to correct environment codes (prevents wrong codes when cloning)
            existing_config.sidecar_config = await self._map_sidecars_to_environment(
                service_config_data.sidecar_config,
                service.applications_mst_code,
                service.resource_group_mst_code,
                existing_config.environment.value
            )

        # Update deployment_strategy if provided
        if service_config_data.deployment_strategy is not None:
            existing_config.deployment_strategy = service_config_data.deployment_strategy.model_dump()

        # Set sync_status based on sync_to_github flag
        # If not syncing, mark as PENDING_SYNC (there are unsynced changes)
        if not service_config_data.sync_to_github:
            existing_config.sync_status = "PENDING_SYNC"

        # Commit changes to database
        self.service_config_repository.session.add(existing_config)
        await self.service_config_repository.session.flush()

        logger.info(f"Successfully updated service config: {code}")

        # Only trigger Terragrunt sync if sync_to_github is True
        if service_config_data.sync_to_github:
            # Note: PR blocking removed - we now use smart PR replacement strategy
            # If existing open PR exists, sync will compare content and either:
            # - Skip if no changes (return existing PR info)
            # - Create new PR and close old one if content changed

            try:
                # Get service with application relationship loaded (relationships are loaded by default in get_by_code)
                service = await self.services_repository.get_by_code(
                    existing_config.services_mst_code
                )

                if service:
                    # Route based on infrastructure type (EKS vs ECS)
                    infra_type_code = existing_config.infrastructuretype_ref_code or ""

                    if infra_type_code == "eks_infrastructuretype_ref":
                        print('************************  infrastructure type is eks (update)**************************************')
                        # EKS: Use EKS pipeline service (no Terragrunt sync, no Dockerfile sync)
                        await self._sync_eks_pipeline(
                            service_config=existing_config,
                            service=service,
                            tenant_code=tenant_code,
                            user_code=user_code
                        )
                        # Check if all PRs are merged before setting SYNCED
                        all_merged = await self._check_all_prs_merged(existing_config)
                        logger.info(f"[{existing_config.code}] EKS _check_all_prs_merged returned: {all_merged}")
                        if all_merged:
                            existing_config.sync_status = "SYNCED"
                            logger.info(f"[{existing_config.code}] EKS Set sync_status to SYNCED")
                        else:
                            existing_config.sync_status = "PENDING_SYNC"
                            logger.info(f"[{existing_config.code}] EKS Set sync_status to PENDING_SYNC")
                        logger.info(f"EKS pipeline sync completed for {existing_config.code}")
                    else:
                        print('************************  infrastructure type is ecs (update)**************************************')
                        # ECS: Existing Terragrunt sync + pipeline sync + Dockerfile sync flow
                        terragrunt_service = TerragruntSyncService()

                        # Get GitHub repository from tenant config
                        tenant_cfg = await get_tenant_config(tenant_code, self.session)
                        github_repository = tenant_cfg.github_infra_repository
                        github_branch = tenant_cfg.github_infra_branch

                        # Load relationships for terragrunt sync (language_ref for template, infrastructure_type for branch naming)
                        relationships_to_load = ['infrastructure_type']
                        if existing_config.language_ref_code:
                            relationships_to_load.append('language_ref')
                        await self.service_config_repository.session.refresh(existing_config, relationships_to_load)

                        # Get GitHub token for PR validation
                        github_token = await terragrunt_service._get_github_token()

                        # Smart PR replacement: Look up existing open PRs (validates on GitHub)
                        existing_dockerfile_prs = await self._get_existing_dockerfile_prs_by_branch(
                            existing_config, tenant_code, github_token=github_token
                        )
                        existing_terragrunt_pr = await self._get_existing_terragrunt_pr(
                            existing_config, tenant_code, github_token=github_token, terragrunt_repo=github_repository
                        )

                        sync_result = await terragrunt_service.sync_config_to_hcl(
                            existing_config,
                            service,  # Pass full service model instead of just name
                            tenant=tenant_code,
                            github_repository=github_repository if github_repository else None,
                            github_branch=github_branch,
                            push_to_github=True,  # Enable GitHub push
                            user_email=user_email,
                            existing_dockerfile_prs=existing_dockerfile_prs,
                            existing_terragrunt_pr=existing_terragrunt_pr
                        )
                        logger.info(f"Terragrunt sync result: {sync_result}")

                        # Update sync_status based on whether all PRs are merged
                        all_merged = await self._check_all_prs_merged(existing_config)
                        logger.info(f"[{existing_config.code}] _check_all_prs_merged returned: {all_merged}")
                        if all_merged:
                            existing_config.sync_status = "SYNCED"
                            logger.info(f"[{existing_config.code}] Set sync_status to SYNCED")
                        else:
                            existing_config.sync_status = "PENDING_SYNC"
                            logger.info(f"[{existing_config.code}] Set sync_status to PENDING_SYNC")

                        # Create/Update GitOps workflow tracking if PR was created
                        # Note: sync_result has nested github_sync with PR details
                        github_sync = sync_result.get("github_sync") if sync_result else {}
                        if github_sync and github_sync.get("pr_number"):
                            try:
                                pr_number = github_sync.get("pr_number")

                                # Smart PR replacement: Mark old workflow as closed if replaced
                                old_terragrunt_workflow_id = github_sync.get("old_workflow_id")
                                if old_terragrunt_workflow_id:
                                    await self.gitops_workflow_repository.update_pr_status(
                                        workflow_id=old_terragrunt_workflow_id,
                                        new_status=PRStatusEnum.PR_CLOSED
                                    )
                                    logger.info(f"Marked old Terragrunt workflow {old_terragrunt_workflow_id} as PR_CLOSED (superseded)")

                                # Check if workflow already exists for this PR (avoid duplicates)
                                existing_workflow = await self.gitops_workflow_repository.get_by_transaction_and_pr(
                                    transaction_code=existing_config.code,
                                    table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                                    pr_number=pr_number,
                                    tenant_code=tenant_code
                                )

                                if existing_workflow:
                                    # Update existing workflow with new commit SHA
                                    existing_workflow.git_commit_sha = github_sync.get("commit_sha")
                                    existing_workflow.git_branch = github_sync.get("feature_branch")
                                    self.session.add(existing_workflow)
                                    await self.session.commit()
                                    await self.session.refresh(existing_config)
                                    logger.info(f"GitOps workflow updated: {existing_workflow.code} (PR #{pr_number})")
                                else:
                                    # Create new workflow record
                                    workflow_data = make_gitops_workflow_detail(
                                        git_repository=github_repository,
                                        git_branch=github_sync.get("feature_branch"),
                                        git_commit_sha=github_sync.get("commit_sha"),
                                        pr_number=pr_number,
                                        pr_url=github_sync.get("pr_url"),
                                        tenant_mst_code=tenant_code,
                                        user_mst_code=user_code,
                                        workflow_name=f"ECS Service Config Update: {existing_config.services_mst_code}",
                                        transaction_code=existing_config.code,
                                        table_name=WorkflowSourceTableEnum.SERVICE_CONFIG
                                    )
                                    workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                    # Link service config to workflow
                                    await self.service_config_repository.link_to_gitops_workflow(
                                        service_config_ids=[existing_config.id],
                                        workflow_id=workflow.id
                                    )
                                    await self.session.commit()
                                    # Refresh to reload attributes after commit (prevents MissingGreenlet error)
                                    await self.session.refresh(existing_config)
                                    logger.info(f"GitOps workflow created and linked: {workflow.code}")
                            except Exception as e:
                                logger.error(f"Failed to create workflow tracking: {e}")
                                # Non-fatal - config exists, just tracking failed

                        # Save Dockerfile PR results to gitops_workflow_detail table and link via junction table
                        dockerfile_sync = github_sync.get("dockerfile_modifications", {})
                        logger.info(f"=== DOCKERFILE SYNC DEBUG (UPDATE) ===")
                        logger.info(f"dockerfile_sync.branches: {dockerfile_sync.get('branches') if dockerfile_sync else 'N/A'}")

                        # Always update dockerfile_path when generate_dockerfile is enabled
                        # This ensures pipeline path filters are correct even if Dockerfile already existed
                        config_dict = existing_config.config or {}
                        if config_dict.get("generate_dockerfile", False):
                            from app.utils.language_helpers import get_dockerfile_path, is_java_language
                            from app.repository.language_ref_repository import LanguageRefRepository
                            # Compute the expected dockerfile_path
                            # Use language_ref_code to look up language name (avoids MissingGreenlet error)
                            language_name = None
                            if existing_config.language_ref_code:
                                lang_repo = LanguageRefRepository(self.session)
                                lang_ref = await lang_repo.get_by_code(existing_config.language_ref_code)
                                language_name = lang_ref.name if lang_ref else None
                            is_java = is_java_language(language_name) if language_name else False
                            expected_dockerfile_path = get_dockerfile_path(
                                service_name=service.name,
                                build_path=config_dict.get("build_path"),
                                is_java=is_java
                            )
                            existing_config.config["dockerfile_path"] = expected_dockerfile_path
                            logger.info(f"Set dockerfile_path for generate_dockerfile: {expected_dockerfile_path}")
                            # Commit the dockerfile_path update immediately (ensures pipeline sync has correct path)
                            await self.session.commit()
                            await self.session.refresh(existing_config)
                        elif dockerfile_sync and dockerfile_sync.get("dockerfile_path"):
                            # Fallback: use dockerfile_path from sync result
                            if existing_config.config is None:
                                existing_config.config = {}
                            existing_config.config["dockerfile_path"] = dockerfile_sync.get("dockerfile_path")
                            logger.info(f"Updated dockerfile_path from sync result: {dockerfile_sync.get('dockerfile_path')}")
                            # Commit the dockerfile_path update immediately
                            await self.session.commit()
                            await self.session.refresh(existing_config)

                        if dockerfile_sync and dockerfile_sync.get("branches"):
                            try:
                                service_repo = existing_config.config.get("repository", "") if existing_config.config else ""
                                linked_count = 0
                                updated_count = 0
                                logger.info(f"Processing {len(dockerfile_sync.get('branches', []))} branch results")
                                for branch_result in dockerfile_sync.get("branches", []):
                                    # Only create/update workflow if PR was actually created
                                    if branch_result.get("pr_number"):
                                        # Generate unique code for dockerfile workflow using hash
                                        branch_name = branch_result.get("branch")
                                        unique_key = f"{existing_config.code}_{branch_name}_{service_repo}"
                                        dockerfile_workflow_code = f"SCDF_{hashlib.md5(unique_key.encode()).hexdigest()[:8]}"
                                        dockerfile_workflow_name = f"Dockerfile: {existing_config.services_mst_code} - {branch_name}"
                                        pr_number = branch_result.get("pr_number")

                                        # Smart PR replacement: Mark old workflow as closed if replaced
                                        old_workflow_id = branch_result.get("old_workflow_id")
                                        if old_workflow_id:
                                            await self.gitops_workflow_repository.update_pr_status(
                                                workflow_id=old_workflow_id,
                                                new_status=PRStatusEnum.PR_CLOSED
                                            )
                                            logger.info(f"Marked old workflow {old_workflow_id} as PR_CLOSED (superseded)")

                                        # Check if workflow already exists for this PR (avoid duplicates)
                                        existing_workflow = await self.gitops_workflow_repository.get_by_transaction_and_pr(
                                            transaction_code=dockerfile_workflow_code,
                                            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                                            pr_number=pr_number,
                                            tenant_code=tenant_code
                                        )

                                        if existing_workflow:
                                            # Update existing workflow with new commit SHA
                                            existing_workflow.git_commit_sha = branch_result.get("commit_sha")
                                            existing_workflow.git_branch = branch_result.get("feature_branch") or branch_name
                                            self.session.add(existing_workflow)
                                            updated_count += 1
                                        else:
                                            # Create gitops_workflow_detail record
                                            workflow_data = make_gitops_workflow_detail(
                                                git_repository=service_repo,
                                                git_branch=branch_result.get("feature_branch") or branch_name,
                                                git_commit_sha=branch_result.get("commit_sha"),
                                                pr_number=pr_number,
                                                pr_url=branch_result.get("pr_url"),
                                                tenant_mst_code=tenant_code,
                                                user_mst_code=user_code,
                                                workflow_name=dockerfile_workflow_name,
                                                transaction_code=dockerfile_workflow_code,
                                                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
                                            )
                                            workflow = await self.gitops_workflow_repository.create(**workflow_data)

                                            # Link via junction table (upsert - updates if exists)
                                            await self.dockerfile_workflow_repository.link_workflow(
                                                service_config_id=existing_config.id,
                                                gitops_workflow_id=workflow.id,
                                                branch=branch_name,
                                                repository=service_repo,
                                                code=dockerfile_workflow_code,
                                                name=dockerfile_workflow_name
                                            )
                                            linked_count += 1

                                await self.session.commit()
                                await self.session.refresh(existing_config)
                                logger.info(f"Dockerfile workflows: {linked_count} created, {updated_count} updated")
                            except Exception as e:
                                logger.error(f"Failed to save Dockerfile workflows: {e}")
                                # Non-fatal - config exists, just tracking failed

                        # Pipeline sync for each branch in config.branches
                        # Pass token from Terragrunt sync to avoid duplicate GitHub App token requests
                        if existing_config.config and existing_config.config.get("branches"):
                            try:
                                terragrunt_github_token = github_sync.get("github_token") if github_sync else None
                                pipeline_sync_result = await self._sync_pipelines_for_branches(
                                    service_config=existing_config,
                                    service=service,
                                    tenant_code=tenant_code,
                                    user_code=user_code,
                                    github_token=terragrunt_github_token
                                )
                                logger.info(f"Pipeline sync result: {pipeline_sync_result.get('successful', 0)} successful, {pipeline_sync_result.get('errors', 0)} errors")
                            except Exception as e:
                                logger.error(f"Pipeline sync failed: {e}")
                                # Non-fatal - service config already updated
                else:
                    logger.warning(f"Service not found for Terragrunt sync: {existing_config.services_mst_code}")
            except Exception as e:
                logger.error(f"Terragrunt sync failed: {e}")
                # Don't fail the save operation if Terragrunt sync fails
        else:
            logger.info(f"Skipping Terragrunt sync for {code} (sync_to_github=False)")

        # Refresh config to ensure all attributes are loaded (prevents MissingGreenlet on expired objects)
        await self.session.refresh(existing_config)

        # Fire-and-forget: Index updated config to Qdrant for semantic search
        # Get service name for indexing (may have been loaded earlier)
        service_for_index = await self.services_repository.get_by_code(existing_config.services_mst_code)
        asyncio.create_task(self._index_config_to_vector_db(existing_config, service_for_index.name if service_for_index else None))

        return await self._build_service_config_response(existing_config)

    async def _index_config_to_vector_db(
        self,
        config,
        service_name: Optional[str] = None,
    ) -> None:
        """
        Index a service config to Qdrant for semantic search.
        Fire-and-forget - errors are logged but don't fail the request.

        Args:
            config: ServiceConfigModel instance
            service_name: Optional service name (if not provided, uses service_code)
        """
        try:
            vectorizer = ConfigVectorizer()

            # Ensure collection exists
            await vectorizer.ensure_collection_exists()

            # Get environment value
            env_value = config.environment.value if hasattr(config.environment, 'value') else str(config.environment)

            # Index the config
            success = await vectorizer.index_config(
                tenant_code=config.tenant_mst_code,
                service_code=config.services_mst_code,
                service_name=service_name or config.services_mst_code,
                environment=env_value,
                geo_loc=config.geo_loc_mst_code,
                config=config.config or {},
                sidecar_config=config.sidecar_config,
            )

            if success:
                logger.info(f"Indexed config {config.code} to Qdrant")
            else:
                logger.warning(f"Failed to index config {config.code} to Qdrant")
        except Exception as e:
            logger.error(f"Error indexing config {config.code} to Qdrant: {e}")

    async def get_available_sidecars(
        self,
        app_code: str,
        rg_code: str,
        environment: str
    ) -> List[SidecarConfigResponse]:
        """
        Get available sidecar configurations for dropdown selection.

        Args:
            app_code: Application code
            rg_code: Resource group code
            environment: Environment (dev/staging/prod)

        Returns:
            List of available sidecar configurations
        """
        logger.info(f"Getting available sidecars for app={app_code}, rg={rg_code}, env={environment}")

        sidecars = await self.sidecar_config_repository.get_available_sidecars(
            app_code,
            rg_code,
            environment
        )

        # Build response list with extracted CPU/RAM and advanced_options from config JSONB
        response_list = []
        for sidecar in sidecars:
            response_list.append(SidecarConfigResponse(
                code=sidecar.code,
                name=sidecar.name,
                description=sidecar.description,
                default_cpu=sidecar.config.get('cpu', ''),
                default_ram=sidecar.config.get('ram', ''),
                environment=sidecar.environment.value,
                default_advanced_options=sidecar.config.get('advanced_options')
            ))

        logger.info(f"Found {len(response_list)} available sidecars")
        return response_list

    async def _validate_sidecar_codes(
        self,
        sidecar_overrides: List[SidecarOverrideSchema]
    ) -> bool:
        """
        Validate that all sidecar_config_codes exist in the database.

        Args:
            sidecar_overrides: List of sidecar overrides to validate

        Returns:
            True if all codes are valid

        Raises:
            HTTPException: If any sidecar_config_code is invalid
        """
        for sidecar in sidecar_overrides:
            exists = await self.sidecar_config_repository.exists_by_code(sidecar.sidecar_config_code)
            if not exists:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid sidecar_config_code: {sidecar.sidecar_config_code}"
                )
        return True

    async def _validate_listener_priority(
        self,
        tenant_code: str,
        listener_priority: Optional[str],
        environment: str,
        application_code: str,
        geo_loc_mst_code: str,
        exclude_service_code: Optional[str] = None,
        service_type: Optional[str] = None
    ) -> None:
        """
        Validate that listener_rule_priority is unique within the scope of
        tenant + environment + application + region + ALB type.

        Rules:
        - Prod: Check only against other prod configs (same tenant + application + region)
        - Dev/Stage: Share same pool within the same region - can't have same priority
          in both dev AND stage for the same region (same tenant + application)
        - OPS_TOOLS: Uses separate ALB (ops-tools/common-infra), so only conflicts
          with other OPS_TOOLS services
        - API/BACKGROUND_SERVICE: Share the same ALB (services/common-infra)

        Args:
            tenant_code: Tenant code (from JWT)
            listener_priority: The listener rule priority to validate
            environment: Environment being validated (dev/stage/prod)
            application_code: Application code for scope filtering
            geo_loc_mst_code: Region code for scope filtering
            exclude_service_code: Service code to exclude (for update validation)
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Raises:
            HTTPException: If priority validation fails
        """
        if not listener_priority or not str(listener_priority).strip():
            return

        # Validate priority range (1-50000)
        try:
            ListenerPriorityValidator.validate_priority_range(listener_priority)
        except ListenerPriorityValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.errors[0] if len(e.errors) == 1 else e.errors
            )

        # Determine which environments to check against
        # Prod checks only prod, dev/staging/qa share the same pool
        if environment == "prod":
            environments_to_check = ["prod"]
        else:
            environments_to_check = ["dev", "stage", "qa"]

        # Get all configs in the tenant for uniqueness check
        existing_configs = await self.service_config_repository.get_configs_by_tenant(
            tenant_code=tenant_code,
            exclude_service_code=exclude_service_code
        )

        # Determine which service types share the same ALB
        # OPS_TOOLS uses ops-tools/common-infra ALB (separate from regular services)
        # API and BACKGROUND_SERVICE share services/common-infra ALB
        if service_type == "OPS_TOOLS":
            conflicting_types = {"OPS_TOOLS"}
            alb_name = "ops-tools ALB"
        else:
            conflicting_types = {"API", "BACKGROUND_SERVICE"}
            alb_name = "services ALB"

        # Check uniqueness within environment + application + region + ALB type scope
        priority_str = str(listener_priority).strip()
        for config in existing_configs:
            if config.config and config.config.get("listener_rule_priority"):
                existing_priority = str(config.config.get("listener_rule_priority")).strip()
                if existing_priority == priority_str:
                    # Get config's environment
                    config_env = config.environment.value if hasattr(config.environment, 'value') else str(config.environment)

                    # Skip if not in environments to check
                    if config_env not in environments_to_check:
                        continue

                    # Skip if not in same region
                    if config.geo_loc_mst_code != geo_loc_mst_code:
                        continue

                    # Get service and check application + service_type
                    service = await self.services_repository.get_by_code(config.services_mst_code)
                    if not service or service.applications_mst_code != application_code:
                        continue  # Different application, skip

                    # Check if service types share the same ALB
                    # OPS_TOOLS only conflicts with OPS_TOOLS
                    # API/BACKGROUND_SERVICE conflict with each other
                    config_service_type = service.service_type.value if hasattr(service.service_type, 'value') else str(service.service_type)
                    if service_type and config_service_type not in conflicting_types:
                        continue  # Different ALB, no conflict

                    # Found conflict - same priority in same env pool, same application, same region, same ALB
                    service_display_name = service.name if service else config.services_mst_code
                    location = config.geo_loc_mst_code or ""
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Priority {priority_str} is already in use by '{service_display_name}' ({config_env} - {location}) on {alb_name}"
                    )

    async def check_listener_priority_availability(
        self,
        tenant_code: str,
        listener_priority: str,
        environment: str,
        application_code: str,
        geo_loc_mst_code: str,
        exclude_service_code: Optional[str] = None,
        service_type: Optional[str] = None
    ) -> dict:
        """
        Check if a listener_rule_priority is available for use.
        Used by frontend for inline validation.
        Priority is scoped by tenant + environment + application + region + ALB type.

        Rules:
        - Prod: Check only against other prod configs (same tenant + application + region)
        - Dev/Stage: Share same pool within same region - can't have same priority in both
        - OPS_TOOLS: Uses separate ALB, only conflicts with other OPS_TOOLS
        - API/BACKGROUND_SERVICE: Share the same ALB

        Args:
            tenant_code: Tenant code (from JWT)
            listener_priority: The listener rule priority to check
            environment: Environment being validated (dev/stage/prod)
            application_code: Application code for scope filtering
            geo_loc_mst_code: Region code for scope filtering
            exclude_service_code: Service code to exclude (for update validation)
            service_type: Service type (API, BACKGROUND_SERVICE, OPS_TOOLS)

        Returns:
            dict with 'valid', 'priority', 'message', and optional 'used_by' fields
        """
        priority_str = str(listener_priority).strip() if listener_priority else ""

        if not priority_str:
            return {
                "valid": True,
                "priority": "",
                "message": "Priority is empty"
            }

        # Validate range
        try:
            priority_int = int(priority_str)
            if priority_int < 1 or priority_int > 50000:
                return {
                    "valid": False,
                    "priority": priority_str,
                    "message": f"Priority must be between 1 and 50000, got: {priority_int}"
                }
        except ValueError:
            return {
                "valid": False,
                "priority": priority_str,
                "message": f"Priority must be a valid integer, got: '{priority_str}'"
            }

        # Determine which environments to check against
        # Prod checks only prod, dev/staging/qa share the same pool
        if environment == "prod":
            environments_to_check = ["prod"]
        else:
            environments_to_check = ["dev", "stage", "qa"]

        # Determine which service types share the same ALB
        # OPS_TOOLS uses ops-tools/common-infra ALB (separate from regular services)
        # API and BACKGROUND_SERVICE share services/common-infra ALB
        if service_type == "OPS_TOOLS":
            conflicting_types = {"OPS_TOOLS"}
            alb_name = "ops-tools ALB"
        else:
            conflicting_types = {"API", "BACKGROUND_SERVICE"}
            alb_name = "services ALB"

        # Check uniqueness within environment + application + region + ALB type scope
        existing_configs = await self.service_config_repository.get_configs_by_tenant(
            tenant_code=tenant_code,
            exclude_service_code=exclude_service_code
        )

        for config in existing_configs:
            if config.config and config.config.get("listener_rule_priority"):
                existing_priority = str(config.config.get("listener_rule_priority")).strip()
                if existing_priority == priority_str:
                    # Get config's environment
                    config_env = config.environment.value if hasattr(config.environment, 'value') else str(config.environment)

                    # Skip if not in environments to check
                    if config_env not in environments_to_check:
                        continue

                    # Skip if not in same region
                    if config.geo_loc_mst_code != geo_loc_mst_code:
                        continue

                    # Get service and check application + service_type
                    service = await self.services_repository.get_by_code(config.services_mst_code)
                    if not service or service.applications_mst_code != application_code:
                        continue  # Different application, skip

                    # Check if service types share the same ALB
                    config_service_type = service.service_type.value if hasattr(service.service_type, 'value') else str(service.service_type)
                    if service_type and config_service_type not in conflicting_types:
                        continue  # Different ALB, no conflict

                    # Found conflict - same priority in same env pool, same application, same region, same ALB
                    service_display_name = service.name if service else config.services_mst_code
                    location = config.geo_loc_mst_code or ""
                    return {
                        "valid": False,
                        "priority": priority_str,
                        "message": f"Priority {priority_str} is already in use by '{service_display_name}' ({config_env} - {location}) on {alb_name}",
                        "used_by": service_display_name
                    }

        return {
            "valid": True,
            "priority": priority_str,
            "message": f"Priority {priority_str} is available"
        }

    async def _map_sidecars_to_environment(
        self,
        sidecars: List[SidecarOverrideSchema],
        app_code: str,
        rg_code: str,
        target_env: str
    ) -> List[dict]:
        """
        Map sidecar codes to the correct environment's codes.
        Used when creating/updating configs to ensure sidecars reference
        the correct environment-specific sidecar_config records.

        Args:
            sidecars: List of sidecar overrides from frontend
            app_code: Application code
            rg_code: Resource group code
            target_env: Target environment (dev/staging/prod)

        Returns:
            List of sidecar dicts with correct codes for target environment
        """
        mapped_sidecars = []

        for sidecar in sidecars:
            # Get source sidecar to find its name
            source_sidecar = await self.sidecar_config_repository.get_by(
                code=sidecar.sidecar_config_code,
                is_deleted=False
            )

            if source_sidecar:
                # Find equivalent sidecar in target environment by name
                target_sidecar = await self.sidecar_config_repository.get_by_name_and_env(
                    app_code, rg_code, target_env, source_sidecar.name
                )

                if target_sidecar:
                    # Use target's code, keep user's cpu/ram/enabled/advanced_options values
                    sidecar_data = {
                        "sidecar_config_code": target_sidecar.code,
                        "enabled": sidecar.enabled,
                        "cpu": sidecar.cpu,
                        "ram": sidecar.ram
                    }
                    # Preserve advanced_options if provided
                    if sidecar.advanced_options is not None:
                        sidecar_data["advanced_options"] = [opt.model_dump() for opt in sidecar.advanced_options]
                    # Preserve datadog_logs_enabled if provided
                    if sidecar.datadog_logs_enabled is not None:
                        sidecar_data["datadog_logs_enabled"] = sidecar.datadog_logs_enabled
                    mapped_sidecars.append(sidecar_data)
                else:
                    logger.warning(f"No matching sidecar found for '{source_sidecar.name}' in {target_env}")
            else:
                logger.warning(f"Source sidecar not found: {sidecar.sidecar_config_code}")

        return mapped_sidecars

    async def _get_existing_dockerfile_prs_by_branch(
        self,
        service_config: ServiceConfigModel,
        tenant_code: str,
        github_token: str = None
    ) -> Dict[str, Dict]:
        """
        Look up existing open Dockerfile PRs for each branch.
        Validates PR status on GitHub if token provided (syncs DB if PR was closed externally).

        Returns:
            Dict mapping branch name to existing PR info:
            {
                "main": {
                    "git_branch": "datadog/service-main-123456",
                    "pr_number": 937,
                    "workflow_id": 123
                }
            }
        """
        from app.services.github_mgmt_service import GitHubMgmtService

        existing_prs = {}

        config = service_config.config
        if not config:
            return existing_prs

        branches = config.get("branches", [])
        service_repo = config.get("repository", "")

        if not branches or not service_repo:
            return existing_prs

        for branch_name in branches:
            # Generate the same workflow code used for tracking
            unique_key = f"{service_config.code}_{branch_name}_{service_repo}"
            dockerfile_workflow_code = f"SCDF_{hashlib.md5(unique_key.encode()).hexdigest()[:8]}"

            # Look up existing open PR
            existing_pr = await self.gitops_workflow_repository.get_open_pr_by_transaction(
                transaction_code=dockerfile_workflow_code,
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                tenant_mst_code=tenant_code
            )

            if existing_pr:
                # Validate PR is still open on GitHub (sync DB if closed externally)
                pr_was_closed = False
                if github_token and "/" in service_repo:
                    try:
                        github_service = GitHubMgmtService(token=github_token)
                        pr_status = await github_service.check_and_update_pr_status(
                            pr_number=existing_pr.pr_number,
                            db=self.gitops_workflow_repository.session,
                            repository=service_repo
                        )
                        if pr_status.get("pr_status") != "PR_OPEN":
                            logger.info(f"Dockerfile PR #{existing_pr.pr_number} is {pr_status.get('pr_status')} - marking for replacement")
                            pr_was_closed = True
                    except Exception as e:
                        logger.warning(f"Failed to validate Dockerfile PR #{existing_pr.pr_number}: {e}")

                # Include PR info even if closed - mark it for replacement
                # This ensures a new PR is created when the old one was manually closed
                existing_prs[branch_name] = {
                    "git_branch": existing_pr.git_branch,
                    "pr_number": existing_pr.pr_number,
                    "workflow_id": existing_pr.id,
                    "commit_sha": existing_pr.git_commit_sha,
                    "was_closed": pr_was_closed  # Flag to force new PR creation
                }
                if pr_was_closed:
                    logger.info(f"Dockerfile PR #{existing_pr.pr_number} for branch {branch_name} was closed - will create replacement")
                else:
                    logger.info(f"Found existing open Dockerfile PR #{existing_pr.pr_number} for branch {branch_name}")

        return existing_prs

    async def _get_existing_terragrunt_pr(
        self,
        service_config: ServiceConfigModel,
        tenant_code: str,
        github_token: str = None,
        terragrunt_repo: str = None
    ) -> Optional[Dict]:
        """
        Look up existing open Terragrunt PR for a service config.
        Validates PR status on GitHub if token provided (syncs DB if PR was closed externally).

        Returns:
            Dict with existing PR info if found:
            {
                "git_branch": "ecs/service-name-stage-123456",
                "pr_number": 863,
                "workflow_id": 123
            }
            Or None if no open PR exists.
        """
        from app.services.github_mgmt_service import GitHubMgmtService

        # Look up existing open PR using service_config.code as transaction_code
        existing_pr = await self.gitops_workflow_repository.get_open_pr_by_transaction(
            transaction_code=service_config.code,
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            tenant_mst_code=tenant_code
        )

        if existing_pr:
            # Validate PR is still open on GitHub (sync DB if closed externally)
            pr_was_closed = False
            if not terragrunt_repo:
                from app.utils.tenant_config import get_tenant_config
                tenant_cfg = await get_tenant_config(tenant_code, self.session)
                terragrunt_repo = tenant_cfg.github_infra_repository
            repo = terragrunt_repo
            if github_token and repo and "/" in repo:
                try:
                    github_service = GitHubMgmtService(token=github_token)
                    pr_status = await github_service.check_and_update_pr_status(
                        pr_number=existing_pr.pr_number,
                        db=self.gitops_workflow_repository.session,
                        repository=repo
                    )
                    if pr_status.get("pr_status") != "PR_OPEN":
                        logger.info(f"Terragrunt PR #{existing_pr.pr_number} is {pr_status.get('pr_status')} - marking for replacement")
                        pr_was_closed = True
                except Exception as e:
                    logger.warning(f"Failed to validate Terragrunt PR #{existing_pr.pr_number}: {e}")

            # Include PR info even if closed - mark it for replacement
            # This ensures a new PR is created when the old one was manually closed
            if pr_was_closed:
                logger.info(f"Terragrunt PR #{existing_pr.pr_number} for {service_config.code} was closed - will create replacement")
            else:
                logger.info(f"Found existing open Terragrunt PR #{existing_pr.pr_number} for {service_config.code}")
            return {
                "git_branch": existing_pr.git_branch,
                "pr_number": existing_pr.pr_number,
                "workflow_id": existing_pr.id,
                "commit_sha": existing_pr.git_commit_sha,
                "was_closed": pr_was_closed  # Flag to force new PR creation
            }

        return None

    async def _sync_pipelines_for_branches(
        self,
        service_config: ServiceConfigModel,
        service,  # ServicesMstModel
        tenant_code: str,
        user_code: str = None,
        github_token: str = None
    ) -> Dict[str, Any]:
        """
        Create/sync pipelines for each branch in service_config.config.branches.

        For each branch:
        1. Check if pipeline exists in DB (service + repo + branch + env)
        2. If no → create pipeline and sync YAML to GitHub
        3. If yes → check if YAML exists in GitHub
           - If no → sync YAML to GitHub
           - If yes → check for workflow_dispatch, add if missing
           - Compare content, update if different

        Args:
            service_config: The service configuration model
            service: The service model (ServicesMstModel)
            tenant_code: Tenant code for tracking
            user_code: User code for tracking
            github_token: GitHub token from Terragrunt sync (optional, reuses token to avoid duplicate requests)

        Returns:
            Dict with total_branches count and results list per branch
        """
        from app.services.pipeline_mgmt_service import PipelineMgmtService

        results = []
        config = service_config.config or {}
        branches = config.get("branches", [])
        repo_url = config.get("repository", "")

        if not branches or not repo_url:
            logger.info(f"Skipping pipeline sync: no branches or repository configured for {service_config.code}")
            return {
                "total_branches": 0,
                "results": [],
                "message": "No branches or repository configured"
            }

        logger.info(f"Starting pipeline sync for {len(branches)} branches in {service_config.code}")

        pipeline_service = PipelineMgmtService(self.session)
        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)

        for branch in branches:
            try:
                logger.info(f"Processing branch '{branch}' for service {service_config.services_mst_code}")

                # Check if pipeline already exists in DB
                existing_pipeline = await pipeline_service.pipeline_repo.check_pipeline_exists(
                    service_code=service_config.services_mst_code,
                    repo_url=repo_url,
                    branch=branch,
                    environment=environment
                )

                if not existing_pipeline:
                    # Pipeline doesn't exist - create new one
                    logger.info(f"Creating new pipeline for branch '{branch}'")
                    result = await pipeline_service.sync_pipeline_for_service_config(
                        service_code=service_config.services_mst_code,
                        repo_url=repo_url,
                        branch=branch,
                        environment=environment,
                        geo_loc_mst_code=service_config.geo_loc_mst_code,
                        service=service,
                        service_config=service_config,
                        tenant_code=tenant_code,
                        user_code=user_code,
                        github_token=github_token
                    )
                else:
                    # Pipeline exists - check and update workflow YAML
                    logger.info(f"Pipeline exists for branch '{branch}', checking/updating YAML")
                    result = await pipeline_service._check_and_update_workflow_yaml(
                        pipeline=existing_pipeline,
                        service=service,
                        service_config=service_config,
                        tenant_code=tenant_code,
                        user_code=user_code,
                        github_token=github_token
                    )

                results.append(result)
                logger.info(f"Pipeline sync result for branch '{branch}': {result.get('status', 'unknown')} - {result.get('action', 'unknown')}")

            except Exception as e:
                logger.error(f"Pipeline sync failed for branch '{branch}': {str(e)}", exc_info=True)
                results.append({
                    "branch": branch,
                    "status": "error",
                    "error": str(e)
                })

        # Count successful operations
        success_count = sum(1 for r in results if r.get("status") == "success")
        error_count = sum(1 for r in results if r.get("status") == "error")

        logger.info(f"Pipeline sync complete for {service_config.code}: {success_count} successful, {error_count} errors")

        return {
            "total_branches": len(branches),
            "successful": success_count,
            "errors": error_count,
            "results": results
        }

    async def _sync_eks_pipeline(
        self,
        service_config: ServiceConfigModel,
        service,  # ServicesMstModel
        tenant_code: str,
        user_code: str = None
    ) -> Dict[str, Any]:
        """
        Sync EKS pipelines for a service config.

        For EKS infrastructure, this creates:
        1. configs/{env}/config.yaml
        2. .github/workflows/deploy-{service}-{env}.yml

        Unlike ECS, EKS does NOT sync:
        - Terragrunt HCL files
        - Dockerfile modifications (handled by shared-lib)

        Args:
            service_config: The service configuration model
            service: The service model (ServicesMstModel)
            tenant_code: Tenant code for tracking
            user_code: User code for tracking

        Returns:
            Dict with total_branches count and results list per branch
        """
        from app.services.eks_pipeline_service import EKSPipelineService

        results = []
        config = service_config.config or {}

        # Use selected_branches if provided, otherwise fallback to branches for backward compatibility
        selected_branches = config.get("selected_branches") or config.get("branches", [])

        if not selected_branches:
            logger.info(f"Skipping EKS pipeline sync: no branches configured for {service_config.code}")
            return {
                "total_branches": 0,
                "results": [],
                "message": "No branches configured"
            }

        logger.info(f"Starting EKS pipeline sync for {len(selected_branches)} branches in {service_config.code}")

        environment = service_config.environment.value if hasattr(service_config.environment, 'value') else str(service_config.environment)
        eks_service = EKSPipelineService(self.session)

        for branch in selected_branches:
            try:
                logger.info(f"[EKS] Processing branch '{branch}' for service {service_config.services_mst_code}")

                result = await eks_service.sync_eks_pipeline(
                    service_config=service_config,
                    service=service,
                    branch=branch,
                    environment=environment,
                    tenant_code=tenant_code,
                    user_code=user_code
                )

                results.append(result)
                logger.info(f"[EKS] Branch '{branch}' sync result: {result.get('status')}")

            except Exception as e:
                logger.error(f"[EKS] Pipeline sync failed for branch '{branch}': {str(e)}", exc_info=True)
                results.append({
                    "branch": branch,
                    "status": "error",
                    "error": str(e)
                })

        # Count successful operations
        success_count = sum(1 for r in results if r.get("status") == "success")
        error_count = sum(1 for r in results if r.get("status") == "error")

        logger.info(f"[EKS] Pipeline sync complete for {service_config.code}: {success_count} successful, {error_count} errors")

        return {
            "total_branches": len(selected_branches),
            "successful": success_count,
            "errors": error_count,
            "results": results
        }

    async def _check_all_prs_merged(
        self,
        service_config: ServiceConfigModel
    ) -> bool:
        """
        Check if all relevant PRs for a service config are merged.

        For BOTH ECS and EKS, checks all three:
        1. Terragrunt PR (gitops_workflow_id) - SINGLE PR
        2. Dockerfile PRs (from junction table service_config_dockerfile_workflows) - MULTIPLE PRs (one per branch)
        3. Pipeline PR (infrastructure_mst.gitops_workflow_id) - SINGLE PR

        Args:
            service_config: The service configuration model

        Returns:
            True if ALL relevant PRs are merged, False otherwise
        """
        # Check 1: Terragrunt PR (SINGLE PR)
        if service_config.gitops_workflow_id:
            terragrunt_workflow = await self.gitops_workflow_repository.get_by_id(
                service_config.gitops_workflow_id
            )
            if not terragrunt_workflow or terragrunt_workflow.pr_status != PRStatusEnum.PR_MERGED:
                logger.info(f"Terragrunt PR not merged for {service_config.code} (status: {terragrunt_workflow.pr_status if terragrunt_workflow else 'None'})")
                return False

        # Check 2: Dockerfile PRs (MULTIPLE PRs from junction table)
        # Check all Dockerfile PRs linked to this service config
        dockerfile_workflow_stmt = sql_select(ServiceConfigDockerfileWorkflowModel.gitops_workflow_id).where(
            ServiceConfigDockerfileWorkflowModel.service_config_id == service_config.id
        )
        dockerfile_workflow_ids_result = await self.session.execute(dockerfile_workflow_stmt)
        dockerfile_workflow_ids = [row[0] for row in dockerfile_workflow_ids_result.fetchall()]

        if dockerfile_workflow_ids:
            # Check if ALL Dockerfile PRs are merged
            for workflow_id in dockerfile_workflow_ids:
                dockerfile_workflow = await self.gitops_workflow_repository.get_by_id(workflow_id)
                if not dockerfile_workflow or dockerfile_workflow.pr_status != PRStatusEnum.PR_MERGED:
                    logger.info(f"Dockerfile PR #{workflow_id} not merged for {service_config.code} (status: {dockerfile_workflow.pr_status if dockerfile_workflow else 'None'})")
                    return False
            logger.info(f"All {len(dockerfile_workflow_ids)} Dockerfile PRs merged for {service_config.code}")

        # Check 3: Pipeline PRs (from pipeline_mst table) - applies to BOTH ECS and EKS
        # Get all pipelines for this service config (direct lookup by transaction_code)
        from sqlalchemy import select
        pipeline_stmt = select(PipelineMstModel).where(
            PipelineMstModel.transaction_code == service_config.code,
            PipelineMstModel.table_name == "SERVICE_CONFIG",
            PipelineMstModel.is_deleted == False,
        )
        pipeline_result = await self.session.execute(pipeline_stmt)
        pipelines = pipeline_result.scalars().all()

        if pipelines:
            logger.info(f"Found {len(pipelines)} pipeline(s) for {service_config.code}")
            for pipeline in pipelines:
                if pipeline.gitops_workflow_id:
                    pipeline_workflow = await self.gitops_workflow_repository.get_by_id(
                        pipeline.gitops_workflow_id
                    )
                    if not pipeline_workflow or pipeline_workflow.pr_status != PRStatusEnum.PR_MERGED:
                        logger.info(f"Pipeline PR not merged for {service_config.code} (pipeline: {pipeline.code}, status: {pipeline_workflow.pr_status if pipeline_workflow else 'None'})")
                        return False
            logger.info(f"All {len(pipelines)} Pipeline PRs merged for {service_config.code}")

        # All relevant PRs are merged
        logger.info(f"All PRs merged for {service_config.code}")
        return True

    async def reindex_all_configs_to_vector_db(
        self,
        tenant_code: str,
    ) -> Dict[str, Any]:
        """
        Reindex all service configs for a tenant to Qdrant.
        Used for batch reindexing via admin endpoint.

        Args:
            tenant_code: Tenant code to reindex configs for

        Returns:
            Dict with total_configs, indexed_count, and any errors
        """
        logger.info(f"Starting batch reindex for tenant {tenant_code}")

        try:
            vectorizer = ConfigVectorizer()

            # Ensure collection exists
            await vectorizer.ensure_collection_exists()

            # Get all configs for tenant
            configs = await self.service_config_repository.get_configs_by_tenant(tenant_code)
            logger.info(f"Found {len(configs)} configs to reindex")

            if not configs:
                return {
                    "total_configs": 0,
                    "indexed_count": 0,
                    "message": "No configs found for tenant"
                }

            # Build list of config dicts for batch indexing
            config_dicts = []
            for config in configs:
                # Get service name
                service = await self.services_repository.get_by_code(config.services_mst_code)
                service_name = service.name if service else config.services_mst_code

                # Get environment value
                env_value = config.environment.value if hasattr(config.environment, 'value') else str(config.environment)

                config_dicts.append({
                    "tenant_code": config.tenant_mst_code,
                    "service_code": config.services_mst_code,
                    "service_name": service_name,
                    "environment": env_value,
                    "geo_loc": config.geo_loc_mst_code,
                    "config": config.config or {},
                    "sidecar_config": config.sidecar_config,
                })

            # Batch index
            indexed_count = await vectorizer.index_configs_batch(config_dicts)

            logger.info(f"Batch reindex complete: {indexed_count}/{len(configs)} indexed")

            return {
                "total_configs": len(configs),
                "indexed_count": indexed_count,
                "message": f"Successfully indexed {indexed_count} of {len(configs)} configs"
            }
        except Exception as e:
            logger.error(f"Batch reindex failed: {e}")
            return {
                "total_configs": 0,
                "indexed_count": 0,
                "error": str(e),
                "message": f"Reindex failed: {str(e)}"
            }

    async def get_dockerfile_from_db_or_fetch(
        self,
        service_config_code: Optional[str],
        tenant_code: str,
        repository: str,
        branch: str,
        dockerfile_path: str,
        github_token: str,
        github_base_url: str = "https://api.github.com"
    ) -> Dict[str, Any]:
        """
        Get Dockerfile from database (if cached) or fetch from GitHub repository.

        Args:
            service_config_code: Optional service config code to check for cached Dockerfile
            tenant_code: Tenant code
            repository: Repository in format 'owner/repo'
            branch: Branch name
            dockerfile_path: Path to Dockerfile in repository
            github_token: GitHub authentication token
            github_base_url: GitHub API base URL

        Returns:
            Dict with content, metadata, and GitHub info
        """
        # If service_config_code is provided, check database for cached Dockerfile
        if service_config_code:
            logger.info(f"[DOCKERFILE_DB_CHECK] Checking for cached Dockerfile with service_config_code={service_config_code}, tenant_code={tenant_code}")

            service_config = await self.service_config_repository.get_by_code_and_tenant(
                code=service_config_code,
                tenant_code=tenant_code
            )

            logger.info(f"[DOCKERFILE_DB_CHECK] Service config found: {service_config is not None}")
            if service_config:
                logger.info(f"[DOCKERFILE_DB_CHECK] Dockerfile column exists: {service_config.dockerfile is not None}")

            if service_config and service_config.dockerfile:
                logger.info(f"[DOCKERFILE_DB_CHECK] Using cached Dockerfile from service config: {service_config_code}")
                return {
                    "content": service_config.dockerfile,
                    "repository": repository,
                    "branch": branch,
                    "path": dockerfile_path,
                    "sha": None,  # No SHA for cached content
                    "url": None   # No URL for cached content
                }
            else:
                logger.warning(f"[DOCKERFILE_DB_CHECK] No cached Dockerfile found. Fetching from GitHub...")
        else:
            logger.info(f"[DOCKERFILE_DB_CHECK] No service_config_code provided. Fetching from GitHub...")

        # Fetch from GitHub repository via DockerfileFetchService
        from app.services.dockerfile_fetch_service import DockerfileFetchService
        dockerfile_service = DockerfileFetchService()

        return await dockerfile_service.fetch_from_repository(
            repository=repository,
            branch=branch,
            dockerfile_path=dockerfile_path,
            github_token=github_token,
            github_base_url=github_base_url
        )

    async def generate_dockerfile(
        self,
        tenant_code: str,
        language_ref_code: str,
        service_name: str,
        enable_datadog: bool = False,
        xms: Optional[int] = None,
        xmx: Optional[int] = None,
        advanced_options: Optional[List[Dict]] = None,
        port: Optional[str] = None,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
        build_args: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Generate Dockerfile with placeholders replaced.
        Always generates a fresh Dockerfile (no database caching).

        Args:
            tenant_code: Tenant code
            language_ref_code: Language reference code
            service_name: Service name for Datadog/documentation
            enable_datadog: Enable Datadog APM (Java only)
            xms: Java heap min in MB (Java only)
            xmx: Java heap max in MB (Java only)
            advanced_options: Datadog advanced options (Java only)
            port: Application port (Go only)
            go_config_path: Config file path (Go only)
            use_aws_secrets: Enable AWS Secrets Manager (Go only)
            build_args: Custom Docker build arguments (all languages)

        Returns:
            Dict with generated content and metadata
        """
        # Get language_ref
        language_ref = await self.language_ref_repository.get_by_code(language_ref_code)
        if not language_ref:
            raise ValueError(f"Language reference not found: {language_ref_code}")

        # Generate new Dockerfile via DockerfileFetchService
        from app.services.dockerfile_fetch_service import DockerfileFetchService
        dockerfile_service = DockerfileFetchService()

        logger.info(f"[DOCKERFILE_GENERATE] Generating fresh Dockerfile for language={language_ref.name}, version={language_ref.version}")

        return dockerfile_service.generate_dockerfile(
            language_ref=language_ref,
            service_name=service_name,
            enable_datadog=enable_datadog,
            xms=xms,
            xmx=xmx,
            advanced_options=advanced_options,
            port=port,
            go_config_path=go_config_path,
            use_aws_secrets=use_aws_secrets,
            build_args=build_args
        )
