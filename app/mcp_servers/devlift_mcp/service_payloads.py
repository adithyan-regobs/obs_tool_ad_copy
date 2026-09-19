"""Assemble obs_tool payloads from the chatbot's EKS service result.

The chatbot's `eks_service_form` resolves its `result_template` into:

    {
      "form_id": "eks_service_form",
      "create_service": {application_code, resource_group_code, service_name, service_type},
      "service_config": {
          infrastructuretype_ref_code, infra_vendor_enum, environment,
          geo_loc_mst_code, language_ref_code,
          "config": {repository, branches, cpu_requested, ..., hpa: {...}, ...}
      }
    }

The chatbot is the source of truth for VALUES (same contract as the resource
forms' `attribute_parameters`): the `config` block is already in the shape
obs_tool's generators read, so it is passed through untouched. What obs_tool
adds is only what obs_tool alone can know — the EKS cluster row for the
placement, the creation baseline the web writes (ALB selection, namespace,
cluster context), and the identity keys the queue readers expect at the root
of a draft snapshot.

Three builders, one per obs_tool call:
  - build_create_service_payload   → POST /services/create-service
  - build_baseline_config_payload  → POST /service-configs (creation baseline,
                                     mirrors the web's "Create & Add")
  - build_draft_snapshot           → POST /transaction/service-settings/{code}
"""

from __future__ import annotations

from typing import Any, Optional

EKS_INFRA_TYPE = "eks_infrastructuretype_ref"
SETTINGS_CASE_REF = "update_service"


class ServicePayloadError(ValueError):
    """The cached chatbot result is missing something the tool cannot infer."""


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


# ── small derivations copied from the web ────────────────────────────────────

def alb_selection_for(service_type: Optional[str]) -> str:
    """Workers have no load balancer; everything else uses the shared ALB
    (ProjectCanvas.tsx, handleAddResource)."""
    return "no_alb" if (service_type or "").upper() == "BACKGROUND_SERVICE" else "existing_alb"


def eks_namespace_for(service_name: str) -> str:
    """DeploymentTab.tsx `eksNamespaceForService`."""
    name = (service_name or "").strip()
    return name if name.endswith("-service") else f"{name}-service"


