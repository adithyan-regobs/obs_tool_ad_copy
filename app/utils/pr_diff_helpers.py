"""
PR diff normalization helpers.

Pure, stateless transformation of a GitHub compare diff (or, as a fallback, the
full committed file contents) into a safe, compact representation for LLM-based PR
title/description generation.

Design (see the feature plan):
- SECURITY FIRST: secret VALUES are masked while KEYS are kept
  (`password = "«redacted»"`), so the model still learns *"a password was set"*
  without the value ever leaving our trust boundary. Redaction FAILS CLOSED:
  when a line/file is uncertain it is redacted rather than risk a leak.
- Injection defense: the rendered payload is wrapped in an <UNTRUSTED_DIFF> block;
  the system prompt instructs the model to treat it as data, never instructions.
- Large/binary files: represented by a label + counts, never raw bytes.
- Truncation: the file manifest (every path) is ALWAYS included; only per-file
  detail is dropped when over budget, so no change is ever silently hidden.

These are pure functions with no I/O — the GitHub fetch lives in the callers.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REDACTION_PLACEHOLDER = '"«redacted»"'
_UNTRUSTED_OPEN = "<UNTRUSTED_DIFF>"
_UNTRUSTED_CLOSE = "</UNTRUSTED_DIFF>"

# Key names whose value must be treated as a secret -> mask the value, keep the key.
_SECRET_KEY_RE = re.compile(
    r"(?i)("
    r"password|passwd|pwd|secret|token|credential|"
    r"private[_-]?key|access[_-]?key|api[_-]?key|auth[_-]?token|client[_-]?secret"
    r")"
)

# HCL/JSON assignment on a single (sign-stripped) line:  key = "value"  |  "key": value
_ASSIGN_RE = re.compile(r'^(?P<pre>\s*"?(?P<key>[A-Za-z0-9_.\-]+)"?\s*[:=]\s*).*$')

# Value-level catches (defense in depth, even when the key is unknown):
_KMS_CIPHERTEXT_RE = re.compile(r"AQICAH[A-Za-z0-9+/=]{20,}")   # AWS KMS blob prefix
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")           # long high-entropy blob

# Whole files whose contents are treated as sensitive -> redact every value (fail closed).
_SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)secrets\.json$"
    r"|\.tfvars(?:\.json)?$"
    r"|templates/terragrunt/database/",
    re.IGNORECASE,
)

_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".tgz", ".whl", ".jar", ".class", ".so", ".dylib", ".dll",
    ".exe", ".bin", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".wasm",
    ".pyc",
}


@dataclass
class NormalizedChange:
    """A single changed file, cleaned and secret-redacted, ready for the prompt."""
    path: str
    status: str                       # added | modified | removed | renamed
    additions: Optional[int]
    deletions: Optional[int]
    kind: str                         # text | binary | large
    changes: str                      # redacted + cleaned diff/content (may be a label)


# --------------------------------------------------------------------------- #
# Secret redaction (mask value, keep key; fail closed)
# --------------------------------------------------------------------------- #

def _is_sensitive_path(path: str) -> bool:
    return bool(path and _SENSITIVE_PATH_RE.search(path))


def _redact_line(line: str, sensitive_file: bool) -> str:
    """Redact a single line's secret value while preserving its key/structure."""
    # Preserve a leading diff sign (+/-) but not the file headers.
    sign = ""
    body = line
    if line[:1] in "+-" and not line.startswith(("+++", "---")):
        sign, body = line[0], line[1:]

    match = _ASSIGN_RE.match(body)
    if match:
        key = match.group("key")
        # Mask when the key looks secret OR the whole file is sensitive (fail closed).
        if sensitive_file or _SECRET_KEY_RE.search(key):
            return f"{sign}{match.group('pre')}{REDACTION_PLACEHOLDER}"

    # Value-level catch: known ciphertext / long high-entropy blob anywhere on the line.
    if _KMS_CIPHERTEXT_RE.search(body) or _LONG_B64_RE.search(body):
        if match:
            return f"{sign}{match.group('pre')}{REDACTION_PLACEHOLDER}"
        return f"{sign} {REDACTION_PLACEHOLDER}"

    return line


def _redact_text(text: str, path: str) -> str:
    sensitive = _is_sensitive_path(path)
    return "\n".join(_redact_line(ln, sensitive) for ln in text.splitlines())


# --------------------------------------------------------------------------- #
# Patch cleaning + file classification
# --------------------------------------------------------------------------- #

