"""
Kong route path compiler.

The Gateway UI stores plain, human-friendly paths (e.g. ``/api/v1/users``,
``/files/:id``). Kong needs a PCRE route pattern (``~/api/v1/users$``). This
module compiles the former into the latter, server-side, so the frontend never
has to know Kong's regex syntax.

Idempotent: a value already in Kong form (starts with ``~``) is returned as-is,
so callers that already send compiled patterns keep working.
"""

import re

# Regex metacharacters escaped inside a literal segment. Intentionally excludes
# ``-`` so names like "beneficiary-service" stay readable and match the existing
# hand-written terragrunt style.
_META = re.compile(r"([.^$*+?()\[\]{}|\\])")


def _escape_literal(segment: str) -> str:
    """Escape regex-special chars in a literal path segment (keeps ``-``)."""
    return _META.sub(r"\\\1", segment)


def compile_route_path(path: str) -> str:
    """
    Compile a plain UI path into a Kong regex route path (``~/...$``).

    Conversions (per path segment):
      - ``:name``  or ``{name}``  -> ``(?<name>[^/]+)``   (one path segment)
      - ``*``                     -> ``(?<rest[N]>.+)``   (greedy catch-all)
      - literal                   -> regex-escaped (``.`` etc.)

    Idempotent: input starting with ``~`` is returned unchanged.

    Examples:
      /api/v1/users     -> ~/api/v1/users$
      /files/:id        -> ~/files/(?<id>[^/]+)$
      /files/{fileId}   -> ~/files/(?<fileId>[^/]+)$
      /files/*          -> ~/files/(?<rest>.+)$
      /report.json      -> ~/report\\.json$
      ~/already/regex$  -> ~/already/regex$   (unchanged)
    """
    path = (path or "").strip()
    if not path:
        raise ValueError("Route path cannot be empty")

    # Already a Kong regex — leave it alone (idempotent for existing callers).
    # A trailing `$` counts too: the gateway holds anchored paths written without
    # the `~` (e.g. "/dummy-route$"), and compiling one escaped the anchor into a
    # literal, so re-saving an untouched route silently corrupted it. No real URL
    # path ends in a literal `$`, so this is safe to treat as already-compiled.
    if path.startswith("~") or path.endswith("$"):
        return path

    if not path.startswith("/"):
        path = "/" + path

    wildcard_n = 0
    out: list[str] = []
    for seg in path.split("/"):
        if seg == "":
            out.append("")  # preserve slash boundaries (incl. the leading slash)
            continue
        if seg == "*":
            name = "rest" if wildcard_n == 0 else f"rest{wildcard_n}"
            wildcard_n += 1
            out.append(f"(?<{name}>.+)")
        elif seg.startswith(":") and len(seg) > 1:
            out.append(f"(?<{seg[1:]}>[^/]+)")
        elif seg.startswith("{") and seg.endswith("}") and len(seg) > 2:
            out.append(f"(?<{seg[1:-1]}>[^/]+)")
        else:
            out.append(_escape_literal(seg))

    return f"~{'/'.join(out)}$"
