"""Helpers for parsing Atlantis PR comments."""

import re

# Cap stored error text — the full output stays on the PR.
ERROR_EXCERPT_MAX = 1000


def extract_atlantis_error(body_raw: str, kind: str) -> str | None:
    """
    Extract the error text from an Atlantis failure comment body.

    kind: "plan" | "apply"

    Returns the first fenced code block after the failure marker (Atlantis
    wraps the terraform/terragrunt output in one), falling back to the plain
    text after the marker. Truncated to ERROR_EXCERPT_MAX chars. None when
    the body carries no failure marker or nothing usable remains.
    """
    if not body_raw:
        return None
    body = body_raw.lower()
    positions = [body.find(m) for m in (f"{kind} error", f"{kind} failed") if m in body]
    if not positions:
        return None
    tail = body_raw[min(positions):]
    m = re.search(r"```(?:\w+)?\n(.*?)```", tail, re.DOTALL)
    excerpt = m.group(1) if m else tail
    # Drop the HTML wrappers Atlantis adds around long output
    excerpt = re.sub(r"</?(details|summary)[^>]*>", "", excerpt).strip()
    if len(excerpt) > ERROR_EXCERPT_MAX:
        excerpt = excerpt[:ERROR_EXCERPT_MAX] + "\n… (truncated — full output on the PR)"
    return excerpt or None