def cluster_context_from_locator(locator: Optional[dict]) -> dict:
    """Pick the cluster fields the web copies from the canvas EKS node.

    `infrastructure_mst.locator` mixes snake_case and camelCase depending on
    who wrote it, so read both spellings.
    """
    loc = locator or {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if not _is_blank(loc.get(key)):
                return loc[key]
        return None

    return {
        "cluster_name": pick("cluster_name", "clusterName"),
        "cluster_arn": pick("cluster_arn", "clusterArn"),
        "region": pick("cloudRegion", "region", "cloud_region"),
        "cloud_region_id": pick("cloudRegionId", "cloud_region_id"),
        "subnet_ids": pick("subnetIds", "subnet_ids"),
        "vpc_id": pick("vpcId", "vpc_id"),
    }


# ── cached-result access ─────────────────────────────────────────────────────

def _require(mapping: dict, key: str, where: str) -> Any:
    value = mapping.get(key)
    if _is_blank(value):
        raise ServicePayloadError(f"chatbot result is missing `{where}.{key}`")
    return value


def create_service_block(cached: dict) -> dict:
    return cached.get("create_service") or {}


def service_config_block(cached: dict) -> dict:
    return cached.get("service_config") or {}


def config_values(cached: dict) -> dict:
    return service_config_block(cached).get("config") or {}


def service_name_of(cached: dict) -> str:
    return str(_require(create_service_block(cached), "service_name", "create_service")).strip()


# ── builders ─────────────────────────────────────────────────────────────────

def build_create_service_payload(cached: dict) -> dict:
    """Body for `POST /services/create-service` (CreateServiceRequest)."""
    cs = create_service_block(cached)
    return {
        "application_code": _require(cs, "application_code", "create_service"),
        "resource_group_code": _require(cs, "resource_group_code", "create_service"),
        "service_name": service_name_of(cached),
        "service_type": _require(cs, "service_type", "create_service"),
        "is_active": True,
        "is_public_facing": False,
    }


def build_baseline_config_payload(
    cached: dict,
    *,
    services_mst_code: str,
    infrastructure_mst_code: Optional[str],
    cluster_locator: Optional[dict],
) -> dict:
    """Body for `POST /service-configs` — the creation baseline only.

    Deliberately carries none of the user's settings: the web creates the row
    with cluster placement, ALB selection and namespace, then the Settings
    tab's Save parks the values as a draft. If the values were written live
    here, the draft right after would diff to nothing and no review row would
    be queued.
    """
    sc = service_config_block(cached)
    cs = create_service_block(cached)
    context = cluster_context_from_locator(cluster_locator)
    # The web's "Create & Add" copies cluster_name, cluster_arn, region,
    # cloud_region_id and subnet_ids from the canvas node — but not vpc_id
    # (its create dialog never carries it), so a web-created row has vpc_id
    # null. Match that exactly.
    context.pop("vpc_id", None)
    config = {
        "alb_selection": alb_selection_for(cs.get("service_type")),
        "namespace": eks_namespace_for(service_name_of(cached)),
        **{k: v for k, v in context.items() if not _is_blank(v)},
    }
    payload = {
        "services_mst_code": services_mst_code,
        "infrastructuretype_ref_code": sc.get("infrastructuretype_ref_code") or EKS_INFRA_TYPE,
        "infra_vendor_enum": (sc.get("infra_vendor_enum") or "aws").lower(),
        "environment": _require(sc, "environment", "service_config"),
        "geo_loc_mst_code": _require(sc, "geo_loc_mst_code", "service_config"),
        "config": config,
    }
    if infrastructure_mst_code:
        payload["infrastructure_mst_code"] = infrastructure_mst_code
    # language_ref_code is deliberately NOT written here: the web's creation
    # leaves it empty and it reaches the live row only through the review lane
    # (the draft snapshot carries it; deploy writes it).
    return payload


_ENVIRONMENT_LABELS = {"qa": "QA", "stage": "Stage", "prod": "Prod", "dev": "Dev"}


def language_base_name(language_ref_name: Optional[str], language_ref_code: Optional[str]) -> Optional[str]:
    """The grouped-languages `language_name` for a language_ref row — same
    rule as LanguageRefService.get_language_versions_grouped."""
    code = language_ref_code or ""
    if code.startswith("JAVA-MAVEN"):
        return "Java Maven"
    if code.startswith("JAVA"):
        return "Java Gradle"
    return (language_ref_name or "").split()[0] if language_ref_name else None


def _bool_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    return "true" if text in ("true", "1", "yes") else "false" if text in ("false", "0", "no") else None


def build_edit_prefill(
    config: dict,
    *,
    service_name: str,
    service_type: Optional[str],
    product_name: Optional[str],
    environment: Optional[str],
    geo_name: Optional[str],
    resource_group_name: Optional[str],
    language_name: Optional[str],
    language_version: Optional[str],
) -> dict:
    """Turn a service's effective configuration into `eks_service_form` answers.

    Keyed by form field id, valued the way a user would have answered:
    dropdown LABELS for the placement and lookup fields (the chatbot maps them
    back to codes through its option cache at isReady), "true"/"false" strings
    for boolean dropdowns, lists for array fields, a dict for build args.
    Empty values are omitted so the form asks for them if they are required.
    """
    cfg = config or {}
    out: dict = {
        "product": product_name,
        "environment": _ENVIRONMENT_LABELS.get((environment or "").lower(), (environment or "").capitalize() or None),
        "geo_location": geo_name,
        "resource_group": resource_group_name,
        "service_name": service_name,
        "service_type": service_type,
        "repository": cfg.get("repository"),
        "branches": cfg.get("branches") or cfg.get("selected_branches"),
        "language": language_name,
        "version": language_version,
        "xms": cfg.get("xms"),
        "xmx": cfg.get("xmx"),
        "go_config_path": cfg.get("go_config_path"),
        "go_use_aws_secrets": _bool_text(cfg.get("go_use_aws_secrets")),
        "generate_dockerfile": _bool_text(cfg.get("generate_dockerfile")),
        "dockerfile_path": cfg.get("dockerfile_path"),
        "cpu_requested": cfg.get("cpu_requested"),
        "cpu_limit": cfg.get("cpu_limit"),
        "memory_requested": cfg.get("memory_requested"),
        "memory_limit": cfg.get("memory_limit"),
        "port": cfg.get("port"),
        "health": cfg.get("health"),
        "service_path": cfg.get("service_path"),
        "alb_schema": cfg.get("alb_schema"),
        "compute": cfg.get("compute"),
        "create_ecr": _bool_text(cfg.get("create_ecr")),
        "create_secrets": _bool_text(cfg.get("create_secrets")),
        "create_ssm": _bool_text(cfg.get("create_ssm")),
        "create_argo": _bool_text(cfg.get("create_argo")),
        "auth_mode": cfg.get("auth_mode"),
        "build_path": cfg.get("build_path"),
        "other_paths": cfg.get("other_paths"),
    }

    hpa = cfg.get("hpa") if isinstance(cfg.get("hpa"), dict) else {}
    hpa_enabled = _bool_text(hpa.get("enabled")) if hpa else None
    out["hpa_enabled"] = hpa_enabled or ("false" if cfg.get("replica_count") not in (None, "") else None)
    if out["hpa_enabled"] == "true":
        out["min_replicas"] = hpa.get("min_replicas")
        out["max_replicas"] = hpa.get("max_replicas")
    else:
        out["replica_count"] = cfg.get("replica_count")

    # Multi-select: the form's array field takes the whole list. Values are
    # passed as stored ("s3", "sqs"); the template maps unknown labels through
    # unchanged, so they come back exactly as they went in.
    policies = cfg.get("custom_iam_policies")
    if isinstance(policies, list) and policies:
        out["custom_iam_policies"] = [str(p) for p in policies if p not in (None, "")]
    elif isinstance(policies, str) and policies:
        out["custom_iam_policies"] = [policies]

    build_args = cfg.get("build_args")
    if isinstance(build_args, list) and build_args:
        out["build_args"] = {
            str(a.get("name") or a.get("key")): "" if a.get("value") is None else str(a.get("value"))
            for a in build_args if isinstance(a, dict) and (a.get("name") or a.get("key"))
        }
    elif isinstance(build_args, dict) and build_args:
        out["build_args"] = {str(k): "" if v is None else str(v) for k, v in build_args.items()}

    # Numbers travel as strings, as the form would have stored a typed answer.
    for key in ("cpu_requested", "cpu_limit", "memory_requested", "memory_limit", "port", "replica_count", "min_replicas", "max_replicas"):
        if key in out and out[key] not in (None, ""):
            v = out[key]
            out[key] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)

    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def build_draft_snapshot(
    cached: dict,
    *,
    services_mst_code: str,
    infrastructure_mst_code: Optional[str],
    ingress_group_order: Any = None,
) -> dict:
    """Body `config_snapshot` for `POST /transaction/service-settings/{code}`.

    Same shape the web's EksServiceSettings Save sends: the chatbot's `config`
    block under `config` (the only block the diff and the deploy read — passed
    through as-is, minus keys the user never answered), identity keys at the
    root where the queue readers look.
    """
    sc = service_config_block(cached)
    cs = create_service_block(cached)
    collected = cached.get("collected_data") or {}

    config = {k: v for k, v in config_values(cached).items() if v is not None}

    snapshot: dict = {
        "config": config,
        "services_mst_code": services_mst_code,
        "service_name": service_name_of(cached),
        "service_type": cs.get("service_type"),
        "environment": sc.get("environment"),
        "geo_loc_mst_code": sc.get("geo_loc_mst_code"),
        "infrastructuretype_ref_code": sc.get("infrastructuretype_ref_code") or EKS_INFRA_TYPE,
        "infrastructure_mst_code": infrastructure_mst_code,
        "applications_mst_code": cs.get("application_code"),
    }
    # Denormalised display copies for the deploy pipeline, root not payload —
    # the live config never stores them, so inside `config` they would diff.
    # The template emits the canonical pair (name "Go", bare version "1.24");
    # the collected labels are only a fallback for older cached results.
    if not _is_blank(collected.get("product")):
        snapshot["product_name"] = collected["product"]
    language_name = sc.get("language_name") or collected.get("language")
    language_version = sc.get("language_version") or collected.get("version")
    if not _is_blank(language_name):
        snapshot["language_name"] = language_name
    if not _is_blank(language_version):
        snapshot["language_version"] = language_version
    # language_ref_code goes INSIDE `config`, which is where the web's Settings
    # tab puts it (EksServiceSettings.buildEksConfig) and, more to the point,
    # where compute_changes walks. At the root it depended on the COLUMN_FIELDS
    # lift instead — a second, weaker path — and a language change saved through
    # this tool produced a draft whose diff showed no language at all, while the
    # same change from the dashboard diffed normally. Only the denormalised
    # display copies (name, version) stay at the root: the live config does not
    # store those, so inside `config` they would diff as changes nobody made.
    # The deploy-time reader takes config first and the root second
    # (ServiceSettingsSaverComponent), so this placement satisfies it too.
    if not _is_blank(sc.get("language_ref_code")):
        config["language_ref_code"] = sc["language_ref_code"]
    if ingress_group_order is not None:
        snapshot["ingress_group_order"] = ingress_group_order
    return snapshot


