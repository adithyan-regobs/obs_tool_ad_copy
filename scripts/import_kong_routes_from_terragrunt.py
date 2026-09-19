"""
Import existing Kong routes from a gateway terragrunt.hcl into the DB.

The DB only knows the routes devlift created. The gateway file is what is actually
deployed. This reads the file and creates the missing kong_route_groups /
kong_route_configs rows so the Gateway tab reflects reality and future edits are
incremental instead of re-adding everything.

Parsing reuses the GENERATOR'S OWN HCL scanner rather than a new parser — anything
a separate parser disagreed about, generation would get wrong too.

Imported routes are marked ACTIVE, never INITIATED. The incremental generator picks
what to add with `creation_status == INITIATED`; importing hundreds of already-live
routes as INITIATED would make the next deploy try to re-add every one of them.

Group -> devlift service is resolved in this order, most specific first:
  1. exact match on services_mst.name
  2. label minus a trailing "-service"
  3. punctuation-insensitive match  (appserver-service -> app-server)
  4. MANUAL_SERVICE_MAP below       (reward-api-service -> rewards-api)
  5. for an override group, whatever its existing_service owner resolves to
Anything still unresolved is reported and skipped — never guessed at.

Usage:
    ./venv/bin/python scripts/import_kong_routes_from_terragrunt.py \
        --file "/path/to/terragrunt.hcl" --env stage --geo region-aspora-mumbai
    ... --apply

Without --apply it is a dry run. Idempotent: existing groups are reconciled and
existing paths skipped, so re-running changes nothing.
"""
import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.handlers  # noqa: F401,E402  (imported first: breaks a circular import)
from sqlalchemy import select, text  # noqa: E402

from app.core.enum import DeploymentStatusEnum  # noqa: E402
from app.db.models.kong_route_config_model import KongRouteConfigModel as R  # noqa: E402
from app.db.models.kong_route_group_model import KongRouteGroupModel as G  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.domain.factories.kong_route_config_factory import make_kong_route_config  # noqa: E402
from app.domain.factories.kong_route_group_factory import make_kong_route_group  # noqa: E402
from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component_v2 import (  # noqa: E402
    _find_block, _match_brace, _method_array_span, _paths_in_array,
    PLUGIN_CATALOG, PLUGIN_ENTRY_KEY,
)

# Groups whose label cannot be matched by rule — singular/plural, a suffix after
# "-service", or a display name that shares nothing with the label. Confirmed by
# hand; the importer will not infer these.
MANUAL_SERVICE_MAP = {
    "bbps-service-ai": "bbps",
    "pulse-service": "pulse-api",
    "reward-api-service": "rewards-api",
    "settlements-service": "settlements-api",
    # "wa-bot-service": ...  — unconfirmed. Nothing in services_mst resembles
    # "wa-bot"; the nearest are "Koda WhatsApp Bot" and "ai-bot", neither of which
    # is a safe inference. Left out so its routes are skipped and reported rather
    # than attached to the wrong service. Add it here once confirmed.
}

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")
IMPORT_ACTOR = "terragrunt-import"

# Only these plugins are imported.
#
# Devlift adopts their existing gateway entry by key (PLUGIN_ENTRY_KEY:
# jwt -> routes_jwt_auth, user-id-injection -> user_id_injection), so recording
# them cannot produce a SECOND entry for the same plugin on the same route —
# which Kong rejects. Both are also single-entry/single-config in the gateway, so
# devlift's name-only model represents them faithfully.
#
# Everything else (cors, key-auth, request-transformer, rate-limiting, ...) has
# per-route configs devlift cannot hold: importing those names would make it emit
# a competing entry carrying a default config — e.g. rewriting
# centhealth_leads_cors's specific origins to ["*"]. Their routes are still
# imported; only their plugin names are skipped, so the hand-written entries stay
# untouched and authoritative.
IMPORTABLE_PLUGINS = {kong for kong in PLUGIN_ENTRY_KEY}


def _uncommented(text: str) -> str:
    """
    Drop `#` comments before pulling quoted strings out of a list.

    Not cosmetic: routes_jwt_auth lists 42 target_keys of which 37 are commented
    out — several annotated "public callbacks". Reading them as live would record
    JWT on routes that deliberately do not have it.
    """
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


# ── file parsing ──────────────────────────────────────────────────────────────

