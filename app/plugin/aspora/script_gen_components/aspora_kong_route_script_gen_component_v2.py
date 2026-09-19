"""
Aspora Kong Route Script Gen Component — v2 (edit-based / incremental).

Self-contained regenerator for the Gateway tab's DB-driven deploy. It updates the
gateway terragrunt by SURGICALLY adding/removing only the routes that changed —
never rebuilding whole blocks — so hand-written routes in the same file are
preserved. Markerless: no `# >>> devlift` fences; routes and plugins are located
by their keys.

What it touches (all driven by the DB):
  - new / changed routes (creation_status == INITIATED) → added
  - soft-deleted routes                                 → removed
  - already-deployed (ACTIVE) routes                    → left as-is
  - hand-written routes / plugins                       → never touched
The common kong_plugins block is reconciled by plugin name.

Trade-off: preserves hand-edits, but does NOT self-heal drift (a route that
silently vanished from the file for another reason is not re-added).

Everything the v2 flow needs (HCL surgery + content generation) lives in this one
file — the only other Kong script-gen file is the legacy v1 component, used for
the per-route chat/MCP flow. ScriptGenHandler decides which of the two runs.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Sequence

from app.handlers.gitops_handler import GitOpsHandler
from app.utils.existing_content import fetch_existing_content
from app.utils.timing import log_timing
from app.repository.kong_route_configs_repository import KongRouteConfigsRepository

logger = logging.getLogger(__name__)


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(
    workflow_context,
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message

ENTRY_INDENT = "    "  # kong_configs / kong_plugins entries sit at 4 spaces
# Above this many target_keys, render one per line instead of a single flat list.
TARGET_KEYS_INLINE_MAX = 4

# Non-host defaults for a generated service block — only the host varies, and it
# is built from the gateway file's own path (see resolve_service_config). A
# hardcoded host here used to point every newly created service block at the
# stage Mumbai ALB regardless of the target environment.
#
# https/443, not http/80: the common application ALB terminates TLS on 443 in
# every environment (k8s-manifests app/common.yaml listenPorts + certificateId,
# and platform-base's per-service default of [{"HTTPS":443}]), and that is what
# every hand-written block on it uses. The http/80 blocks on stage are all this
# fallback's own earlier output for test services.
# TODO: replace with real per-service service_config supplied from the UI.
SERVICE_CONFIG_FALLBACKS: Dict[str, object] = {
    "protocol": "https",
    "port": 443,
    "path": "/",
}

# Internal zone every application ALB record lives in.
INTERNAL_DOMAIN = "internal.genorim.xyz"

# Role token of the ALB Kong upstreams point at. Kong fronts the shared common
# application ALB, not the backend ALB — pointing generated routes at the backend
# ALB is the wrong-host bug this replaces.
ALB_ROLE = "common-application-alb"

# The gateway path carries the AWS region (ap-south-1); resource NAMES use the city
# (mumbai). Only regions listed here can be named — an unmapped one raises rather
# than guessing a token, since a wrong host is exactly the failure being fixed.
AWS_REGION_CITY: Dict[str, str] = {
    "ap-south-1": "mumbai",
    "eu-west-2": "london",
    "us-east-2": "ohio",
}

# Gateway folder product token -> the prefix the estate's resource NAMES carry.
# The folder drops the org prefix the names keep (environment/core-prod-01 ->
# vance-core-prod-london-01-...), and not every product has one at all
# (environment/falcon-prod-01 -> falcon-prod-london-01-...), so it cannot be
# derived from the path. Verified against Vance-Club/infrastructure-v2; an
# unmapped product raises rather than guessing, as with AWS_REGION_CITY.
PRODUCT_NAME_PREFIX: Dict[str, str] = {
    "core": "vance-core",
    "falcon": "falcon",
}


def derive_service_host(file_path: str) -> str:
    """
    Build the application ALB hostname from the gateway file's own path.

    The file locator writes the gateway to
        environment/{product}-{env}-{index}/{aws_region}/gateway/terragrunt.hcl
    and the estate names every regional resource
        {product}-{env}-{city}-{index}-{role}
    (vance-core-stage-mumbai-01-backend-cluster,
    vance-core-prod-london-01-common-application-alb, …).

    The path holds every token but one: the region moves in front of the index and
    the AWS code becomes its city, while the folder's product token has to be
    expanded to the prefix the NAMES carry, because the folder drops it.

        environment/core-stage-01/ap-south-1/gateway/terragrunt.hcl
            -> vance-core-stage-mumbai-01-common-application-alb.internal.genorim.xyz
        environment/core-prod-01/eu-west-2/gateway/terragrunt.hcl
            -> vance-core-prod-london-01-common-application-alb.internal.genorim.xyz
        environment/falcon-prod-01/eu-west-2/gateway/terragrunt.hcl
            -> falcon-prod-london-01-common-application-alb.internal.genorim.xyz

    Raises on anything it cannot name exactly (unexpected path shape, unmapped
    region, unmapped product) instead of falling back to a default.
    """
    parts = file_path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "environment":
        raise ValueError(
            f"Cannot derive the application ALB host: unexpected gateway path {file_path!r} "
            f"(expected environment/{{product}}-{{env}}-{{index}}/{{region}}/gateway/terragrunt.hcl)."
        )

    folder, aws_region = parts[1], parts[2]
    # rsplit from the right: the index and env are always the last two tokens, so a
    # multi-token product folder stays intact.
    product, env, index = (folder.rsplit("-", 2) + ["", ""])[:3]
    if not (product and env and index):
        raise ValueError(
            f"Cannot derive the application ALB host: gateway folder {folder!r} is not "
            f"{{product}}-{{env}}-{{index}}."
        )

    city = AWS_REGION_CITY.get(aws_region)
    if not city:
        raise ValueError(
            f"Cannot derive the application ALB host: no city name known for AWS region "
            f"{aws_region!r}. Add it to AWS_REGION_CITY."
        )

    name_prefix = PRODUCT_NAME_PREFIX.get(product)
    if not name_prefix:
        raise ValueError(
            f"Cannot derive the application ALB host: no resource-name prefix known for "
            f"product {product!r}. Add it to PRODUCT_NAME_PREFIX."
        )

    return f"{name_prefix}-{env}-{city}-{index}-{ALB_ROLE}.{INTERNAL_DOMAIN}"


def resolve_service_config(file_path: str, override: Optional[dict] = None) -> Dict[str, object]:
    """
    The upstream a NEW service block in this gateway should point at: the derived
    host plus the standard protocol/port/path.

    `override` (a per-service service_config, once the UI supplies one) wins over
    everything; None values in it are ignored so a partial override still inherits.
    """
    cfg: Dict[str, object] = {**SERVICE_CONFIG_FALLBACKS, "host": derive_service_host(file_path)}
    cfg.update({k: v for k, v in (override or {}).items() if v is not None})
    logger.info("kong: derived upstream host %r from %s", cfg["host"], file_path)
    return cfg

# UI/display plugin name -> (kong plugin name, fixed default config). Name-only
# model: the config is a sensible gateway default, identical everywhere.
PLUGIN_CATALOG: Dict[str, tuple[str, dict]] = {
    # Mirrors the deployed `routes_jwt_auth` config exactly (uri_param_names /
    # secret_is_base64 / anonymous are Kong's own defaults, spelled out there), so
    # devlift adopting that entry produces no diff on the first deploy.
    "JWT": (
        "jwt",
        {
            "uri_param_names": ["jwt"],
            "cookie_names": ["jwt"],
            "key_claim_name": "iss",
            "run_on_preflight": True,
            "secret_is_base64": False,
            "claims_to_verify": ["exp"],
            "anonymous": None,
        },
    ),
    # Same story as JWT: one entry, one config, applied uniformly to every route
    # it covers — so a name-only catalog entry represents it faithfully. Config
    # copied from the deployed `user_id_injection` entry.
    "User ID Injection": (
        "user-id-injection",
        {
            "auth_url": "http://example.com/user",
            "requester_id": "kong",
            "requester_id_header_key": "x-requester-id",
            "tenant_id": "tenant",
            "user_id_header_key": "x-user-id",
            "required_scope_prefix": "aspora:*",
        },
    ),
    "CORS": (
        "cors",
        {"origins": ["*"], "methods": ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
         "headers": ["*"], "credentials": False, "max_age": 3600},
    ),
    "Key Auth": (
        "key-auth",
        {"key_names": ["apikey"], "hide_credentials": True, "run_on_preflight": True},
    ),
}

# route_config as the gateway writes it. 39 of its 62 blocks carry exactly these
# four keys; protocols and strip_path are identical in all 59 that set them, and
# preserve_host is true in 48. Written only when a group needs a non-default
# regex_priority — a group that does not is left without the block, like devlift's
# existing ones. Keys are padded so the `=` line up, matching the file.
ROUTE_CONFIG_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("preserve_host", "true"),
    ("protocols", '["http", "https"]'),
    ("strip_path", "false"),
)
_RC_PAD = len("regex_priority")


def _render_route_config(indent: str, regex_priority: int) -> list[str]:
    """The route_config block for a group that needs a priority."""
    inner = indent + "  "
    lines = [f"{indent}route_config = {{"]
    for key, value in ROUTE_CONFIG_DEFAULTS:
        lines.append(f"{inner}{key.ljust(_RC_PAD)} = {value}")
    lines.append(f"{inner}{'regex_priority'.ljust(_RC_PAD)} = {regex_priority}")
    lines.append(f"{indent}}},")
    return lines


# Kong plugin names devlift owns — the reconcile removes a managed entry no longer
# used, while leaving hand-written (non-managed) plugin entries untouched.
MANAGED_PLUGIN_KONG_NAMES = {kong_name for kong_name, _cfg in PLUGIN_CATALOG.values()}

# The kong_plugins entry key devlift writes for a plugin, as it appears in HCL.
# Defaults to the quoted kong name. Override where the gateway ALREADY has an entry
# for that plugin under a different key: devlift then updates that entry instead of
# adding a second one, which would put the same plugin on a route twice — Kong
# rejects that. Bare (unquoted) keys are fine; _find_block matches both forms.
PLUGIN_ENTRY_KEY: Dict[str, str] = {
    "jwt": "routes_jwt_auth",
    "user-id-injection": "user_id_injection",
}


def _plugin_entry_key(kong_name: str) -> str:
    """HCL key for a plugin's kong_plugins entry (quoted name unless overridden)."""
    return PLUGIN_ENTRY_KEY.get(kong_name, f'"{kong_name}"')