# ============================================================
# Kong gateway routes — the `kong_route_form` result
# ============================================================
# The chatbot's `kong_route_form` fills ONE gateway card, the unit the web's
# Gateway tab works in: one HTTP method, secured (JWT) or public, one tag,
# one or more regex paths, optional priority and extra plugins:
#
#     "gateway_group": {
#         service_mst_code, service_name, applications_mst_code, product_name,
#         environment, geo_loc_mst_code, http_method, secured (bool),
#         route_group_key (may be null), regex_priority (int),
#         plugins (list or null), paths (list of "~/...$")
#     }
#
# obs_tool stores a gateway change as the browser sends it — the group's
# desired end state plus a `delta` (GatewayGroupSave) — so the tool has to do
# what the web's buildPayload does: find the existing group for (tag, method)
# in the current gateway state, keep its paths, add the new ones, and record
# the moves. Everything else (dropping already-live adds, the concurrency
# token, permissions) stays with obs_tool's own route.

GATEWAY_CASE_REF = "add_route"
JWT_PLUGIN = "JWT"


class GatewayConflict(ValueError):
    """The requested card contradicts the existing gateway (e.g. the tag is
    already a No Auth group for that method)."""


def gateway_group_block(cached: dict) -> dict:
    group = cached.get("gateway_group")
    if not isinstance(group, dict) or not group:
        raise ServicePayloadError("The chatbot result carries no gateway_group block.")
    for key in ("service_mst_code", "environment", "geo_loc_mst_code", "http_method"):
        _require(group, key, "gateway_group")
    # A card may ADD paths, REMOVE them, RENAME them, change the group's
    # PLUGINS, or any mix — so no single list is mandatory, but an empty card
    # is. Without this a card that asks for nothing becomes a queue row that
    # deploys an empty change and reports success.
    #
    # Plugins count as a change in their own right: a card that names a group
    # and a plugin and no path is exactly the "add User ID Injection to the
    # orders group" request, and `build_gateway_group_save` has always emitted
    # a save for it (it compares the group's plugins before and after). Only
    # this guard stood in the way, and it refused with a message about paths.
    # A plugin the group already has is still a no-op — that is settled below,
    # against the LIVE group, which is the only place it can be settled.
    if not (
        _nonblank_list(group.get("paths"))
        or _nonblank_list(group.get("remove_paths"))
        or _path_pairs(group.get("edit_paths"))
        or _nonblank_list(group.get("plugins"))
    ):
        raise ServicePayloadError(
            "This gateway card changes nothing — give at least one path to add, "
            "remove or rename, or a plugin for the route group."
        )
    return group


