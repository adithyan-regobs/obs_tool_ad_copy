"""Generate the security report: every route and its access declaration.

Usage:  ./venv/bin/python scripts/security_report.py            # readable table
        ./venv/bin/python scripts/security_report.py --json     # JSON for tooling/PR diffs

The report is GENERATED from the route table (the `_access` labels that
SecureRouter attaches), never hand-written — so it can never drift from
the code. Commit the JSON on each build and every PR's security changes
show up as a plain diff.

Ported from the openfga 4 PoC. Adaptation for FastAPI 0.137: include_router
keeps sub-routers NESTED (_IncludedRouter), so routes are walked recursively,
prefixes are reconstructed, and include-level declarations (public_surface on
an include) count for every route beneath them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.routing import APIRoute

from app.main import app
from app.core.authz.security import AuthenticationOnly, Public, Authorization, AuthorizationFromBody


def _access_of(dependencies):
    for d in dependencies or []:
        access = getattr(d.dependency, "_access", None)
        if access is not None:
            return access
    return None


def walk(routes, prefix="", inherited=None):
    """Yield (full_path, route, access) — access is the route's own declaration
    or the nearest include-level one."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield prefix + r.path, r, (_access_of(r.dependencies) or inherited)
        elif type(r).__name__ == "_IncludedRouter":
            ctx = r.include_context
            inc_access = _access_of(getattr(ctx, "dependencies", None)) or inherited
            yield from walk(r.original_router.routes, prefix + (ctx.prefix or ""), inc_access)


def build_report() -> dict:
    report = {"public": {}, "authentication_only": {}, "authorization": {}, "UNDECLARED": {}}
    for path, route, access in walk(app.routes):
        key = f"{'/'.join(sorted(route.methods))} {path}"
        match access:
            case Public(reason=reason):
                report["public"][key] = {"reason": reason}
            case AuthenticationOnly(reason=reason):
                report["authentication_only"][key] = {"authorization": "inside handler", "reason": reason}
            case Authorization():
                report["authorization"][key] = {
                    "permission": access.permission,
                    "object": f"{access.obj_type}:<{access.param}>",
                    "on_deny": access.deny_status,
                }
            case AuthorizationFromBody():
                report["authorization"][key] = {
                    "permission": access.permission,
                    "object": f"{access.obj_type}:<body.{access.field}>",
                    "on_deny": access.deny_status,
                }
            case None:
                report["UNDECLARED"][key] = {"DANGER": "no access declaration!"}
    return report


def main() -> None:
    report = build_report()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    counts = {section: len(routes) for section, routes in report.items()}
    total = sum(counts.values())
    print(f"SECURITY REPORT — {total} routes")
    print("  authorization: %s   authentication_only: %s   public: %s   UNDECLARED: %s"
          % (counts["authorization"], counts["authentication_only"], counts["public"], counts["UNDECLARED"]))
    for section, routes in report.items():
        if not routes:
            continue
        print(f"\n\u2500\u2500 {section} ({len(routes)}) " + "\u2500" * (48 - len(section)))
        for key, info in sorted(routes.items()):
            detail = ", ".join(f"{k}={v}" for k, v in info.items())
            print(f"  {key:55s} {detail}")


if __name__ == "__main__":
    main()
