"""
Kong route-group naming.

A route lands in a GROUP, and the group is what carries the terragrunt
``route_config { regex_priority = N }`` block. A route added with no tag goes
into the service's default group — which is fine until two groups claim the same
path, because the tie is then broken by priority and a default group sits at 0.

The Gateway tab never leaves this to chance: opening its "Add route" form
preloads a valid, unused tag rather than an empty box. Chat/MCP had no equivalent
— no tag, no suggestion, no way to know a default group was being chosen for you.
This module is that logic, server-side, so both surfaces name groups the same way.

TWO BASE KEYS, ONE RULE. The ``-tagN`` walk below is shared, but what it counts
from is not:

* **v1** (``create_route_from_infrastructure_request``, the kong executor, the
  generator's own no-tag fallback) counts from ``default_group_key`` — the api
  name with ``-service`` enforced, because ``generate()`` appends that suffix
  before using the name as a group key.
* **v2** (the Gateway tab and the chat/MCP flow behind it) counts from the PLAIN
  service name, matching ``preloadTag`` / ``suggestTags`` in
  GatewayContentV4.tsx and ``default_route_group_key`` in
  ``mcp_servers/devlift_mcp/service_payloads.py``.

So ``goms`` suggests ``goms-service-tag1`` on v1 and ``goms-tag1`` on v2, and
both are right for their own surface. ``tag_names`` / ``preload_for`` take the
base key explicitly and are what v2 callers use; ``suggest_tags`` /
``preload_tag`` are the v1 wrappers that supply the suffixed base. Keep the walk
itself in step with the frontend's ``suggestTags``.
"""

from typing import List, Sequence

#: Suffix the script generator enforces on a service name before using it as the
#: default group key. Kept here so the suggestion and the generated HCL cannot
#: disagree about what the default group is actually called.
_SERVICE_SUFFIX = "-service"


def default_group_key(api_name: str) -> str:
    """
    The group a route falls into when no tag is given.

    Resolved the way the generator resolves it — ``api_name`` with ``-service``
    enforced (see ``generate()`` in the v2 script-gen component, which appends the
    suffix before using the name as a group key). Deriving it any other way would
    let chat name a group the deploy then writes somewhere else.
    """
    name = (api_name or "").strip()
    if not name:
        return ""
    return name if name.endswith(_SERVICE_SUFFIX) else f"{name}{_SERVICE_SUFFIX}"


def _used(taken: Sequence[str]) -> set:
    return {str(t).strip().lower() for t in taken if t and str(t).strip()}


def tag_names(base_key: str, taken: Sequence[str] = (), count: int = 3) -> List[str]:
    """
    Next free ``<base_key>-tagN`` names, so a second group can be created without
    inventing a convention on the spot. Only offers names nobody is using yet.

    ``base_key`` is passed in rather than derived because v1 and v2 count from
    different bases — see the module docstring. Callers on v1 go through
    ``suggest_tags``, which supplies the ``-service`` form.

    ``taken`` is optional: chat can suggest without a DB round-trip, and the
    suggestion is still valid — a tag that turns out to be taken merges into that
    group rather than corrupting anything, and the save path checks it properly.
    Pass the service's known tags when they are at hand and the suggestion gets
    sharper.
    """
    base = (base_key or "").strip()
    if not base:
        return []
    used = _used(taken)
    out: List[str] = []
    n = 1
    # Same ceiling as the frontend: a service with 99 tags does not need a
    # hundredth suggested for it.
    while len(out) < count and n < 100:
        candidate = f"{base}-tag{n}"
        if candidate.lower() not in used:
            out.append(candidate)
        n += 1
    return out


def preload_for(base_key: str, taken: Sequence[str] = ()) -> str:
    """
    The tag to offer first, for a caller that already knows its base key.

    The base itself when it is free — that group gets the terragrunt
    ``service { }`` block and every other group points at it with
    ``existing_service``, so leaving it unclaimed means the owner falls back to
    whichever tag happens to sort first. Once taken, fall through to the next
    free ``<base_key>-tagN``.
    """
    base = (base_key or "").strip()
    if not base:
        return ""
    if base.lower() not in _used(taken):
        return base
    names = tag_names(base, taken, count=1)
    return names[0] if names else ""


def suggest_tags(api_name: str, taken: Sequence[str] = (), count: int = 3) -> List[str]:
    """v1: ``tag_names`` counting from the ``-service`` group key."""
    return tag_names(default_group_key(api_name), taken, count)


def preload_tag(api_name: str, taken: Sequence[str] = ()) -> str:
    """v1: ``preload_for`` counting from the ``-service`` group key."""
    return preload_for(default_group_key(api_name), taken)


def describe_group_choice(api_name: str, tag: str = "", taken: Sequence[str] = ()) -> str:
    """
    One sentence for the chat reply saying which group the route is going into,
    and how to choose a different one.

    Chat shows the executor's ``message`` verbatim, so this is the only place a
    user finds out that a group was picked for them. Saying nothing is how every
    chat-created route ended up in the default group without anyone deciding to
    put it there.
    """
    chosen = (tag or "").strip()
    if chosen:
        return f"Route group: '{chosen}'."

    default = default_group_key(api_name)
    suggestion = suggest_tags(api_name, taken, count=1)
    hint = suggestion[0] if suggestion else f"{default}-tag1"
    return (
        f"Route group: '{default}' (the service default — no tag was given). "
        f"To put this route in a NEW group instead, pass tag='{hint}' (any name "
        f"works). A new group also lets you set regex_priority, which is what "
        f"decides the winner when two groups match the same path."
    )