def _nonblank_list(value) -> list[str]:
    """The non-empty strings of a path list, or [] for anything else."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if not _is_blank(v)]


def _path_pairs(value) -> list[tuple[str, str]]:
    """`edit_paths` as (old, new) pairs.

    The chatbot sends a key_value field as `$edit_paths.pairs`, i.e.
    [{"name": old, "value": new}, ...]; a plain dict is accepted too so the
    shape is not a trap for a caller that skipped the substitution.
    """
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = [
            (e.get("name"), e.get("value"))
            for e in value
            if isinstance(e, dict)
        ]
    else:
        return out
    for old, new in items:
        if _is_blank(old) or _is_blank(new):
            continue
        old_s, new_s = str(old).strip(), str(new).strip()
        if old_s != new_s:
            out.append((old_s, new_s))
    return out


def default_route_group_key(service_name: str, secured: bool, existing_keys: list[str]) -> str:
    """The web's rule: the plain service name for the first group of the
    service (whichever card it is), then '<service>-open' for public cards and
    the plain name for secured ones."""
    if service_name not in existing_keys:
        return service_name
    return service_name if secured else f"{service_name}-open"


def gateway_tag_choices(
    service_name: str, existing_tags=(), limit: int = 3, taken_tags=None
) -> list[str]:
    """Tags to offer for a new card, in the order the Gateway tab offers them.

    `existing_tags` are the groups the user may JOIN — same method and same
    auth. `taken_tags` is every name already used on this method whatever its
    auth, and defaults to `existing_tags`. The two differ because a group is
    identified by (tag, method) with auth outside that identity: a tag held by
    the other auth cannot be joined, but a fresh `<base>-tagN` still has to step
    over it or the save is refused for a name we suggested.

    1. THE DEFAULT, first — `preload_for`: the base itself while it is free,
       else the first unused `<base>-tagN`.
    2. THE GROUPS THAT ALREADY EXIST, so adding to one is a pick rather than a
       retyping of a name the user has to remember exactly.
    3. FRESH `<base>-tagN` NAMES to fill the rest.

    The naming rule itself lives in `kong_route_group_naming`; this only orders
    the result into a picker and caps it so a Skip choice still fits the dialog.
    The base key is the PLAIN service name — v2, matching
    `default_route_group_key` above and `preloadTag`/`suggestTags` in the
    Gateway tab. The module's own `suggest_tags` / `preload_tag` count from the
    `-service` form instead; that is v1 and would offer `goms-service-tag1`
    where the tab offers `goms-tag1` for the same service.
    """
    from app.domain.policies.kong_route_group_naming import preload_for, tag_names

    base = (service_name or "").strip()
    if not base:
        return []
    known = [str(t).strip() for t in (existing_tags or []) if t and str(t).strip()]
    pool = known if taken_tags is None else [
        str(t).strip() for t in taken_tags if t and str(t).strip()
    ]
    taken = {t.lower() for t in pool}

    first = preload_for(base, pool)
    out = [first] if first else []
    seen = {o.lower() for o in out}

    for tag in known:
        if len(out) >= limit:
            return out[:limit]
        if tag.lower() not in seen:
            out.append(tag)
            seen.add(tag.lower())

    if len(out) < limit:
        out += tag_names(base, taken | seen, limit - len(out))
    return out[:limit]


def _find_gateway_group(state: dict, route_group_key: str, http_method: str) -> Optional[dict]:
    for g in state.get("groups") or []:
        if (g.get("route_group_key") or "") == route_group_key and (g.get("http_method") or "").upper() == http_method:
            return g
    return None


def build_gateway_group_save(group: dict, state: dict) -> tuple[Optional[dict], dict]:
    """One `GatewayGroupSave` for obs_tool's kong-gateway transaction route,
    plus a summary of what it does. `(None, summary)` when every requested path
    is already on the group and nothing else moves."""
    method = str(group.get("http_method") or "").upper()
    secured = bool(group.get("secured"))
    service_name = group.get("service_name") or state.get("service_name") or ""
    existing_keys = [g.get("route_group_key") or "" for g in state.get("groups") or []]
    tag = group.get("route_group_key") or default_route_group_key(service_name, secured, existing_keys)

    existing = _find_gateway_group(state, tag, method)
    existing_plugins = list((existing or {}).get("plugins") or [])
    if existing is not None:
        existing_secured = JWT_PLUGIN in existing_plugins
        if existing_secured != secured:
            kind = "an Auth (JWT)" if existing_secured else "a No Auth (public)"
            raise GatewayConflict(
                f"Tag '{tag}' is already {kind} group for {method} on {service_name}. "
                f"A tag is one group per method; pick another tag or the matching auth."
            )

    # Plugins: the card's extra plugins on top of what the group already has;
    # JWT follows the card's auth. Priority: the card's value, or the group's
    # own when the user did not give one (0 is the form's default).
    extra = [p for p in (group.get("plugins") or []) if p and p != JWT_PLUGIN]
    plugins_after = [p for p in existing_plugins if p != JWT_PLUGIN]
    for p in extra:
        if p not in plugins_after:
            plugins_after.append(p)
    if secured:
        plugins_after.insert(0, JWT_PLUGIN)
    priority_before = int((existing or {}).get("regex_priority") or 0)
    requested_priority = group.get("regex_priority")
    priority_after = int(requested_priority) if requested_priority else priority_before

    existing_paths = [
        {"code": p.get("code"), "route_path": p.get("route_path")}
        for p in (existing or {}).get("paths") or []
        if p.get("route_path")
    ]
    present = {p["route_path"] for p in existing_paths}
    by_path = {p["route_path"]: p for p in existing_paths}

    # RENAME first: an edit keeps the row (same code, new path), so it must
    # settle before adds and removes decide what is already present. `old_path`
    # on the delta is the DEPLOYED path — what the generator has to find in the
    # file to swap — so it is the path as it stands here, never a pending value.
    edited: list[dict] = []
    missing_edits: list[str] = []
    for old_path, new_path in _path_pairs(group.get("edit_paths")):
        row = by_path.get(old_path)
        if row is None:
            missing_edits.append(old_path)
            continue
        row["route_path"] = new_path          # same code — the row keeps its identity
        present.discard(old_path)
        present.add(new_path)
        by_path.pop(old_path, None)
        by_path[new_path] = row
        edited.append({"code": row.get("code"), "old_path": old_path, "route_path": new_path})

    # REMOVE: drop it from the desired list — _reconcile_paths removes whatever
    # is absent — and record the action so the approver sees a deletion rather
    # than a path that quietly vanished.
    removed: list[dict] = []
    missing_removes: list[str] = []
    for path in _nonblank_list(group.get("remove_paths")):
        row = by_path.get(path)
        if row is None:
            missing_removes.append(path)
            continue
        existing_paths = [e for e in existing_paths if e is not row]
        present.discard(path)
        by_path.pop(path, None)
        removed.append({"code": row.get("code"), "route_path": path})

    if missing_edits or missing_removes:
        bits = []
        if missing_edits:
            bits.append("rename " + ", ".join(missing_edits))
        if missing_removes:
            bits.append("remove " + ", ".join(missing_removes))
        raise GatewayConflict(
            f"Cannot {' and '.join(bits)} on '{tag} · {method}' — "
            f"{'that path is' if len(missing_edits) + len(missing_removes) == 1 else 'those paths are'} "
            f"not on this route group. Its current paths are: "
            + (", ".join(sorted(p["route_path"] for p in existing_paths)) or "(none)")
            + "."
        )

    added: list[str] = []
    skipped: list[str] = []
    for raw in group.get("paths") or []:
        path = str(raw or "").strip()
        if not path:
            continue
        if path in present or path in added:
            skipped.append(path)
        else:
            added.append(path)

    summary = {
        "route_group_key": tag,
        "http_method": method,
        "secured": secured,
        "new_group": existing is None,
        "added_paths": added,
        "removed_paths": [r["route_path"] for r in removed],
        "edited_paths": [{"from": e["old_path"], "to": e["route_path"]} for e in edited],
        "already_present": skipped,
        "plugins": {"from": existing_plugins, "to": plugins_after},
        "regex_priority": {"from": priority_before, "to": priority_after},
    }
    same_plugins = sorted(existing_plugins) == sorted(plugins_after)
    if (
        not added
        and not removed
        and not edited
        and same_plugins
        and priority_before == priority_after
        and existing is not None
    ):
        return None, summary

    save = {
        "code": (existing or {}).get("code"),
        "route_group_key": tag,
        "http_method": method,
        "updated_at": (existing or {}).get("updated_at"),
        "plugins": plugins_after,
        "regex_priority": priority_after,
        "paths": existing_paths + [{"code": None, "route_path": p} for p in added],
        "delta": {
            "paths": [
                *(
                    {"action": "edit", "code": e["code"], "route_path": e["route_path"], "old_path": e["old_path"]}
                    for e in edited
                ),
                *(
                    {"action": "delete", "code": r["code"], "route_path": r["route_path"]}
                    for r in removed
                ),
                *({"action": "add", "code": None, "route_path": p} for p in added),
            ],
            "plugins_before": existing_plugins,
            "plugins_after": plugins_after,
            "regex_priority_before": priority_before,
            "regex_priority_after": priority_after,
        },
    }
    return save, summary


def build_gateway_prefill(
    *, service_name: str, product_name: Optional[str], environment: Optional[str],
    geo_name: Optional[str], route_action: Optional[str] = None,
) -> dict:
    """`kong_route_form` answers for the placement + service questions, the way
    the user would have answered them (option LABELS). The service option's
    label is 'name - product', as the chatbot's service_list adapter builds it."""
    out = {
        "product": product_name,
        "environment": _ENVIRONMENT_LABELS.get((environment or "").lower(), (environment or "").capitalize() or None),
        "geo_location": geo_name,
        "service_name": f"{service_name} - {product_name}" if product_name else service_name,
        # Answered up front only when the caller could tell from the user's own
        # words which it is ("add a path" / "remove /x" / "rename /a to /b"). An
        # answered field is not asked, so the opening dialog drops to method +
        # auth. Left out when the wording is ambiguous ("update a path"), and the
        # form asks — guessing here picks the wrong path field and the user has
        # to notice and undo it.
        "route_action": route_action,
    }
    return {k: v for k, v in out.items() if not _is_blank(v)}