# Map keys come in BOTH styles: kong_configs quotes them ("appserver-service" = {)
# while kong_plugins uses bare identifiers (api_token_validator = {). Matching only
# the quoted form silently skips every plugin and then matches `"add" = {` inside the
# jsonencode bodies instead, which yields nonsense target_keys.
_ENTRY_KEY = re.compile(r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_-]*))\s*=\s*\{')


def _entries(content: str, open_i: int, close_i: int):
    """Yield (key, start, end) for each `key = { ... }` directly inside a map."""
    i = open_i + 1
    while i < close_i:
        m = _ENTRY_KEY.search(content, i, close_i)
        if not m:
            return
        end = _match_brace(content, m.end() - 1)
        if end == -1:
            return
        # Skipping the whole entry body keeps nested `x = {` out of the results.
        yield (m.group(1) or m.group(2)), m.start(), end
        i = end + 1


def parse_gateway(content: str):
    """-> ({group: {...}}, {"<group>-<method>": [display plugin names]})"""
    groups: Dict[str, dict] = {}
    kc = _find_block(content, "kong_configs")
    if kc:
        _, o, c = kc
        for key, s, e in _entries(content, o, c):
            body = content[s:e]
            routes: Dict[str, List[str]] = {}
            rb = _find_block(content, "routes", s, e)
            if rb:
                _, ro, rc = rb
                for meth in METHODS:
                    arr = _method_array_span(content, meth, ro + 1, rc)
                    if arr:
                        _, lb, rbk = arr
                        paths = _paths_in_array(content[lb + 1:rbk])
                        if paths:  # skip `"DELETE" = []` — generates nothing
                            routes[meth] = paths
            prio = re.search(r"regex_priority\s*=\s*(\d+)", body)
            owner_ref = re.search(r'existing_service\s*=\s*"([^"]+)"', body)
            groups[key] = {
                "is_owner": re.search(r"\n\s*service\s*=\s*\{", body) is not None,
                "existing_service": owner_ref.group(1) if owner_ref else None,
                "regex_priority": int(prio.group(1)) if prio else 0,
                "routes": routes,
            }

    kong_to_display = {kong: disp for disp, (kong, _cfg) in PLUGIN_CATALOG.items()}
    by_target: Dict[str, List[str]] = defaultdict(list)
    kp = _find_block(content, "kong_plugins")
    if kp:
        _, o, c = kp
        for _key, s, e in _entries(content, o, c):
            body = content[s:e]
            nm = re.search(r'name\s*=\s*"([^"]+)"', body)
            tk = re.search(r"target_keys\s*=\s*\[(.*?)\]", body, re.S)
            if not (nm and tk):
                continue
            kong = nm.group(1)
            if kong not in IMPORTABLE_PLUGINS:
                continue  # hand-written and per-route configured — see IMPORTABLE_PLUGINS
            # Files store Kong names (jwt); the DB stores display names (JWT).
            display = kong_to_display.get(kong, kong)
            for target in re.findall(r'"([^"]+)"', _uncommented(tk.group(1))):
                by_target[target].append(display)
    return groups, dict(by_target)


# ── service resolution ────────────────────────────────────────────────────────

def _same_plugins(a, b) -> bool:
    """True if two plugin lists resolve to the same set of Kong plugin names."""
    def kong_names(names):
        out = set()
        for n in names or []:
            if n in PLUGIN_CATALOG:
                out.add(PLUGIN_CATALOG[n][0])
            else:
                out.add(n.strip().lower().replace(" ", "-").replace("_", "-"))
        return out
    return kong_names(a) == kong_names(b)


def _strip(name: str) -> str:
    return name[: -len("-service")] if name.endswith("-service") else name


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _strip(name.strip().lower()))


def resolve_service(
    label: str, groups: Dict[str, dict], by_name: Dict[str, List[str]],
) -> Tuple[Optional[str], str]:
    """-> (service_name, how). service_name is None when nothing matched safely."""
    if label in by_name:
        return label, "exact"
    if _strip(label) in by_name:
        return _strip(label), "strip"
    if label in MANUAL_SERVICE_MAP and MANUAL_SERVICE_MAP[label] in by_name:
        return MANUAL_SERVICE_MAP[label], "manual"

    norm_index: Dict[str, List[str]] = defaultdict(list)
    for name in by_name:
        norm_index[_norm(name)].append(name)
    candidates = norm_index.get(_norm(label), [])
    if len(candidates) == 1:
        return candidates[0], "normalised"
    if len(candidates) > 1:
        return None, f"AMBIGUOUS {candidates}"

    owner = groups.get(label, {}).get("existing_service")
    if owner and owner != label:
        svc, how = resolve_service(owner, groups, by_name)
        if svc:
            return svc, f"via owner {owner}"
    return None, "no match"


# ── import ────────────────────────────────────────────────────────────────────

