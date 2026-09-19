"""
Line-level patch helpers for terragrunt/HCL files.

Used by script-gen components that patch-in-place: the existing repo file is
the base and only devlift-managed `field = value` lines are rewritten, so
hand-added inputs (lifecycle_rules, CORS, kms_key_arn, ...) survive redeploys.
Same mechanism as aspora_eks_terragrunt_script_gen_component's local helper.
"""

import re
from typing import Iterable, Tuple


def replace_scalar_field(content: str, field: str, value: str) -> Tuple[str, bool]:
    """Set the RHS of a single-line `field = <value>` assignment.

    Matches everything from `=` to end of line, so it works whether the base is
    a fresh template or an already-rendered file, and leaves the line's own
    indentation and alignment untouched. Returns (content, replaced).
    """
    pattern = rf'(^[ \t]*{re.escape(field)}[ \t]*=[ \t]*).*$'
    new_content, count = re.subn(
        pattern, lambda m: m.group(1) + value, content, count=1, flags=re.MULTILINE
    )
    return new_content, bool(count)


def replace_list_field(content: str, field: str, value: str) -> Tuple[str, bool]:
    """Like replace_scalar_field for a `field = [...]` assignment whose list
    may span multiple lines. `value` must include its own brackets.
    """
    pattern = rf'(^[ \t]*{re.escape(field)}[ \t]*=[ \t]*)\[[^\]]*\]'
    new_content, count = re.subn(
        pattern, lambda m: m.group(1) + value, content, count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    return new_content, bool(count)


def _insert_after_anchor(content: str, field: str, value: str, anchors: Iterable[str]) -> str:
    """Insert `field = value` on a new line after the first anchor assignment
    found (anchors tried in order), reusing the anchor's indentation. The
    anchor's RHS may be a multi-line `[...]` list. No-op when no anchor exists.
    """
    for anchor in anchors:
        m = re.search(
            rf'^([ \t]*){re.escape(anchor)}[ \t]*=[ \t]*(?:\[[^\]]*\]|[^\n]*)',
            content,
            flags=re.MULTILINE | re.DOTALL,
        )
        if m:
            return content[:m.end()] + f"\n{m.group(1)}{field} = {value}" + content[m.end():]
    return content


def upsert_scalar_field(content: str, field: str, value: str, anchors: Iterable[str]) -> str:
    """Replace `field = value` in place, or insert it after the first anchor
    field found. Returns content unchanged when neither the field nor any
    anchor exists.
    """
    new_content, replaced = replace_scalar_field(content, field, value)
    if replaced:
        return new_content
    return _insert_after_anchor(content, field, value, anchors)


def upsert_list_field(content: str, field: str, value: str, anchors: Iterable[str]) -> str:
    """upsert_scalar_field for a `field = [...]` assignment (value includes
    brackets); replacing handles an existing multi-line list.
    """
    new_content, replaced = replace_list_field(content, field, value)
    if replaced:
        return new_content
    return _insert_after_anchor(content, field, value, anchors)


def remove_scalar_field(content: str, field: str) -> Tuple[str, bool]:
    """Delete the whole `field = <value>` line (single-line RHS), newline
    included. For the explicit-clear signal: the caller decided the user wants
    the override gone so the terraform module default applies. Returns
    (content, removed).
    """
    pattern = rf'^[ \t]*{re.escape(field)}[ \t]*=[^\n]*\n?'
    new_content, count = re.subn(pattern, "", content, count=1, flags=re.MULTILINE)
    return new_content, bool(count)


def remove_list_field(content: str, field: str) -> Tuple[str, bool]:
    """remove_scalar_field for a `field = [...]` assignment whose list may
    span multiple lines. Returns (content, removed).
    """
    pattern = rf'^[ \t]*{re.escape(field)}[ \t]*=[ \t]*\[[^\]]*\][ \t]*\n?'
    new_content, count = re.subn(
        pattern, "", content, count=1, flags=re.MULTILINE | re.DOTALL
    )
    return new_content, bool(count)