def summarize_gateway_groups(groups: list) -> list[dict]:
    """A stored gateway change (`changes.groups`, flat entries) → what the web's
    GatewayChangeList shows per group: added / removed / changed paths, the
    plugin move and the priority move (only when they move)."""
    out: list[dict] = []
    for e in groups or []:
        if not isinstance(e, dict):
            continue
        added, removed, changed = [], [], []
        for a in e.get("paths") or []:
            act = (a.get("action") or "").lower()
            if act == "add":
                added.append(a.get("route_path"))
            elif act == "delete":
                removed.append(a.get("route_path"))
            elif act == "edit":
                changed.append({"from": a.get("old_path"), "to": a.get("route_path")})
        pb, pa = list(e.get("plugins_before") or []), list(e.get("plugins_after") or [])
        rb, ra = e.get("regex_priority_before"), e.get("regex_priority_after")
        item = {
            "route_group_key": e.get("route_group_key"),
            "http_method": (e.get("http_method") or "").upper(),
            "secured": JWT_PLUGIN in (pa or pb),
            "added": added,
            "removed": removed,
            "changed": changed,
        }
        if sorted(pb) != sorted(pa):
            item["plugins"] = {"from": pb, "to": pa}
        if (rb or 0) != (ra or 0) and ra is not None:
            item["regex_priority"] = {"from": rb or 0, "to": ra}
        out.append(item)
    return out