# ── content model + generation ────────────────────────────────────────────────

@dataclass
class RouteInput:
    """One route row, reduced to what generation needs."""
    http_method: str
    route_path: str
    plugins: List[str] = field(default_factory=list)
    route_group_key: Optional[str] = None  # human terragrunt group key (falls back to service)


def _kong_plugin(name: str) -> tuple[str, dict]:
    """Map a display plugin name to (kong_name, default_config), with a slug fallback."""
    if name in PLUGIN_CATALOG:
        return PLUGIN_CATALOG[name]
    slug = name.strip().lower().replace(" ", "-").replace("_", "-")
    logger.warning("Unknown plugin %r — emitting slug %r with empty config", name, slug)
    return slug, {}


def _ind(level: int) -> str:
    return "  " * level


def _hcl_string_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _config_json(config: dict) -> str:
    return f"jsonencode({json.dumps(config)})"


def group_key_of(m) -> Optional[str]:
    """The route's terragrunt group key, from its kong_route_groups row."""
    g = getattr(m, "route_group", None)
    return g.route_group_key if g is not None else None


def plugins_of(m) -> List[str]:
    """
    The route's plugin set, from its kong_route_groups row.

    Plugins belong to the Kong route object — (group, method) — not to an
    individual path, so the group row is the only source.
    """
    g = getattr(m, "route_group", None)
    src = g.plugins if g is not None else None
    return list(src) if isinstance(src, list) else list(src or [])


def routes_from_models(models: Sequence) -> List[RouteInput]:
    """Adapt KongRouteConfigModel rows to RouteInput, reading group-level config
    from the linked kong_route_groups row."""
    result: List[RouteInput] = []
    for m in models:
        result.append(RouteInput(
            http_method=m.http_method,
            route_path=m.route_path,
            plugins=plugins_of(m),
            route_group_key=group_key_of(m),
        ))
    return result


def generate_plugins_map(routes: Sequence[RouteInput], base_indent: int = 2) -> Dict[str, str]:
    """
    Build the COMMON kong_plugins entries from ALL of a gateway's routes. One entry
    per plugin NAME (not per service) — the plugin config is name-only/common. Each
    entry's `target_keys` aggregates every route carrying it (key = "<group>-<method>").

    Plugins now come from the route's GROUP row, so every route sharing a
    (group, method) reports the same set by construction — the aggregation below
    can no longer union two conflicting sets into one target key, which was the
    failure mode when plugins lived on the individual path.
    Returns {kong_name: rendered "name" = { ... } block} for markerless reconcile.
    `base_indent` = 2 places the entry directly as a kong_plugins map entry (4 spaces).
    """
    targets: Dict[str, List[str]] = {}
    order: List[str] = []
    for r in routes:
        group = (r.route_group_key or "").strip()
        if not group:
            continue
        route_key = f"{group}-{r.http_method.strip().lower()}"
        for plugin in sorted({p for p in r.plugins if p}):
            lst = targets.get(plugin)
            if lst is None:
                lst = []
                targets[plugin] = lst
                order.append(plugin)
            if route_key not in lst:
                lst.append(route_key)

    i1, i2, i3 = _ind(base_indent), _ind(base_indent + 1), _ind(base_indent + 2)
    out: Dict[str, str] = {}
    for display_name in order:
        kong_name, default_config = _kong_plugin(display_name)
        keys = sorted(targets[display_name])
        # One target per line past a handful. A single flat list is thousands of
        # characters wide on a real gateway — unreviewable in a PR, and every added
        # route rewrites the whole line so the diff hides what actually changed.
        # Matches how the hand-written entries in these files are laid out.
        if len(keys) > TARGET_KEYS_INLINE_MAX:
            rendered = "\n".join([f"{i2}target_keys = ["]
                                 + [f'{i3}"{k}",' for k in keys]
                                 + [f"{i2}]"])
        else:
            rendered = f"{i2}target_keys = {_hcl_string_list(keys)}"
        out[kong_name] = "\n".join([
            f'{i1}{_plugin_entry_key(kong_name)} = {{',
            f'{i2}name        = "{kong_name}"',
            f'{i2}target      = "route"',
            rendered,
            f"{i2}config_json = {_config_json(default_config)}",
            f"{i1}}}",
        ])
    return out


# ── low-level, string-aware HCL scanning (absolute indices into `content`) ─────

