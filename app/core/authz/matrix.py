"""Derive a role → permissions matrix from the authorization model.

Answers "what can an admin actually do?" without anyone maintaining a second
list that drifts from the model.

The transitive part matters. On `service`, can_deploy is defined as `deployer`,
and `deployer` is defined as `... or admin`. So admin holds can_deploy without
can_deploy ever mentioning admin. A direct lookup would miss it; this walks the
implication graph.
"""

from typing import Any

import httpx

from .config import settings


def _walk(node: dict, implies: set[str], excludes: set[str], inherited: list[str]) -> None:
    """Collect which same-type relations imply this one.

    implies   — holding one of these grants the relation being defined
    excludes  — subtracted by a `but not`
    inherited — reached through a parent object (tupleToUserset)
    """
    if not isinstance(node, dict):
        return
    if "computedUserset" in node:
        rel = node["computedUserset"].get("relation")
        if rel:
            implies.add(rel)
    if "tupleToUserset" in node:
        ttu = node["tupleToUserset"]
        via = ttu.get("tupleset", {}).get("relation")
        rel = ttu.get("computedUserset", {}).get("relation")
        if via and rel:
            inherited.append(f"{rel} from {via}")
    if "union" in node:
        for child in node["union"].get("child", []):
            _walk(child, implies, excludes, inherited)
    if "intersection" in node:
        for child in node["intersection"].get("child", []):
            _walk(child, implies, excludes, inherited)
    if "difference" in node:
        _walk(node["difference"].get("base", {}), implies, excludes, inherited)
        sub_implies: set[str] = set()
        _walk(node["difference"].get("subtract", {}), sub_implies, set(), [])
        excludes |= sub_implies


async def fetch_model() -> dict[str, Any]:
    url = f"{settings.fga_api_url}/stores/{settings.fga_store_id}/authorization-models"
    if settings.fga_model_id:
        url += f"/{settings.fga_model_id}"
    async with httpx.AsyncClient(timeout=10) as client:
        data = (await client.get(url)).json()
    if "authorization_model" in data:
        return data["authorization_model"]
    models = data.get("authorization_models", [])
    if not models:
        raise RuntimeError("store has no authorization model")
    return models[0]


def build(model: dict[str, Any]) -> list[dict]:
    """One entry per type: its permissions, its roles, and who can do what."""
    out = []
    for td in model.get("type_definitions", []):
        relations = td.get("relations", {})
        if not relations:
            continue

        parsed: dict[str, dict] = {}
        structural: set[str] = set()
        for name, tree in relations.items():
            implies: set[str] = set()
            excludes: set[str] = set()
            inherited: list[str] = []
            _walk(tree, implies, excludes, inherited)
            parsed[name] = {
                "implies": implies,
                "excludes": excludes,
                "inherited": inherited,
            }
            for item in inherited:
                structural.add(item.split(" from ")[-1])

        # Forward edges: holding `src` grants `dst`.
        grants: dict[str, set[str]] = {name: set() for name in parsed}
        for dst, info in parsed.items():
            for src in info["implies"]:
                if src in grants:
                    grants[src].add(dst)

        def reachable(start: str) -> set[str]:
            seen, stack = set(), [start]
            while stack:
                cur = stack.pop()
                for nxt in grants.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            return seen

        permissions = sorted(n for n in parsed if n.startswith("can_"))
        roles = sorted(
            n for n in parsed
            if not n.startswith("can_") and n not in structural
        )

        rows = []
        for role in roles:
            reach = reachable(role) | {role}
            allowed = [p for p in permissions if p in reach]
            caveats = sorted(
                {e for p in allowed for e in parsed[p]["excludes"]}
            )
            rows.append({"role": role, "can": allowed, "unless": caveats})

        out.append({
            "type": td["type"],
            "permissions": permissions,
            "roles": roles,
            "matrix": rows,
            "sources": {
                name: {
                    "direct": bool(parsed[name]["implies"]) or True,
                    "from": sorted(parsed[name]["implies"]),
                    "inherited": parsed[name]["inherited"],
                    "excludes": sorted(parsed[name]["excludes"]),
                }
                for name in sorted(parsed)
            },
        })
    return out
