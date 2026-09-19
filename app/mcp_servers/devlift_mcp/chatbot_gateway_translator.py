"""
Chatbot `gateway_group` → one v2 gateway delta entry.

The chatbot's kong form sends the same two blocks every other form sends
(`attribute_parameters`, `placement_parameters`) plus one extra object,
`gateway_group`, describing the Gateway card the user filled in: one HTTP
method, secured or not, one tag, a priority, plugins, and one or more paths.

Only `gateway_group` needs code. Its four transformations cannot be expressed as
template copies, which is all the form's result_template can do:

  1. compile each path      /api/v1/users -> ~/api/v1/users$
  2. wrap each path         "~/a$" -> {"action": "add", "route_path": "~/a$"}
  3. secured -> a plugin    true -> "JWT" in the plugin list
  4. drop duplicate paths

They are also devlift's rules rather than the chat UI's — Kong's path syntax,
the plugin catalog's name for auth, and the shape of a v2 delta entry. Keeping
them here means one copy; the form is duplicated per tenant (vance, aspora) and
anything encoded there is two copies that drift.

Everything else the card carries — method, tag, priority, plugin list — is
passed through unchanged; it is the transformation's INPUT, not logic.

Keep in step with the form at
`chat-bot-POC/backend/data/metadata/<tenant>/kong_route_form.json`.
"""

from typing import Any, Dict, List, Optional

from app.domain.policies.kong_route_group_naming import default_group_key
from app.utils.kong_path_compiler import compile_route_path

#: Display name of the auth plugin. `secured: true` on the form means "this card
#: requires a JWT", and everything downstream stores that as a PLUGIN, not a
#: boolean — so it has to be translated here or the routes deploy public.
#: Matches DEFAULT_AUTH_PLUGIN in GatewayContentV4.tsx and the "JWT" key in the
#: generator's PLUGIN_CATALOG.
AUTH_PLUGIN = "JWT"


def _as_bool(value: Any) -> bool:
    """`secured` arrives as a real bool, or as the string a dropdown carries
    ("true"/"yes"/"auth"). Anything unrecognised counts as NOT secured:
    defaulting to public is visible and correctable, while defaulting to secured
    would put a JWT on routes meant to be open and read as the gateway rejecting
    valid traffic."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "auth", "jwt", "secured", "protected"}


def _compiled_paths(raw: Any) -> List[str]:
    """Transformations 1 and 4: compile, keep order, drop duplicates.

    `compile_route_path` is idempotent, so a path the form already anchored
    passes through untouched while a plain one gets its `~` and `$`. Duplicates
    collapse because the same path twice is one route — the second would be
    written as a duplicate line in the HCL.
    """
    if isinstance(raw, str):
        raw = [raw]
    out: List[str] = []
    seen = set()
    for item in raw or []:
        text = (item or "").strip() if isinstance(item, str) else ""
        if not text:
            continue
        compiled = compile_route_path(text)
        if compiled not in seen:
            seen.add(compiled)
            out.append(compiled)
    return out


def build_gateway_group_delta(
    gateway_group: Dict[str, Any], api_name: str
) -> Optional[Dict[str, Any]]:
    """
    Turn one chatbot gateway card into one v2 delta group entry.

    Returns None when there is no card, so the caller leaves a non-kong form
    alone. Raises ValueError when the card is present but unusable — better here,
    where the message can be handed back into the chat turn, than deep inside the
    generator.

    `api_name` comes from the form's own attribute_parameters; it decides the
    default group key, and must match what the generator derives or the routes
    deploy into a group nobody writes.
    """
    if not isinstance(gateway_group, dict) or not gateway_group:
        return None

    method = (gateway_group.get("http_method") or "").strip().upper()
    paths = _compiled_paths(gateway_group.get("paths"))

    missing = [
        name for name, value in (("http_method", method), ("paths", paths)) if not value
    ]
    if not (api_name or "").strip():
        missing.append("service_name")
    if missing:
        raise ValueError(
            f"Chatbot gateway_group is incomplete — missing {', '.join(missing)}. "
            f"Finish the Kong route form in chat before deploying."
        )

    route_group_key = (gateway_group.get("route_group_key") or "").strip() or default_group_key(api_name)
    regex_priority = int(gateway_group.get("regex_priority") or 0)

    # Transformation 3: the boolean becomes a plugin, in front of the rest so the
    # auth plugin reads first wherever the list is shown.
    plugins = [p for p in (gateway_group.get("plugins") or []) if isinstance(p, str) and p.strip()]
    if _as_bool(gateway_group.get("secured")) and AUTH_PLUGIN not in plugins:
        plugins = [AUTH_PLUGIN, *plugins]

    # `*_after` is what the generator reads (the approved target state); the bare
    # keys are what the DB writer falls back to. A create has no "before", so
    # both carry the same value.
    return {
        "route_group_key": route_group_key,
        "http_method": method,
        "regex_priority": regex_priority,
        "regex_priority_after": regex_priority,
        "plugins": plugins,
        "plugins_after": plugins,
        # Transformation 2.
        "paths": [{"action": "add", "route_path": p} for p in paths],
        "desired_paths": [{"route_path": p} for p in paths],
    }