def count_gateway_changes(groups: list) -> int:
    """The web's countGatewayChanges: path actions + 1 per plugin move + 1 per
    priority move — never len(changes), which is always 1 for a gateway row."""
    total = 0
    for item in summarize_gateway_groups(groups):
        total += len(item["added"]) + len(item["removed"]) + len(item["changed"])
        total += 1 if "plugins" in item else 0
        total += 1 if "regex_priority" in item else 0
    return total


def summarize_gateway_state(state: dict) -> list[dict]:
    """The current gateway (`GET .../gateway/by-config/{sc}`) as cards: one row
    per group with its paths and any pending change on it."""
    cards: list[dict] = []
    for g in state.get("groups") or []:
        plugins = list(g.get("plugins") or [])
        pending = []
        for p in g.get("pending") or []:
            delta = p.get("delta") or {}
            pending.append({
                "queue_code": p.get("queue_code"),
                "status": p.get("status"),
                "mine": bool(p.get("is_mine")),
                "added": [a.get("route_path") for a in (delta.get("paths") or []) if (a.get("action") or "") == "add"],
                "removed": [a.get("route_path") for a in (delta.get("paths") or []) if (a.get("action") or "") == "delete"],
            })
        cards.append({
            "http_method": (g.get("http_method") or "").upper(),
            "secured": JWT_PLUGIN in plugins,
            "route_group_key": g.get("route_group_key"),
            "plugins": [p for p in plugins if p != JWT_PLUGIN],
            "regex_priority": g.get("regex_priority") or 0,
            "paths": [
                {"route_path": p.get("route_path"), "deployed": bool(p.get("code"))}
                for p in g.get("paths") or []
            ],
            "pending": pending,
        })
    order = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4, "OPTIONS": 5}
    cards.sort(key=lambda c: (order.get(c["http_method"], 9), not c["secured"], c["route_group_key"] or ""))
    return cards