@lru_cache(maxsize=16)
def _masked(content: str) -> str:
    """`content` with every comment body blanked to spaces, same length.

    Comments are not decoration to a brace matcher: the gateway file carries all
    three HCL comment styles, and a `{`, `}` or `"` inside one used to steer the
    parse. A commented-out group matched as if it were live (one entry in the
    real file was yielded twice), a stray `}` in a `# TODO` closed kong_configs
    early and spliced a group into the middle of the comment, and a single
    apostrophe-style `"` left the matcher inside a string for the rest of the
    file, which made every edit silently no-op.

    Blanking rather than deleting is the point: the mask indexes identically to
    the original, so callers SEARCH the mask and SLICE the original with the
    same offsets. Cached because one pass over a 120KB file is repeated by every
    lookup within a reconcile.
    """
    out = list(content)
    i, n = 0, len(content)
    in_str = False
    while i < n:
        ch = content[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == "#" or (ch == "/" and i + 1 < n and content[i + 1] == "/"):
            while i < n and content[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and content[i + 1] == "*":
            while i < n and not (content[i] == "*" and i + 1 < n and content[i + 1] == "/"):
                if content[i] != "\n":
                    out[i] = " "
                i += 1
            # Blank the closing */ too, when it is there.
            for j in range(i, min(i + 2, n)):
                out[j] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def _match_brace(content: str, open_i: int) -> int:
    """Index of the '}' matching the '{' at open_i (string- and comment-aware).
    -1 if unbalanced."""
    content = _masked(content)
    depth = 0
    in_str = False
    esc = False
    i = open_i
    while i < len(content):
        c = content[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _find_block(content: str, key: str, start: int = 0, end: Optional[int] = None):
    """
    Find `<key> = { ... }` within content[start:end]. `key` is a bare word
    (`kong_configs`, `routes`) or a quoted map key (`"login-service"`).
    Returns absolute (name_start, open_brace_i, close_brace_i) or None.
    """
    end = len(content) if end is None else end
    if key.startswith('"'):
        pat = re.escape(key) + r"\s*=\s*\{"
    else:
        pat = r"(?<![\w.\-\"])" + re.escape(key) + r"\s*=\s*\{"  # not a suffix of a longer ident
    # Match on the mask so a commented-out block is never found as live.
    m = re.search(pat, _masked(content)[start:end])
    if not m:
        return None
    name_start = start + m.start()
    open_i = start + m.end() - 1
    close_i = _match_brace(content, open_i)
    if close_i == -1 or close_i > end:
        return None
    return name_start, open_i, close_i


def _line_start(content: str, i: int) -> int:
    return content.rfind("\n", 0, i) + 1


def _insert_before_close(content: str, close_i: int, block: str) -> str:
    """
    Insert `block` just before the map's closing brace at close_i. Handles both the
    multi-line format we generate (`}` alone on its line) and an inline map on one
    line without corrupting it — HCL allows a newline as the entry separator.
    """
    line_prefix = content[_line_start(content, close_i):close_i]  # text before `}` on its line
    head = content[:close_i].rstrip()
    if line_prefix.strip() == "":
        return f"{head}\n{block}\n{line_prefix}" + content[close_i:]
    indent = line_prefix[: len(line_prefix) - len(line_prefix.lstrip())]
    return f"{head}\n{indent}  {block.strip()} " + content[close_i:]


# ── route array helpers ────────────────────────────────────────────────────────

def _match_bracket(content: str, open_i: int) -> int:
    """
    Index of the `]` closing the `[` at open_i, ignoring brackets inside strings.

    A plain `content.find("]")` is wrong here: route paths are Kong regexes and
    almost all of them contain a character class — `~/cx-bot/(?<orderId>[^/]+)$`.
    The first `]` in the array therefore belongs to `[^/]`, not to the array, so
    the span stopped a few paths in. That silently truncated every long method
    array: the importer read only the paths before the first capture group, and
    the generator would have spliced new routes into the middle of a regex.
    """
    content = _masked(content)
    i = open_i + 1
    in_str = False
    while i < len(content):
        ch = content[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "]":
            return i
        i += 1
    return -1


def _method_array_span(content: str, method: str, start: int, end: int):
    """`"<METHOD>" = [ ... ]` within [start,end) → (line_start, lbracket, rbracket) abs, or None."""
    m = re.search(rf'"{re.escape(method)}"\s*=\s*\[', _masked(content)[start:end])
    if not m:
        return None
    lb = start + m.end() - 1
    rb = _match_bracket(content, lb)
    if rb == -1 or rb > end:
        return None
    return _line_start(content, start + m.start()), lb, rb


def _paths_in_array(arr_body: str) -> list[str]:
    # Masked: a commented-out path is not deployed, and reading it back would
    # re-add it live on the next write of this array.
    return re.findall(r'"([^"]*)"', _masked(arr_body))


def _render_array(paths: list[str]) -> str:
    return "[" + ", ".join(f'"{p}"' for p in paths) + "]"


def _kc_bounds(content: str):
    kc = _find_block(content, "kong_configs")
    if kc is None:
        return 0, len(content)
    _, kc_open, kc_close = kc
    return kc_open + 1, kc_close


def group_owns_service(content: str, group: str) -> bool:
    """True if `group`'s kong_configs entry carries its own `service { }` block.

    That block is what makes terraform create `kong_service.services[group]`
    (layers/gateway: `for_each = ... if existing_service == null && service != null`),
    so it is the only thing that makes a group a legal existing_service target.
    """
    lo, hi = _kc_bounds(content)
    grp = _find_block(content, f'"{group}"', lo, hi)
    if grp is None:
        return False
    _, g_open, g_close = grp
    return _find_block(content, "service", g_open + 1, g_close) is not None


def find_owner_in_content(content: str, candidates: Sequence[str]) -> Optional[str]:
    """The group that actually owns the service block in the FILE, or None.

    The file is the ground truth for ownership: it is what terraform reads, and
    `kong_service.services` is keyed by the owning group's name. Any candidate
    that only points elsewhere is followed one hop at a time, so passing a
    non-owner group still lands on the real owner.
    """
    lo, hi = _kc_bounds(content)
    seen: set[str] = set()
    queue = [c for c in candidates if c]
    while queue:
        group = queue.pop(0)
        if group in seen:
            continue
        seen.add(group)
        grp = _find_block(content, f'"{group}"', lo, hi)
        if grp is None:
            continue
        _, g_open, g_close = grp
        if _find_block(content, "service", g_open + 1, g_close) is not None:
            return group
        pointer = re.search(
            r'existing_service\s*=\s*"([^"]+)"', content[g_open:g_close]
        )
        if pointer:
            queue.append(pointer.group(1))
    return None


def _route_present(content: str, group: str, method: str, path: str) -> bool:
    """True if `path` is in kong_configs[`group`].routes[`method`]. Used by drift detection."""
    kc = _find_block(content, "kong_configs")
    if kc is None:
        return False
    _, kc_open, kc_close = kc
    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        return False
    _, g_open, g_close = grp
    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is None:
        return False
    _, r_open, r_close = routes
    arr = _method_array_span(content, method.upper(), r_open + 1, r_close)
    if arr is None:
        return False
    _, lb, rb = arr
    return path in _paths_in_array(content[lb + 1:rb])


def _route_present_in_group(content: str, group: str, method: str) -> bool:
    """
    True if kong_configs[`group`].routes[`method`] still holds any path.

    Distinguishes "this route object exists and carries no plugins" from "this
    route object is gone". remove_route cleans up an emptied array, so the second
    case leaves no target key in the file — and adding plugin targets to it would
    write a target for a route that no longer exists.
    """
    kc = _find_block(content, "kong_configs")
    if kc is None:
        return False
    _, kc_open, kc_close = kc
    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        return False
    _, g_open, g_close = grp
    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is None:
        return False
    _, r_open, r_close = routes
    arr = _method_array_span(content, method.upper(), r_open + 1, r_close)
    if arr is None:
        return False
    _, lb, rb = arr
    return bool(_paths_in_array(content[lb + 1:rb]))


# ── route add / remove ─────────────────────────────────────────────────────────

def _render_new_group(owner_group: str, group: str, method: str, path: str, service_config: dict,
                      regex_priority: int = 0) -> str:
    i2 = ENTRY_INDENT           # entry
    i3 = ENTRY_INDENT + "  "    # inside entry
    i4 = ENTRY_INDENT + "    "  # inside service/routes
    lines = [f'{i2}"{group}" = {{']
    if group == owner_group:
        # Merge onto the non-host fallbacks so a partial config still fills in.
        cfg = {**SERVICE_CONFIG_FALLBACKS, **(service_config or {})}
        if not cfg.get("host"):
            # Never write a service block without a host: it is valid HCL that
            # applies clean and then blackholes live traffic.
            raise ValueError(f"Cannot create gateway service block for '{group}': no upstream host.")
        lines += [
            f"{i3}service = {{",
            f'{i4}host     = "{cfg.get("host")}"',
            f'{i4}protocol = "{cfg.get("protocol")}"',
            f'{i4}port     = {cfg.get("port")}',
            f'{i4}path     = "{cfg.get("path")}"',
            f"{i3}}}",
        ]
    else:
        lines.append(f'{i3}existing_service = "{owner_group}"')
    # Only when it is non-default. 0 is Kong's default and the file leaves it out —
    # writing `regex_priority = 0` on every group would be pure noise. The minimal
    # route_config shape is already used by three groups in the gateway.
    if regex_priority:
        lines += _render_route_config(i3, regex_priority)
    lines += [
        f"{i3}routes = {{",
        f'{i4}"{method}" = ["{path}"]',
        f"{i3}}}",
        f"{i2}}}",
    ]
    return "\n".join(lines)


def add_route(content: str, owner_group: str, group: str, method: str, path: str, service_config: dict,
              regex_priority: int = 0) -> str:
    """
    Ensure `path` exists in kong_configs[`group`].routes[`method`]. Creates the group
    entry (service{} block for the owner, else existing_service) and the method array
    as needed. Idempotent (no-op if already present).
    """
    method = method.upper()
    kc = _find_block(content, "kong_configs")
    if kc is None:
        raise ValueError("kong_configs map not found in gateway config")
    _, kc_open, kc_close = kc

    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        block = _render_new_group(owner_group, group, method, path, service_config, regex_priority)
        return _insert_before_close(content, kc_close, block)

    _, g_open, g_close = grp
    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is None:
        i3, i4 = ENTRY_INDENT + "  ", ENTRY_INDENT + "    "
        block = f'{i3}routes = {{\n{i4}"{method}" = ["{path}"]\n{i3}}}'
        return _insert_before_close(content, g_close, block)

    _, r_open, r_close = routes
    arr = _method_array_span(content, method, r_open + 1, r_close)
    if arr is None:
        block = f'{ENTRY_INDENT + "    "}"{method}" = ["{path}"]'
        return _insert_before_close(content, r_close, block)

    _, lb, rb = arr
    paths = _paths_in_array(content[lb + 1:rb])
    if path in paths:
        return content
    paths.append(path)
    return content[:lb] + _render_array(paths) + content[rb + 1:]


def set_group_priority(content: str, group: str, regex_priority: int) -> str:
    """
    Set kong_configs[`group`].route_config.regex_priority, editing in place.

    Kong resolves two routes matching the same path by regex_priority — the higher
    wins — so a group that deliberately overrides another needs this line or it ties
    at the default 0 and the winner is undefined.

    Touches nothing else: the line is replaced where it already exists, added to an
    existing route_config, or given a minimal route_config of its own. A value of 0
    removes the line (and the block, if that leaves it empty), because 0 is the
    default and the file omits it. No-op if the group is absent.
    """
    kc = _find_block(content, "kong_configs")
    if kc is None:
        return content
    _, kc_open, kc_close = kc
    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        return content
    _, g_open, g_close = grp

    i3, i4 = ENTRY_INDENT + "  ", ENTRY_INDENT + "    "
    rc = _find_block(content, "route_config", g_open + 1, g_close)

    if rc is not None:
        _, rc_open, rc_close = rc
        existing = re.search(r"[ \t]*regex_priority\s*=\s*-?\d+[ \t]*\n?", content[rc_open:rc_close])
        if existing:
            a, b = rc_open + existing.start(), rc_open + existing.end()
            if regex_priority:
                return content[:a] + f"{i4}regex_priority = {regex_priority}\n" + content[b:]
            # Dropping the only key leaves an empty route_config — take the block too.
            body = _strip_hcl_comments(content[rc_open + 1:rc_close]).replace(content[a:b], "")
            if not body.strip():
                line_a = _line_start(content, content.rfind("route_config", g_open, rc_open))
                tail = rc_close + 1
                while tail < len(content) and content[tail] in ", \t":
                    tail += 1
                if tail < len(content) and content[tail] == "\n":
                    tail += 1
                return content[:line_a] + content[tail:]
            return content[:a] + content[b:]
        if regex_priority:
            return _insert_before_close(content, rc_close, f"{i4}regex_priority = {regex_priority}")
        return content

    if not regex_priority:
        return content
    # No route_config yet — write the full conventional block, not a bare priority.
    block = "\n".join(_render_route_config(i3, regex_priority))
    routes = _find_block(content, "routes", g_open + 1, g_close)
    anchor = _line_start(content, content.rfind("routes", g_open, routes[1])) if routes else g_close
    if routes is None:
        return _insert_before_close(content, g_close, block)
    return content[:anchor] + block + "\n" + content[anchor:]


def remove_route(content: str, group: str, method: str, path: str) -> str:
    """
    Ensure `path` is absent from kong_configs[`group`].routes[`method`]. Cleans up an
    emptied method array, routes map, and group entry. Idempotent.
    """
    method = method.upper()
    kc = _find_block(content, "kong_configs")
    if kc is None:
        return content
    _, kc_open, kc_close = kc

    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        return content
    g_line, g_open, g_close = grp

    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is None:
        return content
    _, r_open, r_close = routes

    arr = _method_array_span(content, method, r_open + 1, r_close)
    if arr is None:
        return content
    a_line, lb, rb = arr
    paths = [p for p in _paths_in_array(content[lb + 1:rb]) if p != path]

    if paths:
        return content[:lb] + _render_array(paths) + content[rb + 1:]

    # Method array now empty → remove the `"METHOD" = [...]` line, but only when it's
    # a standalone line. If it shares its line with the routes/group braces (an inline
    # hand-edit), leave an empty `[]` instead of deleting a brace (no corruption).
    #
    # Look only OUTSIDE the array: everything between the brackets is path strings,
    # and Kong paths are regexes where `{` is a repetition count — `~/otp/[0-9]{4,6}$`
    # is a legal path the validator accepts. Scanning the whole line counted that as
    # structure and bailed out to `"GET" = []`, which terraform still builds into a
    # route with methods and NO paths — and a Kong route with no paths matches every
    # path on that service, with the group's plugins stripped because it reads as
    # empty. It also made a repeated delete non-idempotent: the second pass saw no
    # brace, removed the line, and took the whole group with it.
    end_of_line = content.find("\n", rb)
    end_of_line = len(content) if end_of_line == -1 else end_of_line + 1
    masked = _masked(content)
    around_array = masked[a_line:lb] + masked[rb + 1:end_of_line]
    if "{" in around_array or "}" in around_array:
        return content[:lb] + "[]" + content[rb + 1:]
    content = content[:a_line] + content[end_of_line:]

    # re-scan: if the routes map is now empty, drop the whole group entry —
    # UNLESS this group carries the service block. That block is what creates
    # kong_service.services[group], which every other group of the service names
    # with existing_service and resolves through a single unguarded index
    # (layers/gateway/main.tf). Deleting it takes the upstream out from under
    # groups that still have live routes and fails the plan on a missing key.
    # An owner emptied of paths keeps its entry and an empty routes map: the
    # service survives, and re-adding a route later finds its own group intact.
    grp = _find_block(content, f'"{group}"', *_kc_bounds(content))
    if grp is None:
        return content
    g_line, g_open, g_close = grp
    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is not None:
        _, r_open, r_close = routes
        if not _paths_in_array(content[r_open + 1:r_close]) and not re.search(r'"[^"]+"\s*=\s*\[', content[r_open + 1:r_close]):
            if group_owns_service(content, group):
                return content
            g_end = content.find("\n", g_close)
            g_end = len(content) if g_end == -1 else g_end + 1
            # Remove from the group's LINE start (incl. indent), not the key position,
            # so the removed line's indent isn't left dangling on the next line.
            content = content[:_line_start(content, g_open)] + content[g_end:]
    return content


def replace_route_path(content: str, owner_group: str, group: str, method: str,
                       old_path: str, new_path: str, service_config: dict) -> str:
    """
    Replace `old_path` with `new_path` IN PLACE in kong_configs[`group`].routes[`method`],
    keeping its array position — so a path edit shows as a clean one-element change
    instead of a remove + append that reshuffles the array. Falls back to add_route
    (append) if `old_path` isn't present.
    """
    method = method.upper()
    kc = _find_block(content, "kong_configs")
    if kc is not None:
        _, kc_open, kc_close = kc
        grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
        if grp is not None:
            _, g_open, g_close = grp
            routes = _find_block(content, "routes", g_open + 1, g_close)
            if routes is not None:
                _, r_open, r_close = routes
                arr = _method_array_span(content, method, r_open + 1, r_close)
                if arr is not None:
                    _, lb, rb = arr
                    paths = _paths_in_array(content[lb + 1:rb])
                    if old_path in paths:
                        paths = [new_path if p == old_path else p for p in paths]
                        return content[:lb] + _render_array(paths) + content[rb + 1:]
    # old_path not in the file — just ensure new_path is present.
    return add_route(content, owner_group, group, method, new_path, service_config)


# ── markers cleanup + plugins reconcile (markerless) ───────────────────────────

_MARKER_LINE_RES = [
    re.compile(r"^[ \t]*# >>> devlift:.*\n", re.MULTILINE),
    re.compile(r"^[ \t]*# <<< devlift:.*\n", re.MULTILINE),
    re.compile(r"^[ \t]*#\s*⚠?\s*generated by devlift.*\n", re.MULTILINE),
]


def strip_devlift_markers(content: str) -> str:
    """
    Remove every devlift fence/warning comment line (current AND legacy formats),
    keeping the real HCL. The incremental approach is markerless — this cleans up
    leftovers from earlier rebuild-mode deploys so the file ends up marker-free.
    """
    for rx in _MARKER_LINE_RES:
        content = rx.sub("", content)
    return content


def _remove_plugin_entry(content: str, kong_name: str) -> str:
    """
    Remove the plugin's kong_plugins entry (if present).

    Looks it up by the entry key devlift writes — see PLUGIN_ENTRY_KEY — so an
    entry adopted under an existing key (e.g. jwt -> routes_jwt_auth) is found and
    replaced rather than duplicated alongside.
    """
    kp = _find_block(content, "kong_plugins")
    if kp is None:
        return content
    _, kp_open, kp_close = kp
    ent = _find_block(content, _plugin_entry_key(kong_name), kp_open + 1, kp_close)
    if ent is None:
        return content
    name_start, _, e_close = ent
    e_line = _line_start(content, name_start)  # start of the entry's line (incl. indent)
    end = content.find("\n", e_close)
    end = len(content) if end == -1 else end + 1
    return content[:e_line] + content[end:]


def _ensure_kong_plugins_map(content: str) -> str:
    """Create an empty `kong_plugins = { }` inside `inputs { }` if it doesn't exist."""
    if _find_block(content, "kong_plugins") is not None:
        return content
    inp = _find_block(content, "inputs")
    if inp is None:
        raise ValueError("no `inputs { }` block to add kong_plugins into")
    _, _, inp_close = inp
    block = f"{ENTRY_INDENT[:-2]}kong_plugins = {{\n{ENTRY_INDENT[:-2]}}}"
    return _insert_before_close(content, inp_close, block)


def _plugin_target_span(content: str, kong_name: str):
    """(lb, rb) of a plugin entry's `target_keys = [ ... ]` brackets, or None."""
    kp = _find_block(content, "kong_plugins")
    if kp is None:
        return None
    _, kp_open, kp_close = kp
    ent = _find_block(content, _plugin_entry_key(kong_name), kp_open + 1, kp_close)
    if ent is None:
        return None
    _, e_open, e_close = ent
    m = re.search(r"target_keys\s*=\s*\[", _masked(content)[e_open:e_close])
    if not m:
        return None
    lb = e_open + m.end() - 1
    rb = content.find("]", lb)
    return (lb, rb) if rb != -1 and rb < e_close else None


def _strip_hcl_comments(text: str) -> str:
    """Blank out `#` comments so commented-out target keys are not treated as live."""
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def _ensure_trailing_comma(head: str) -> str:
    """
    Put a comma after the last list item in `head` if it hasn't got one.

    HCL needs an explicit separator between tuple elements — a newline is not
    enough ("Missing item separator"). The last key of a hand-written target_keys
    list usually has no trailing comma, so appending after it produced a file
    terraform refuses to parse. Comments and blank lines are skipped, and a comma
    goes before any trailing comment on that line.
    """
    lines = head.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        code = re.sub(r"#.*$", "", lines[i]).rstrip()
        if not code:
            continue  # blank or comment-only line
        if not code.endswith((",", "[")):
            lines[i] = code + "," + lines[i][len(code):]
        break
    return "\n".join(lines)


def add_plugin_target(content: str, kong_name: str, target: str, base_indent: int = 2) -> str:
    """
    Ensure `target` appears in this plugin's target_keys. Idempotent.

    Inserts a line rather than re-rendering the array, so existing entries,
    ordering and commented-out targets survive untouched.
    """
    span = _plugin_target_span(content, kong_name)
    if span is None:
        # No entry yet — create one carrying just this target.
        _, cfg = _kong_plugin(next(
            (d for d, (k, _c) in PLUGIN_CATALOG.items() if k == kong_name), kong_name))
        i1, i2 = _ind(base_indent), _ind(base_indent + 1)
        block = "\n".join([
            f'{i1}{_plugin_entry_key(kong_name)} = {{',
            f'{i2}name        = "{kong_name}"',
            f'{i2}target      = "route"',
            f'{i2}target_keys = ["{target}"]',
            f"{i2}config_json = {_config_json(cfg)}",
            f"{i1}}}",
        ])
        content = _ensure_kong_plugins_map(content)
        _, _, kp_close = _find_block(content, "kong_plugins")
        return _insert_before_close(content, kp_close, block)

    lb, rb = span
    body = content[lb + 1:rb]
    if f'"{target}"' in _strip_hcl_comments(body):
        return content  # already targeted

    if "\n" in body:  # multi-line list — add a line before the closing bracket
        lines = body.splitlines()
        indent = next((re.match(r"[ \t]*", l).group(0) for l in lines
                       if l.strip() and not l.strip().startswith("#")), _ind(base_indent + 2))
        # Splice at the START of the `]`'s line, not at the bracket itself: content[:rb]
        # already ends with that line's leading whitespace, so prepending `indent` there
        # stacked the two and produced a double-indented key.
        line_start = _line_start(content, rb)
        if content[line_start:rb].strip() == "":  # `]` alone on its line — usual case
            head = _ensure_trailing_comma(content[:line_start])
            return head + f'{indent}"{target}",\n' + content[line_start:]
        # `]` shares a line with the last item — keep it valid by inserting in place.
        sep = "" if body.rstrip().endswith(",") else ","
        return content[:rb] + f'{sep}\n{indent}"{target}"' + content[rb:]

    items = _paths_in_array(body)
    items.append(target)
    return content[:lb] + _render_array(items) + content[rb + 1:]


def remove_plugin_target(content: str, kong_name: str, target: str) -> str:
    """Remove `target` from this plugin's target_keys, leaving everything else."""
    span = _plugin_target_span(content, kong_name)
    if span is None:
        return content
    lb, rb = span
    body = content[lb + 1:rb]
    if f'"{target}"' not in _strip_hcl_comments(body):
        return content

    if "\n" in body:  # drop just that line
        kept = [l for l in body.splitlines()
                if f'"{target}"' not in _strip_hcl_comments(l)]
        return content[:lb + 1] + "\n".join(kept) + content[rb:]

    items = [i for i in _paths_in_array(body) if i != target]
    return content[:lb] + _render_array(items) + content[rb + 1:]


def reconcile_plugins(content: str, desired: dict, managed_names: set) -> str:
    """
    Markerless reconcile of the common kong_plugins block. `desired` maps a kong
    plugin name -> its rendered block. `managed_names` is every name devlift owns
    (so an entry no longer used gets removed). Hand-written (non-managed) plugin
    entries are left untouched.
    """
    for name in sorted(managed_names | set(desired)):
        content = _remove_plugin_entry(content, name)
    if not desired:
        return content
    content = _ensure_kong_plugins_map(content)
    _, _, kp_close = _find_block(content, "kong_plugins")
    body = "\n".join(desired[name] for name in desired)
    return _insert_before_close(content, kp_close, body)


# ── deployed-state reader ──────────────────────────────────────────────────────
#
# The file is the record of what is deployed. Everything else that claims to know
# it — the queue delta's plugins_before / regex_priority_before — is a snapshot
# taken by the client when the edit STARTED, and goes stale the moment a deploy
# half-succeeds (applied, then failed before the merge landed). Reading it back
# here means a decision about what still needs deploying is made against the file
# rather than against that claim.
#
# Read from the BASE branch, never the feature branch: the feature branch already
# carries the change being evaluated, which would make every group look deployed.


def _iter_plugin_entries(content: str):
    """Yield (entry_key, open_brace, close_brace) for each entry in kong_plugins.

    Walks every entry rather than deriving the key from the plugin name: a
    hand-written entry may carry any key (the real gateway file has
    `repeat_transfer_transformer` holding a `request-transformer`), and a plugin
    applied through one of those still applies to the route.
    """
    kp = _find_block(content, "kong_plugins")
    if kp is None:
        return
    _, kp_open, kp_close = kp
    i = kp_open + 1
    while i < kp_close:
        m = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*\{").search(_masked(content), i, kp_close)
        if not m:
            return
        e_open = m.end() - 1
        e_close = _match_brace(content, e_open)
        if e_close == -1 or e_close > kp_close:
            return
        yield m.group(1), e_open, e_close
        i = e_close + 1


def plugin_target_key(group: str, method: str) -> str:
    """The `target_keys` entry Kong uses for a (group, method) route object."""
    return f"{group}-{method.lower()}"


def read_deployed_group(content: str, group: str, method: str) -> dict:
    """What the gateway file currently says is deployed for one (group, method).

    Returns {"paths": [...], "plugins": [...], "regex_priority": int, "exists": bool}.
    `plugins` uses PLUGIN_CATALOG display names where the kong name is one devlift
    knows, else the raw kong name — an unrecognised plugin still counts as applied,
    so it must not silently vanish from the comparison.
    `exists` is False when the group has no entry in the file at all, which is a
    genuinely different answer from "present but empty".
    """
    method = method.upper()
    empty = {"paths": [], "plugins": [], "regex_priority": 0, "exists": False}

    kc = _find_block(content, "kong_configs")
    if kc is None:
        return empty
    _, kc_open, kc_close = kc
    grp = _find_block(content, f'"{group}"', kc_open + 1, kc_close)
    if grp is None:
        return empty
    _, g_open, g_close = grp

    # ── paths ────────────────────────────────────────────────────────────────
    paths: list[str] = []
    routes = _find_block(content, "routes", g_open + 1, g_close)
    if routes is not None:
        _, r_open, r_close = routes
        arr = _method_array_span(content, method, r_open + 1, r_close)
        if arr is not None:
            _, lb, rb = arr
            paths = _paths_in_array(_strip_hcl_comments(content[lb + 1:rb]))

    # ── regex_priority ───────────────────────────────────────────────────────
    # Lives on route_config, which the group may omit entirely — Kong's own
    # default is 0, and that is what generation writes when nothing is set.
    regex_priority = 0
    rc = _find_block(content, "route_config", g_open + 1, g_close)
    if rc is not None:
        _, rc_open, rc_close = rc
        m = re.search(r"regex_priority\s*=\s*(\d+)", content[rc_open:rc_close])
        if m:
            regex_priority = int(m.group(1))

    # ── plugins ──────────────────────────────────────────────────────────────
    target = plugin_target_key(group, method)
    kong_to_display = {kong: disp for disp, (kong, _cfg) in PLUGIN_CATALOG.items()}
    plugins: list[str] = []
    for _key, e_open, e_close in _iter_plugin_entries(content):
        body = content[e_open:e_close]
        tk = re.search(r"target_keys\s*=\s*\[([^\]]*)\]", body, re.DOTALL)
        if not tk or target not in _paths_in_array(_strip_hcl_comments(tk.group(1))):
            continue
        nm = re.search(r'name\s*=\s*"([^"]+)"', body)
        if not nm:
            continue
        display = kong_to_display.get(nm.group(1), nm.group(1))
        if display not in plugins:
            plugins.append(display)

    return {
        "paths": paths,
        "plugins": plugins,
        "regex_priority": regex_priority,
        "exists": True,
    }


def diff_group_state(deployed: dict, desired: dict) -> dict:
    """Recompute a group's change set as file → desired, ignoring any stored delta.

    `deployed` comes from read_deployed_group; `desired` is the same shape, built
    from the tables. Returns a GatewayDelta-shaped dict whose `is_empty` twin is
    the `changed` flag — False means the file already says what the tables want,
    i.e. the change is live and the queue row is stale rather than pending.

    Edits surface as a delete plus an add rather than an `edit` action: the file
    records paths, not identities, so a renamed path is indistinguishable from one
    removed and another added. The generated result is identical either way — the
    only loss is that the modal reads "-old +new" instead of "old → new".

    A path in the file with no row behind it is LEFT ALONE. Absence from the
    tables is not a request to delete — the gateway file carries hand-written
    routes that devlift never knew about, and treating "not in the DB" as "remove"
    would quietly drop them. Only `desired["removed_paths"]` — rows soft-deleted
    but not yet deployed — produce a delete. Same rule _apply_incremental follows.
    """
    dep_paths = list(deployed.get("paths") or [])
    des_paths = list(desired.get("paths") or [])
    removed_paths = list(desired.get("removed_paths") or [])

    actions: list[dict] = []
    for p in des_paths:
        if p not in dep_paths:
            actions.append({"action": "add", "route_path": p})
    for p in removed_paths:
        # Only if still in the file; one already removed by an earlier deploy is
        # not outstanding work.
        if p in dep_paths and p not in des_paths:
            actions.append({"action": "delete", "route_path": p})

    plugins_before = list(deployed.get("plugins") or [])
    plugins_after = list(desired.get("plugins") or [])
    prio_before = int(deployed.get("regex_priority") or 0)
    prio_after = int(desired.get("regex_priority") or 0)

    changed = bool(actions) or sorted(plugins_before) != sorted(plugins_after) \
        or prio_before != prio_after

    return {
        "paths": actions,
        "plugins_before": plugins_before,
        "plugins_after": plugins_after,
        "regex_priority_before": prio_before,
        "regex_priority_after": prio_after,
        "changed": changed,
    }


# ── component ──────────────────────────────────────────────────────────────────

class AsporaKongRouteScriptGenComponentV2:
    """
    Reconciles a service's gateway routes from the DB into the terragrunt HCL and
    stages the modified file for the PR. HCL surgery only — DB reads via the
    repository, GitHub via the GitOps handler; no validation or PR-raising here.
    """

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.repository = repository

    async def generate(
        self,
        tenant: str,
        repository=None,
        file_location=None,
        queue_dict: dict | None = None,
        workflow_context=None,
        db=None,
        upload_to_s3: bool = True,
    ) -> str:
        """
        Reconcile a service's routes into the gateway HCL (edit-based). Matches the
        ScriptGenHandler dispatch contract (same call shape as v1); the service
        identity is read from `queue_dict['config_snapshot']`:
            service_mst_code / services_mst_code  (required)
            api_name / service_name               (owner group key; `-service` enforced)
            environment, geo_loc_mst_code          (optional scoping)
        Returns the modified full HCL content (also staged into workflow_context).
        """
        component_name = self.__class__.__name__
        queue_dict = queue_dict or {}
        snap = queue_dict.get("config_snapshot") or {}

        # Identity comes from the snapshot, never from a lookup. A gateway row is
        # scope-keyed — its snapshot names the service, environment and region the
        # change was saved against — so there is nothing to resolve and nothing
        # that can disagree with the row being applied.
        #
        # `groups` is also what selects the path below: present means the delta
        # flow, absent means a legacy service-keyed snapshot for _apply_incremental.
        # Same discriminator the file locator uses to pick this component at all.
        delta_entries = snap.get("groups")
        is_delta = isinstance(delta_entries, list)

        service_mst_code = snap.get("service_mst_code") or snap.get("services_mst_code")
        if not service_mst_code:
            raise ValueError("config_snapshot.service_mst_code is required for gateway route generation")

        service_name = snap.get("api_name") or snap.get("service_name") or ""
        if service_name and not service_name.endswith("-service"):
            service_name = f"{service_name}-service"

        environment = snap.get("environment")
        geo_loc_mst_code = snap.get("geo_loc_mst_code")
        queue_id = queue_dict.get("id") or queue_dict.get("queue_id")
        script_gen_key = getattr(file_location, "script_gen_key", None)

        # An approved gateway row with no groups has nothing to write. Raising
        # beats returning the file unchanged: that produces an empty PR, which the
        # deploy reports as success having written nothing — the exact silent
        # failure the queue's own guards elsewhere are built to avoid. Both
        # save_gateway_changes and ensure_deploy_items refuse to leave a row in
        # this state, so reaching it means something upstream broke.
        if is_delta and not delta_entries:
            raise ValueError(
                f"Gateway queue row for '{service_name or service_mst_code}' carries no "
                f"group changes — nothing to generate."
            )

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or getattr(file_location, "target_branch", "") or ""

        # 1. Existing HCL — prefer a staged copy, else fetch from GitHub.
        cached = None
        if workflow_context and not getattr(workflow_context, "skip_commit", False):
            cached = _find_staged_entry(workflow_context, file_location.repo, base_branch, file_location.file_path)

        if cached:
            existing_content = cached.get("content")
        else:
            existing_file = await fetch_existing_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_location.file_path,
                base_branch=base_branch,
                feature_branch=feature_branch,
                workflow_context=workflow_context,
                logger=self.logger,
                component_name=component_name,
            )
            if existing_file.get("status") == "error":
                raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")
            if not existing_file.get("exists"):
                raise ValueError(
                    f"Kong gateway configuration file does not exist: {file_location.file_path}. "
                    f"The gateway stack must exist before generated routes can be spliced in."
                )
            existing_content = existing_file["content"]

        # Named route_repo, not repo: `repo` above holds the GitHub repository name
        # and is still read further down. Reusing the name worked only because the
        # later read goes through file_location.repo, which is a coincidence rather
        # than a design, and this function now has more between the two.
        route_repo = KongRouteConfigsRepository(db)

        # This service's active routes. Used for the owner-group decision on both
        # paths, and as the desired state on the legacy path.
        rows = await route_repo.list_routes_for_service(
            services_code=service_mst_code, environment=environment, geo_loc_mst_code=geo_loc_mst_code,
        )

        # The owner group carries terragrunt's `service { }` block; every other
        # group of the service points at it with existing_service. Resolved ONCE,
        # from the whole service's rows — it is a service-level fact, and deciding
        # it per group would let a non-owner group name itself and emit a service
        # block that belongs elsewhere.
        #
        # The groups being written are passed as a last resort. Route rows are
        # not created until a change has MERGED, so a service's first gateway
        # change reaches here with no rows at all and nothing to resolve from —
        # see _owner_group.
        written_keys = [
            k for k in dict.fromkeys(
                [(e.get("route_group_key") or "").strip() for e in delta_entries if isinstance(e, dict)]
                if is_delta else [(group_key_of(r) or service_name) for r in rows]
            ) if k
        ]

        owner_group = self._owner_group(
            rows, service_name,
            [(group_key_of(r) or service_name) for r in rows],
            proposed_keys=written_keys if is_delta else None,
            content=existing_content,
        )

        # The owner has to be a group that EXISTS in the file once this change is
        # applied: it is the only entry carrying a service block, and every other
        # group names it with existing_service. An owner that is neither already
        # in the file nor written by this change is created by nobody, so each
        # pointer resolves to a missing kong_service key.
        #
        # This is reachable from the DB-derived rules whenever route rows name a
        # group the gateway file never received — a change that was queued and
        # rolled back, or rows imported ahead of their file. Re-resolving over
        # the groups actually being written keeps the deploy correct instead of
        # emitting a file terraform rejects.
        if (
            written_keys
            and not (existing_content and _find_block(
                existing_content, f'"{owner_group}"', *_kc_bounds(existing_content)
            ))
            and owner_group not in written_keys
        ):
            corrected = service_name if service_name in written_keys else sorted(written_keys)[0]
            self.logger.warning(
                "kong: owner group '%s' for '%s' is in neither the gateway file nor this "
                "change — nothing would create it. Using '%s' instead.",
                owner_group, service_name, corrected,
            )
            owner_group = corrected

        # A group already in the file that owns no service block cannot be named
        # as existing_service — terraform indexes kong_service.services by the
        # owner's key and fails the plan on a miss. Reaching here means the file
        # is already inconsistent (its owner was renamed or deleted by hand), so
        # say which group is broken instead of writing another pointer to it.
        if (
            existing_content
            and _find_block(existing_content, f'"{owner_group}"', *_kc_bounds(existing_content))
            and not group_owns_service(existing_content, owner_group)
        ):
            raise ValueError(
                f"Gateway file has no service block for '{owner_group}', the owner group of "
                f"'{service_name}'. Every other group points at it with existing_service, and "
                f"terraform resolves that to kong_service.services[\"{owner_group}\"], which "
                f"would not exist. Restore the service block on '{owner_group}' (host/protocol/"
                f"port/path) before deploying this service's routes."
            )

        self.logger.info(
            "kong incremental: %d active route(s) for '%s'; %s",
            len(rows), service_name,
            f"{len(delta_entries)} changed group(s)" if is_delta else "legacy snapshot",
        )

        # The upstream for any service block this run creates, built from the gateway
        # file's path — which the file locator already scoped to this environment and
        # region. A per-service service_config from the snapshot wins when present;
        # today nothing sets it, so the path is the only source.
        service_config = resolve_service_config(file_location.file_path, snap.get("service_config"))

        with log_timing(self.logger, f"{component_name}.reconcile", context=f"service={service_name}"):
            if is_delta:
                # The queue row already names the actions, per group, so nothing is
                # derived here — see _apply_from_delta.
                content = await self._apply_from_delta(
                    existing_content, snap, service_name, owner_group, service_config,
                )
            else:
                content = await self._apply_incremental(
                    existing_content, route_repo, service_mst_code, service_name,
                    environment, geo_loc_mst_code, rows, service_config,
                )

        self.logger.warning(
            "[KONG-V2] service=%s routes=%d groups=%s env=%s geo=%s content_changed=%s file=%s",
            service_name, len(rows), len(delta_entries) if is_delta else "-",
            environment, geo_loc_mst_code,
            content != existing_content, file_location.file_path,
        )

        # Stage the modified content for the PR.
        _upsert_staged_entry(
            workflow_context, repo=file_location.repo, base_branch=base_branch,
            feature_branch=feature_branch, file_path=file_location.file_path,
            content=content, queue_id=queue_id, script_gen_key=script_gen_key,
        )
        _append_commit_message(
            workflow_context, file_location.repo, base_branch,
            f"gateway: update Kong routes for {service_name}",
        )

        # Record the generated file like every other component does — the PR
        # preview endpoint reads script_gen_responses, and without this entry a
        # gateway change previews as "no files at all" rather than as its
        # terragrunt diff.
        if queue_id:
            workflow_context.script_gen_responses[queue_id][script_gen_key] = {
                "original_content": content,
                "preview_content": content,
            }
        return content

    async def _apply_from_delta(
        self, content, snap, service_name, owner_group, service_config,
    ):
        """
        Apply EVERY group's recorded change set to the gateway HCL.

        The queue row already says what happened — add, edit, delete, per path,
        per group — so nothing is derived here. That is the whole reason the delta
        is stored as actions rather than as a desired-state snapshot: the snapshot
        flow this replaced had to rebuild two pictures and compare them to reach
        the same three verbs.

        The loop did not appear here, it MOVED here. One queue row used to hold one
        group, so three changed groups meant three rows and three calls into this
        component, each editing the same staged buffer in turn. Now one row holds
        them all and the chaining is a local variable — `content = f(content)` —
        instead of stage/read/stage. Same primitives, same order, same output file.

        Every group needs its own pass because every primitive is group-scoped:
        add_route finds `kong_configs["<group>"]` by name, set_group_priority edits
        that one entry, and the plugin target key is "<group>-<method>". None of
        them can touch two groups at once, so the loop is simply the feeder.

        Group identity comes from the ENTRY, not from kong_route_groups. That is
        what removes the per-deploy DB lookup this used to do, and it means a
        renamed group cannot make an in-flight change target the wrong HCL block.

        Every primitive is idempotent (add_route no-ops when the path is already
        there, remove_route when it is already gone, replace_route_path falls back
        to an append), so a Temporal retry — which re-runs ALL groups from the
        start — converges instead of double-applying.

        Order between groups does not matter: each edits its own kong_configs
        entry, and a non-owner group created before the owner's entry exists is
        fine because existing_service resolves at plan time, not parse time.
        Sorted anyway, so repeat runs produce byte-identical output.
        """
        entries = sorted(
            (e for e in (snap.get("groups") or []) if isinstance(e, dict)),
            key=lambda e: ((e.get("route_group_key") or ""), (e.get("http_method") or "")),
        )

        for delta in entries:
            group_key = delta.get("route_group_key") or service_name
            method = (delta.get("http_method") or "").upper()
            # The delta's target value, not the group row's. The row is the live
            # DB value and could have moved on since ensure_deploy_items recomputed
            # this change against the file; the snapshot is what was approved.
            priority = int(delta.get("regex_priority_after") or 0)

            added = removed = edited = 0
            for entry in (delta.get("paths") or []):
                action = (entry.get("action") or "").strip().lower()
                path = entry.get("route_path")
                if not path:
                    self.logger.warning(
                        "[KONG-DELTA] group=%s skipping %s entry with no route_path",
                        group_key, action or "?",
                    )
                    continue

                if action == "add":
                    content = add_route(
                        content, owner_group, group_key, method, path,
                        service_config, regex_priority=priority,
                    )
                    added += 1
                elif action == "delete":
                    content = remove_route(content, group_key, method, path)
                    removed += 1
                elif action == "edit":
                    old_path = entry.get("old_path")
                    if not old_path:
                        # Without the deployed path there is no line to swap, and
                        # appending the new one alone would leave the old route live.
                        raise ValueError(
                            f"Gateway change for '{group_key} · {method}' has an edit with no "
                            f"old_path ({path}) — cannot tell which line to replace."
                        )
                    content = replace_route_path(
                        content, owner_group, group_key, method,
                        old_path, path, service_config,
                    )
                    edited += 1
                else:
                    raise ValueError(
                        f"Unknown gateway action '{action}' for '{group_key} · {method}'"
                    )

            # regex_priority is group-level, so set it whatever the paths did — a
            # priority-only change carries no path entries at all.
            content = set_group_priority(content, group_key, priority)

            # Plugins are applied per (group, method) target key, and reconciled from
            # the DESIRED set rather than from the delta: the target either carries a
            # plugin or it does not, so converging is both simpler and self-healing.
            # An empty group is a special case — its target key is gone from the file,
            # so adding plugin targets to it would resurrect a dead route.
            target = f"{group_key}-{method.lower()}"
            still_present = _route_present_in_group(content, group_key, method)
            desired = {
                _kong_plugin(p)[0] for p in (delta.get("plugins_after") or []) if p
            } if still_present else set()

            plugin_edits = 0
            for kong in sorted(MANAGED_PLUGIN_KONG_NAMES | desired):
                before = content
                content = (
                    add_plugin_target(content, kong, target, base_indent=2)
                    if kong in desired
                    else remove_plugin_target(content, kong, target)
                )
                plugin_edits += content != before

            self.logger.warning(
                "[KONG-DELTA] group=%s method=%s add=%d edit=%d remove=%d plugin_edits=%d",
                group_key, method, added, edited, removed, plugin_edits,
            )

        return content

    @staticmethod
    def _owner_group(deployed_rows, service_name, keys, proposed_keys=None, content=None):
        """
        The group carrying terragrunt's `service { }` block; every other group points
        at it with existing_service.

        `content` — the gateway file being edited — is consulted FIRST and wins
        outright, because it is the only source that describes what terraform
        will actually read. layers/gateway creates `kong_service.services` keyed
        by the owning group and resolves routes with
        `kong_service.services[existing_service]`, a single hop with no fallback:
        name a group that does not own a service block and the plan fails with a
        missing-key error.

        Deriving it from anything else made ownership MOVE. The rules below rank
        service_name above the recorded is_service_owner, so a service whose
        first deploy created only tag groups (owner correctly resolved to a tag,
        service block written there) silently switched to service_name as soon as
        a base-named group merged its first row — while the block stayed where it
        was. Every group written after that pointed at a group that owns nothing.

        The remaining rules only run for a service with no entry in the file yet,
        where there is nothing to preserve and any answer is a fresh choice:
        kong_route_groups' recorded owner, else the service's own label, else the
        keys the change proposes (rows are written only after a PR merges, so a
        first-ever change reaches here with nothing deployed to learn from).
        """
        names = []
        for k in keys:
            name = k[0] if isinstance(k, tuple) else k
            if name not in names:
                names.append(name)
        candidates = names + [k for k in (proposed_keys or []) if k] + [service_name]

        # The file wins: preserving the deployed owner is what keeps
        # existing_service pointing at a group that owns a service block.
        if content:
            deployed_owner = find_owner_in_content(content, candidates)
            if deployed_owner:
                return deployed_owner

        # Nothing owns a service block in the file yet — pick one. The recorded
        # owner comes first now: it is evidence about a real deployment, where
        # service_name is only a convention.
        owners = [
            (group_key_of(r) or service_name) for r in deployed_rows
            if getattr(getattr(r, "route_group", None), "is_service_owner", False)
        ]
        if owners:
            return sorted(set(owners))[0]
        if service_name in names:
            return service_name
        if not names:
            names = [k for k in dict.fromkeys(proposed_keys or []) if k]
            # The service's own label owns it when the change creates it, matching
            # the rule applied to deployed keys above.
            if service_name in names:
                return service_name
        return sorted(names)[0] if names else service_name

    async def _apply_incremental(
        self, content, repo, service_mst_code, service_name, environment, geo_loc_mst_code, active_rows,
        service_config,
    ):
        """
        Edit-based reconcile: ADD only not-yet-deployed routes (creation_status ==
        INITIATED), REMOVE only soft-deleted ones, leave ACTIVE routes and anything
        hand-written untouched. Strips leftover devlift markers and reconciles the
        common kong_plugins block by plugin name.
        """
        from app.core.enum import DeploymentStatusEnum

        # This approach is markerless — clean any leftover fence/warning lines.
        content = strip_devlift_markers(content)

        # Drift detection (visibility ONLY — no auto-fix). A route the DB thinks is
        # deployed (ACTIVE) but missing from the file means it vanished by other means
        # (bad merge, manual edit). We just log it — incremental won't resurrect it,
        # so re-touch it in the UI + deploy to restore.
        #
        # Only check routes we can locate PRECISELY: v2-managed rows always have a
        # route_group_key, so we know exactly which group they live under. Legacy
        # rows have route_group_key = NULL — we can't reliably find them and must NOT
        # flag them as drift, so they're skipped.
        missing = [
            f"{r.http_method} {r.route_path} [{group_key_of(r)}]"
            for r in active_rows
            if getattr(r, "creation_status", None) == DeploymentStatusEnum.ACTIVE
            and group_key_of(r)
            and not _route_present(content, group_key_of(r), r.http_method, r.route_path)
        ]
        if missing:
            self.logger.warning(
                "[KONG-DRIFT] service=%s — %d ACTIVE route(s) in the DB are MISSING from the file "
                "(NOT re-added; re-touch them in the Gateway tab + deploy to restore): %s",
                service_name, len(missing), ", ".join(missing),
            )

        # Owner group (emits the service{} block): the group named exactly
        # `service_name` if present, else the first by name. Derived from ALL active
        # rows so it's stable even when only a secondary route is added this deploy.
        keys: list[str] = []
        for r in active_rows:
            k = (group_key_of(r) or service_name)
            if k not in keys:
                keys.append(k)
        # The owner is the group carrying the terragrunt `service { }` block — every
        # other group points at it with existing_service. kong_route_groups records
        # which one that is (is_service_owner, set by the import), so use it rather
        # than guessing. The old fallback took the alphabetically first key, which
        # is only right by luck: goblin resolved to "dynamic-rates-api" and
        # verification to a typo'd group, so any NEW group would have been attached
        # to the wrong upstream.
        owner_keys = [
            k for k in keys
            if any(
                (group_key_of(r) or service_name) == k
                and getattr(getattr(r, "route_group", None), "is_service_owner", False)
                for r in active_rows
            )
        ]
        if service_name in keys:
            owner_group = service_name
        elif owner_keys:
            owner_group = sorted(owner_keys)[0]
        else:
            owner_group = sorted(keys)[0] if keys else service_name

        to_add = [r for r in active_rows if getattr(r, "creation_status", None) == DeploymentStatusEnum.INITIATED]
        to_remove = await repo.list_deleted_routes_for_service(
            services_code=service_mst_code, environment=environment, geo_loc_mst_code=geo_loc_mst_code,
        )

        # Bucket adds/removes by (group, method). A remove paired with an add in the
        # SAME bucket is a path EDIT — do it as an in-place replace so the route keeps
        # its position (clean diff). Leftovers are plain adds/removes.
        from collections import defaultdict
        adds: dict = defaultdict(list)
        removes: dict = defaultdict(list)
        # A group's regex_priority, from its kong_route_groups row. Group-level, so
        # every route in the group reports the same value. Collected from ALL of the
        # service's live rows, not just the ones being added: a group whose routes are
        # already ACTIVE still needs its line, and a priority-only edit adds nothing.
        priorities: dict = {}
        for r in active_rows:
            grp = getattr(r, "route_group", None)
            if grp is not None:
                priorities[(group_key_of(r) or service_name)] = grp.regex_priority or 0
        for r in to_add:
            adds[((group_key_of(r) or service_name), r.http_method.upper())].append(r.route_path)
        for r in to_remove:
            removes[((group_key_of(r) or service_name), r.http_method.upper())].append(r.route_path)

        for group, method in sorted(set(adds) | set(removes)):
            a = adds.get((group, method), [])
            # Only count removes that are ACTUALLY in the file as real deletions.
            # Stale soft-deleted paths (already gone) would otherwise mis-pair with an
            # add by index and turn a clean in-place edit into a remove + append reshuffle.
            d = [p for p in removes.get((group, method), []) if _route_present(content, group, method, p)]
            n = min(len(a), len(d))
            for i in range(n):  # paired = in-place path edits (position preserved)
                content = replace_route_path(content, owner_group, group, method, d[i], a[i], service_config)
            for p in d[n:]:     # leftover deletes
                content = remove_route(content, group=group, method=method, path=p)
            for p in a[n:]:     # leftover pure adds
                content = add_route(
                    content, owner_group=owner_group, group=group, method=method, path=p,
                    service_config=service_config,
                    regex_priority=priorities.get(group, 0),
                )

        # Kong breaks a same-path tie by regex_priority, so every group this service
        # owns must carry the value the DB holds. Done for ALL of them, not just the
        # ones edited this deploy: add_route only writes it when it CREATES the entry,
        # so a group deployed before priority support — or one whose routes are all
        # ACTIVE — would never get the line. A group already matching is a no-op.
        for group, priority in sorted(priorities.items()):
            content = set_group_priority(content, group, priority)


        # Plugins are reconciled SURGICALLY, like the routes above — only the target
        # keys of the routes in this deploy are touched.
        #
        # Rebuilding the entries from the DB (the old reconcile_plugins) rewrote every
        # target_keys list wholesale, so any target devlift does not know about was
        # dropped: on the real stage gateway that silently removed JWT from 29 routes
        # belonging to services outside this DB. Never rewrite a list we do not own
        # in full.
        # A plugin target key is (group, method) — a Kong ROUTE, not a path. So it is
        # decided per key from the FINAL state, never per row: iterating adds and then
        # removes made a delete undo an add. Deleting one path from a group while
        # adding another to it (same method) removed the group's JWT/user-id-injection
        # target even though the route still exists — the default group, where paths
        # get edited most, silently lost both plugins.
        def _target_key(r) -> str:
            return f"{(group_key_of(r) or service_name)}-{r.http_method.strip().lower()}"

        # A route with NO kong_route_groups row tells us nothing about its plugins —
        # plugins_of() returns [] for "the group says none" and for "there is no group"
        # alike. Treating the second as the first is how an un-backfilled database
        # removes live auth: right after migration 155/156 every FK is NULL, so a
        # deploy would reconcile every key toward empty and strip JWT from the gateway.
        # Verified by simulation: with groups forced to None, `verification` drops
        # "verification-service-get" from routes_jwt_auth on a deploy that adds and
        # removes nothing.
        #
        # So an ungrouped route is INVISIBLE to the plugin reconcile — it contributes
        # no key, and no key is judged from it. Backfill makes it visible again.
        # Scoped per row rather than "skip if the service has no groups at all", so a
        # partial backfill can't strip the half that hasn't landed yet.
        grouped = [r for r in active_rows if getattr(r, "route_group", None) is not None]
        ungrouped = len(active_rows) - len(grouped)
        if ungrouped:
            self.logger.warning(
                "[KONG-PLUGINS] service=%s — %d of %d route(s) have no kong_route_groups row; "
                "their plugin targets are left ALONE (run the terragrunt import to backfill)",
                service_name, ungrouped, len(active_rows),
            )

        # What each key SHOULD carry: every live route still in that (group, method).
        desired: dict[str, set] = {}
        for r in grouped:
            desired.setdefault(_target_key(r), set()).update(
                _kong_plugin(p)[0] for p in plugins_of(r) if p
            )
        # Sync EVERY key this service owns, not just the ones edited this deploy.
        # Two things need that: a plugins-only edit (tick user-id-injection, touch no
        # path) adds and removes nothing, and a key the add/remove bug above already
        # stripped would stay stripped forever — no later deploy would repair it,
        # since nothing in that group changes again.
        #
        # This is still surgical: keys come from this service's own groups and are
        # added/removed individually. What must never come back is the old
        # reconcile_plugins, which REBUILT each target_keys list from the DB and so
        # dropped every target devlift didn't know about — that silently removed JWT
        # from 29 stage routes belonging to other services. Verified against the live
        # stage gateway: a full sync of all services adds the 3 genuinely-missing keys
        # and removes nothing.
        # Deletions likewise only count when we know which group they were in.
        touched = set(desired) | {
            _target_key(r) for r in to_remove if getattr(r, "route_group", None) is not None
        }

        plugin_edits = 0
        for key in sorted(touched):
            carried = desired.get(key, set())  # empty => route fully gone, drop it
            for kong in sorted(MANAGED_PLUGIN_KONG_NAMES | carried):
                before = content
                content = (add_plugin_target(content, kong, key, base_indent=2)
                           if kong in carried
                           else remove_plugin_target(content, kong, key))
                plugin_edits += content != before

        self.logger.warning(
            "[KONG-INCR] service=%s owner=%s add=%d remove=%d plugin_target_edits=%d",
            service_name, owner_group, len(to_add), len(to_remove), plugin_edits,
        )
        return content