async def run(path: str, env: str, geo: str, apply: bool,
              tenant: Optional[str] = None, app: Optional[str] = None,
              only: Optional[List[str]] = None) -> None:
    groups, plugins_by_target = parse_gateway(open(path).read())

    # --only: bring in specific groups without touching anything else. A full run
    # re-adds every path the file has that the DB lacks — including ones deleted
    # deliberately in the UI, which are soft-deleted rows the importer cannot tell
    # from "never imported". Scoping the run avoids resurrecting them.
    if only:
        wanted = set(only)
        keep = {k: v for k, v in groups.items()
                if k in wanted or (v.get("existing") in wanted)}
        dropped = len(groups) - len(keep)
        groups = keep
        print(f"--only: {len(groups)} group(s) in scope, {dropped} ignored\n")

    async with AsyncSessionLocal() as db:
        # Scope candidate services to one tenant/application. Matching on name
        # across the whole table is too loose: a gateway group called
        # "kyc-service" matched a service named KYC-Service belonging to an
        # unrelated tenant, and "pulse-service" matched pulse-api in a different
        # application. Both names were unique, so the ambiguity guard — which only
        # fires on duplicates — let them through.
        by_name: Dict[str, List[str]] = defaultdict(list)
        rows = (await db.execute(text("""
            select s.name, s.code
            from services_mst s
            left join applications_mst a on a.code = s.applications_mst_code
            where s.is_deleted = false
              -- CAST: asyncpg cannot infer a bind param's type from `$1 is null`
              and (cast(:tenant as text) is null or s.tenants_mst_code = cast(:tenant as text))
              and (cast(:app    as text) is null or a.name             = cast(:app    as text))
        """), {"tenant": tenant, "app": app})).all()
        for name, code in rows:
            by_name[name].append(code)
        scope = f"tenant={tenant or 'ANY'} app={app or 'ANY'}"
        print(f"candidate services: {len(rows)}  ({scope})")
        if tenant is None and app is None:
            print("  WARNING: unscoped — a name can match a service in another tenant")

        existing_groups = {
            (g.route_group_key, g.http_method): g
            for g in (await db.execute(
                select(G).where(G.environments_enum == env, G.geo_loc_mst_code == geo)
            )).scalars().all()
        }
        # Existing rows in scope, split by whether they already know their group.
        #
        # These used to be read with an INNER JOIN to kong_route_groups, which meant
        # an UNLINKED row was invisible — so every path looked new and the importer
        # inserted a second copy beside it. Harmless while every row was linked;
        # catastrophic right after a schema reset, when none of them are.
        #
        # Linked rows are keyed by (label, method, path): the label matters because
        # the same path in two groups is a legitimate override, not a duplicate.
        # Unlinked rows have no label to key on — that is the column the reset
        # cleared — so they are pooled by (method, path) and adopted by whichever
        # group in the file claims them. Two groups sharing a path each take one.
        linked_paths = set()
        unlinked_pool: Dict[Tuple[str, str], List[R]] = defaultdict(list)
        rows_in_scope = (await db.execute(
            select(R).where(
                R.is_deleted == False,  # noqa: E712
                R.environments_enum == env,
                R.geo_loc_mst_code == geo,
            )
        )).scalars().all()
        group_key_by_id = {g.id: g.route_group_key for g in existing_groups.values()}
        for r in rows_in_scope:
            method_u = (r.http_method or "").upper()
            if r.kong_route_group_id is not None:
                label_r = group_key_by_id.get(r.kong_route_group_id)
                if label_r is not None:
                    linked_paths.add((label_r, method_u, r.route_path))
                    continue
            unlinked_pool[(method_u, r.route_path)].append(r)

        new_groups = new_paths = reused = skipped_paths = relinked = 0
        skipped: List[str] = []

        print(f"{'group':<42} {'meth':<7} {'paths':>5}  service / reason")
        print("-" * 104)

        for label in sorted(groups, key=str.lower):
            meta = groups[label]
            if not meta["routes"]:
                continue

            for method, paths in sorted(meta["routes"].items()):
                target_plugins = plugins_by_target.get(f"{label}-{method.lower()}", [])
                group = existing_groups.get((label, method))

                if group is not None:
                    # Already devlift-managed — the DB already knows which service
                    # this belongs to, so name matching is neither needed nor
                    # trustworthy (a devlift group label is rarely a service name).
                    svc_name, svc_code, how = group.api_name, group.services_mst_code, "in DB"
                else:
                    svc_name, how = resolve_service(label, groups, by_name)
                    if svc_name is None:
                        skipped_paths += len(paths)
                        skipped.append(f"{label} {method} ({len(paths)} paths) — {how}")
                        print(f"{label:<42} {method:<7} {len(paths):>5}  SKIP: {how}")
                        continue
                    codes = by_name[svc_name]
                    if len(codes) > 1:
                        skipped_paths += len(paths)
                        skipped.append(
                            f"{label} {method} ({len(paths)} paths) — '{svc_name}' maps to {len(codes)} service codes")
                        print(f"{label:<42} {method:<7} {len(paths):>5}  SKIP: '{svc_name}' is not unique")
                        continue
                    svc_code = codes[0]

                if group is None:
                    data = make_kong_route_group(
                        route_group_key=label,
                        http_method=method,
                        api_name=svc_name,
                        services_mst_code=svc_code,
                        environments_enum=env,
                        geo_loc_mst_code=geo,
                        plugins=target_plugins,
                        regex_priority=meta["regex_priority"],
                    )
                    # The file is authoritative on ownership: a group holding a
                    # service{} block IS the owner, whatever its label looks like.
                    data["is_service_owner"] = meta["is_owner"]
                    if apply:
                        group = G(**data)
                        db.add(group)
                        await db.flush()
                        existing_groups[(label, method)] = group
                    new_groups += 1
                    mark = "NEW GROUP"
                else:
                    if apply:
                        # Keep the DB's spelling when both sides mean the same Kong
                        # plugins. The file stores kong names ("request-transformer")
                        # and the DB display names ("Request Transformer"); for a
                        # plugin outside PLUGIN_CATALOG neither reverses to the other,
                        # so a blind overwrite would churn the label for no change in
                        # generated output.
                        if _same_plugins(group.plugins, target_plugins):
                            target_plugins = list(group.plugins or [])
                        group.plugins = target_plugins
                        group.regex_priority = meta["regex_priority"]
                        group.is_service_owner = meta["is_owner"]
                        db.add(group)
                    reused += 1
                    mark = "reconcile"

                added = 0
                adopted = 0
                for route_path in paths:
                    if (label, method, route_path) in linked_paths:
                        continue  # already this group's — nothing to do

                    # Present but unlinked: adopt it rather than inserting a twin.
                    # This is what makes a re-import after a schema reset put the
                    # database back together instead of doubling it. The row keeps
                    # its code, so queue items and terragrunt that reference it stay
                    # valid, and it is marked ACTIVE because the file says it is live.
                    pool = unlinked_pool.get((method, route_path))
                    if pool:
                        row_obj = pool.pop(0)
                        if apply:
                            row_obj.kong_route_group_id = group.id
                            row_obj.api_name = svc_name
                            row_obj.services_mst_code = svc_code
                            row_obj.creation_status = DeploymentStatusEnum.ACTIVE
                            db.add(row_obj)
                        adopted += 1
                        relinked += 1
                        continue

                    if apply:
                        row = make_kong_route_config(
                            api_name=svc_name,
                            http_method=method,
                            route_path=route_path,
                            services_mst_code=svc_code,
                            environments_enum=env,
                            geo_loc_mst_code=geo,
                            # ACTIVE: already deployed. INITIATED would make the
                            # next deploy try to re-add every imported route.
                            creation_status=DeploymentStatusEnum.ACTIVE,
                            creation_status_updated_by=IMPORT_ACTOR,
                            kong_route_group_id=group.id,
                        )
                        db.add(R(**row))
                    added += 1
                    new_paths += 1

                own = " OWNER" if meta["is_owner"] else ""
                pr = f" prio={meta['regex_priority']}" if meta["regex_priority"] else ""
                adopt = f" ~{adopted} relinked" if adopted else ""
                print(f"{label:<42} {method:<7} {len(paths):>5}  {svc_name} [{how}] "
                      f"[{mark}{own}{pr}] +{added} path(s){adopt} {target_plugins or ''}")

        print("-" * 104)
        print(f"groups : {new_groups} new, {reused} reconciled")
        print(f"paths  : {new_paths} to insert, {relinked} existing relinked to their group")
        print(f"skipped: {skipped_paths} paths across {len(skipped)} group(s)")
        for s in skipped:
            print(f"   - {s}")

        if apply:
            await db.commit()
            print("\nAPPLIED.")
        else:
            await db.rollback()
            print("\nDry run — nothing written. Re-run with --apply to commit.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import Kong routes from a gateway terragrunt.hcl")
    ap.add_argument("--file", required=True, help="path to the gateway terragrunt.hcl")
    ap.add_argument("--env", required=True, help="environment, e.g. stage")
    ap.add_argument("--geo", required=True, help="geo_loc_mst_code, e.g. region-aspora-mumbai")
    ap.add_argument("--tenant", help="restrict service matching to this tenants_mst_code")
    ap.add_argument("--app", help="restrict service matching to this application name")
    ap.add_argument("--only", action="append", default=[],
                    help="import only these gateway group keys (repeatable). "
                         "Everything else is reported but untouched — use it to bring "
                         "in one service without re-adding paths deleted elsewhere.")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    args = ap.parse_args()
    asyncio.run(run(args.file, args.env, args.geo, args.apply, args.tenant, args.app, args.only))


if __name__ == "__main__":
    main()