#: How many paths of one route group the card tables print before the rest are
#: rolled into a count. A service with a hundred paths otherwise arrives as one
#: cell per group holding all of them, which is the opposite of the table's job
#: — naming the cards so the user can pick one.
GATEWAY_PATH_PREVIEW = 10


def preview_gateway_cards(cards: list[dict], limit: int = GATEWAY_PATH_PREVIEW) -> list[dict]:
    """The cards as the tables should render them: at most `limit` paths per
    group, with `paths_total` and `paths_more` carrying what was left out.

    DISPLAY ONLY. `summarize_gateway_state` keeps every path because the tag
    and priority questions read them — `_priority_action` decides whether the
    incoming paths clash with another group by walking `card['paths']`, and a
    card cut to ten would report "nothing clashes" against the eleventh.
    """
    preview: list[dict] = []
    for card in cards:
        paths = card.get("paths") or []
        preview.append({
            **card,
            "paths": paths[:limit],
            "paths_total": len(paths),
            "paths_more": max(len(paths) - limit, 0),
        })
    return preview


def pending_entry_to_save(entry: dict) -> dict:
    """A stored gateway entry (flat, as `_gateway_entry` writes it on the
    queue row) back into the GatewayGroupSave the route accepts. Needed
    because obs_tool keeps ONE gateway row per user and scope whose groups
    are exactly what the last save sent: a save that carries only the new
    card would drop every card saved before it. The web avoids that by
    resending all changed groups each time; so does this."""
    return {
        "code": entry.get("group_code"),
        "route_group_key": entry.get("route_group_key"),
        "http_method": (entry.get("http_method") or "").upper(),
        "updated_at": entry.get("updated_at"),
        "plugins": list(entry.get("plugins") or []),
        "regex_priority": int(entry.get("regex_priority") or 0),
        "paths": list(entry.get("desired_paths") or []),
        "delta": {
            "paths": list(entry.get("paths") or []),
            "plugins_before": list(entry.get("plugins_before") or []),
            "plugins_after": list(entry.get("plugins_after") or []),
            "regex_priority_before": entry.get("regex_priority_before"),
            "regex_priority_after": entry.get("regex_priority_after"),
        },
    }


