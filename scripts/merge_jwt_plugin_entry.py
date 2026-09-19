"""
One-off: fold devlift's `"jwt"` kong_plugins entry into the hand-written
`routes_jwt_auth` one, leaving a single entry keyed `"jwt"`.

Why:
  The gateway has two jwt entries — `routes_jwt_auth` (hand-written, 42 targets)
  and `"jwt"` (devlift, 4). Both apply the SAME plugin, so devlift cannot take
  over the 42 routes: generating `"jwt"` for them would put jwt on a route twice
  and Kong rejects that. It also cannot remove the hand-written one, because
  _remove_plugin_entry only matches a QUOTED key and `routes_jwt_auth` is bare.

  Merging is safe here because the two configs are behaviourally identical: every
  shared key matches, and the extras `routes_jwt_auth` carries
  (uri_param_names, secret_is_base64, anonymous) are Kong's own defaults.
  That is NOT true of cors/key-auth, whose configs genuinely differ per route —
  do not reuse this for them.

The surviving entry keeps `routes_jwt_auth`'s position but is keyed `"jwt"` so
devlift's existing remove-and-regenerate cycle owns it, with no code change. Its
config is normalised to devlift's so the next deploy produces no diff.

Writes to --out (never in place). Review the diff before opening a PR.

Usage:
    ./venv/bin/python scripts/merge_jwt_plugin_entry.py --file gateway.hcl
    ./venv/bin/python scripts/merge_jwt_plugin_entry.py --file gateway.hcl --out merged.hcl
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.handlers  # noqa: F401,E402  (breaks a circular import)
from app.plugin.aspora.script_gen_components.aspora_kong_route_script_gen_component_v2 import (  # noqa: E402
    _find_block, _match_brace, PLUGIN_CATALOG,
)

ENTRY_KEY = re.compile(r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_-]*))\s*=\s*\{')


def find_jwt_entries(content):
    """-> [(key, was_quoted, start, end, targets)] for every entry whose name = "jwt"."""
    kp = _find_block(content, "kong_plugins")
    if kp is None:
        raise SystemExit("no kong_plugins block in this file")
    _, open_i, close_i = kp

    out, i = [], open_i + 1
    while i < close_i:
        m = ENTRY_KEY.search(content, i, close_i)
        if not m:
            break
        end = _match_brace(content, m.end() - 1)
        if end == -1:
            break
        body = content[m.end():end]
        name = re.search(r'name\s*=\s*"([^"]+)"', body)
        if name and name.group(1) == "jwt":
            tk = re.search(r"target_keys\s*=\s*\[(.*?)\]", body, re.S)
            targets = re.findall(r'"([^"]+)"', tk.group(1)) if tk else []
            line_start = content.rfind("\n", 0, m.start()) + 1
            # swallow the trailing comma/newline so removal leaves no blank line
            tail = end + 1
            while tail < len(content) and content[tail] in ", \t":
                tail += 1
            if tail < len(content) and content[tail] == "\n":
                tail += 1
            out.append((m.group(1) or m.group(2), bool(m.group(1)), line_start, tail, targets))
        i = end + 1
    return out


def render(targets, indent="    "):
    i2 = indent + "  "
    cfg = json.dumps(PLUGIN_CATALOG["JWT"][1])
    lines = [
        f'{indent}"jwt" = {{',
        f'{i2}name        = "jwt"',
        f'{i2}target      = "route"',
        f'{i2}target_keys = [' + ", ".join(f'"{t}"' for t in targets) + "]",
        f"{i2}config_json = jsonencode({cfg})",
        f"{indent}}}",
    ]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Merge the two jwt kong_plugins entries into one")
    ap.add_argument("--file", required=True)
    ap.add_argument("--out", help="output path (default: <file>.merged.hcl)")
    args = ap.parse_args()

    content = open(args.file).read()
    entries = find_jwt_entries(content)

    print(f"jwt entries found: {len(entries)}")
    for key, quoted, _s, _e, targets in entries:
        print(f"   {key:<22} {'quoted' if quoted else 'bare':<7} {len(targets)} targets")
    if len(entries) < 2:
        raise SystemExit("nothing to merge — expected two jwt entries")

    # Keep the entry with the MOST targets in place; fold the others into it.
    keeper = max(entries, key=lambda e: len(e[4]))
    merged, seen = [], set()
    for _k, _q, _s, _e, targets in entries:
        for t in targets:
            if t not in seen:
                seen.add(t)
                merged.append(t)

    print(f"\nkeeping '{keeper[0]}' in place, re-keyed as \"jwt\"")
    print(f"merged targets: {len(merged)} "
          f"({' + '.join(str(len(e[4])) for e in entries)}, {sum(len(e[4]) for e in entries)-len(merged)} duplicate)")

    # Rewrite from the END of the file backwards so earlier offsets stay valid.
    indent = content[keeper[2]: content.index('"', keeper[2]) if keeper[1] else keeper[2] + 0]
    indent = re.match(r"[ \t]*", content[keeper[2]:]).group(0)
    for key, _q, start, end, _t in sorted(entries, key=lambda e: e[2], reverse=True):
        if (start, end) == (keeper[2], keeper[3]):
            content = content[:start] + render(merged, indent) + content[end:]
        else:
            content = content[:start] + content[end:]

    left = find_jwt_entries(content)
    print(f"\nafter merge: {len(left)} jwt entry")
    for key, quoted, _s, _e, targets in left:
        print(f"   {key:<22} {'quoted' if quoted else 'bare':<7} {len(targets)} targets")
    assert len(left) == 1 and left[0][1] and len(left[0][4]) == len(merged), "merge did not converge"

    out = args.out or (args.file + ".merged.hcl")
    open(out, "w").write(content)
    print(f"\nwritten: {out}")
    print("Review the diff, then raise it as a PR. Import JWT routes only AFTER it merges.")


if __name__ == "__main__":
    main()