def _clean_patch(patch: str) -> str:
    """
    Simplify a patch: strip the `@@ -a,b +c,d @@` line numbers to a bare separator
    (keeping any section heading), drop file-header lines, but KEEP context lines.

    Context lines are kept because in shared-state files (DB users, Kong routes) the
    entity identity — e.g. `name = "yahiya"` — lives in an unchanged context line, and
    the PR title needs it to say *whose* permission changed. Diffs for updates are small,
    so the token cost is negligible; oversized patches are still capped at render time.
    """
    out: List[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            # Real hunk headers are `@@ -a,b +c,d @@ heading`; keep only the heading.
            parts = line.split("@@")
            heading = (parts[-1] if len(parts) >= 3 else line[2:]).strip()
            out.append(f"@@ {heading}".rstrip() if heading else "@@")
        elif line.startswith(("+++", "---")):
            continue  # file headers (usually absent from GitHub's patch field anyway)
        else:
            out.append(line)  # +, -, and context lines all kept
    return "\n".join(out)


def _classify(entry: Dict[str, Any]) -> str:
    path = entry.get("path") or entry.get("file_path") or ""
    ext = os.path.splitext(path)[1].lower()
    if ext in _BINARY_EXTS:
        return "binary"
    # Compare API omits `patch` for very large (and binary) files.
    if "patch" in entry and entry.get("patch") is None:
        return "large"
    return "text"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def normalize_diff(entries: List[Dict[str, Any]]) -> List[NormalizedChange]:
    """
    Normalize a list of change entries into safe NormalizedChange objects.

    Each entry may be a GitHub compare file `{path,status,additions,deletions,patch}`
    or a committed-file fallback `{path,content,status}`. Content is redacted either way.
    """
    result: List[NormalizedChange] = []
    for entry in entries or []:
        path = entry.get("path") or entry.get("file_path") or "unknown"
        status = entry.get("status", "added")
        adds = entry.get("additions")
        dels = entry.get("deletions")
        kind = _classify(entry)

        if kind == "binary":
            changes = "[binary file changed]"
        elif kind == "large":
            a = adds if adds is not None else "?"
            d = dels if dels is not None else "?"
            changes = f"[large file — {a} additions, {d} deletions; content omitted]"
        else:
            is_patch = "patch" in entry and entry.get("patch") is not None
            if is_patch:
                raw = entry.get("patch") or ""
            else:
                # committed-file fallback or workflow-context entry: content, else config JSON
                raw = entry.get("content")
                if not raw and entry.get("config"):
                    try:
                        raw = json.dumps(entry["config"], indent=2, default=str)
                    except Exception:
                        raw = str(entry["config"])
                raw = raw or ""
            redacted = _redact_text(raw, path)  # redaction runs on config values too
            changes = _clean_patch(redacted) if is_patch else redacted

        result.append(NormalizedChange(
            path=path, status=status, additions=adds, deletions=dels,
            kind=kind, changes=changes,
        ))
    return result


def render_changes_for_prompt(
    changes: List[NormalizedChange],
    max_per_file: int = 8000,   # ECS/service files run ~6KB; 2KB starved them of config detail
    max_total: int = 60000,     # gpt-4o has a large context — plenty of headroom
) -> str:
    """
    Render normalized changes into the delimited payload sent to the LLM.

    Always lists every file (manifest); per-file detail is added within a budget so
    no change is invisible. Wrapped in <UNTRUSTED_DIFF> for prompt-injection safety.
    """
    if not changes:
        return ""

    manifest = [f"Files changed ({len(changes)}):"]
    for c in changes:
        counts = (
            f" (+{c.additions}/-{c.deletions})"
            if c.additions is not None and c.deletions is not None else ""
        )
        manifest.append(f"- {c.status}: {c.path}{counts}")
    manifest_text = "\n".join(manifest)

    details: List[str] = []
    total = len(manifest_text)
    omitted = 0
    for c in changes:
        body = c.changes or ""
        if len(body) > max_per_file:
            body = body[:max_per_file] + "\n... [content truncated] ..."
        block = f"=== {c.path} ({c.status}) ===\n{body}\n"
        if total + len(block) > max_total:
            omitted += 1
            continue
        details.append(block)
        total += len(block)

    payload = manifest_text + "\n\n--- file changes ---\n\n" + "\n".join(details)
    if omitted:
        payload += (
            f"\n... detailed content for {omitted} more file(s) omitted for size; "
            "see the file list above ...\n"
        )
    return f"{_UNTRUSTED_OPEN}\n{payload}\n{_UNTRUSTED_CLOSE}"


async def collect_pr_changes(
    *,
    token: str,
    base_url: str,
    owner: str,
    repo: str,
    base: str,
    head: str,
    fallback_entries: Optional[List[Dict[str, Any]]] = None,
) -> List[NormalizedChange]:
    """
    Shared fetch + normalize used by every PR flow (removes the duplicated wiring).

    Primary source is the real diff (feature `head` vs the PR's `base` branch). If the
    diff is empty or the fetch fails, fall back to `fallback_entries` (the committed
    `{path, content}` files) so a create still gets described. Diff-fetch failures are
    logged with their reason (auth/rate-limit/network/repo) rather than silently ignored.
    Returns already-redacted, normalized changes ready for the LLM.
    """
    from app.integrations.github_integration import GitHubIntegration, DiffStatus

    try:
        result = await GitHubIntegration.get_branch_diff(
            token=token, base_url=base_url, owner=owner, repo=repo, base=base, head=head,
        )
    except Exception as e:  # get_branch_diff shouldn't raise, but never let this break a PR
        logger.warning(f"collect_pr_changes: diff fetch raised, using fallback: {e}")
        return normalize_diff(fallback_entries or [])

    if result.status == DiffStatus.OK and result.files:
        return normalize_diff(result.files)

    if result.status not in (DiffStatus.OK, DiffStatus.EMPTY):
        logger.warning(
            f"collect_pr_changes: diff unavailable ({result.status.value}) for "
            f"{base}...{head}; using committed-file fallback"
        )

    return normalize_diff(fallback_entries or [])