def merge_gateway_batch(save: dict, pending_entries: list) -> list[dict]:
    """The full `gateway_groups` list for one save: every card already pending
    for this user, plus the new card — merged into its pending twin when the
    same (tag, method) was saved before, so earlier path additions survive."""
    key = ((save.get("route_group_key") or ""), (save.get("http_method") or "").upper())
    batch: list[dict] = []
    merged = dict(save)
    for entry in pending_entries or []:
        if not isinstance(entry, dict):
            continue
        prior = pending_entry_to_save(entry)
        if (prior["route_group_key"], prior["http_method"]) != key:
            batch.append(prior)
            continue
        # Same card saved earlier in this draft: keep its recorded moves and
        # add the new ones; the "before" values stay the older baseline.
        seen = {(a.get("action"), a.get("route_path")) for a in prior["delta"]["paths"]}
        actions = list(prior["delta"]["paths"]) + [
            a for a in save["delta"]["paths"] if (a.get("action"), a.get("route_path")) not in seen
        ]
        known = {p.get("route_path") for p in prior["paths"]}
        paths = list(prior["paths"]) + [p for p in save["paths"] if p.get("route_path") not in known]
        merged = {
            **save,
            "code": save.get("code") or prior["code"],
            "updated_at": save.get("updated_at") or prior["updated_at"],
            "paths": paths,
            "delta": {
                **save["delta"],
                "paths": actions,
                "plugins_before": prior["delta"]["plugins_before"],
                "regex_priority_before": prior["delta"]["regex_priority_before"],
            },
        }
    batch.append(merged)
    return batch
