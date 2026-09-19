"""
Line-level patch helpers for YAML files (GitHub Actions workflows).

The YAML sibling of hcl_patch.py: used by script-gen components that patch an
existing workflow file in place — only devlift-managed `key: value` lines are
rewritten, so hand-added steps, env vars, and edited commands survive
redeploys. Callers pick keys that are unique within the file.
"""

import re
from typing import Sequence, Tuple


def replace_yaml_value(content: str, key: str, value: str) -> Tuple[str, bool]:
    """Set the RHS of a single-line `key: value` mapping entry.

    Replaces the first occurrence only, preserving the line's indentation.
    Returns (content, replaced) — replaced is False when the key is absent,
    which callers treat as "leave the file alone" (no inserts: workflow keys
    are template-borne).
    """
    pattern = rf'^([ \t]*{re.escape(key)}:[ \t]*).*$'
    new_content, count = re.subn(
        pattern, lambda m: m.group(1) + value, content, count=1, flags=re.MULTILINE
    )
    return new_content, bool(count)


def replace_nested_yaml_value(content: str, path: Sequence[str], value: str) -> Tuple[str, bool]:
    """Set the RHS of a `key: value` line addressed by a mapping path, e.g.
    ("resources", "requests", "cpu") — needed when the leaf key alone is
    ambiguous (requests.cpu vs limits.cpu).

    Block-style mappings only; comment and blank lines never terminate a
    block. The first path element must sit at top level (indent 0). Returns
    (content, replaced) — an absent path leaves the content untouched
    (no inserts).
    """
    lines = content.splitlines(keepends=True)

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    start, end = 0, len(lines)
    parent_indent = None
    target = None

    for key in path:
        target = None
        for i in range(start, end):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                continue
            ind = indent_of(lines[i])
            if parent_indent is None and ind != 0:
                continue  # top-level element must sit at indent 0
            if parent_indent is not None and ind <= parent_indent:
                break  # left the parent's block
            if re.match(rf'^{re.escape(key)}[ \t]*:', stripped):
                target = i
                break
        if target is None:
            return content, False

        # narrow the window to this key's block for the next path element
        parent_indent = indent_of(lines[target])
        start = target + 1
        new_end = end
        for j in range(start, end):
            s = lines[j].strip()
            if not s or s.startswith("#"):
                continue
            if indent_of(lines[j]) <= parent_indent:
                new_end = j
                break
        end = new_end

    line = lines[target]
    m = re.match(
        rf'^([ \t]*{re.escape(path[-1])}[ \t]*:[ \t]*)[^\n]*(\n?)$', line
    )
    if not m:
        return content, False
    lines[target] = m.group(1) + value + m.group(2)
    return "".join(lines), True


def replace_trigger_branch(content: str, branch: str) -> Tuple[str, bool]:
    """Replace the first list item directly under `branches:`.

    Only the first entry is devlift-managed; branches added by hand below it
    are left untouched. Returns (content, replaced).
    """
    pattern = r'(branches:[ \t]*\n[ \t]*-[ \t]*)[^\n]*'
    new_content, count = re.subn(
        pattern, lambda m: m.group(1) + branch, content, count=1
    )
    return new_content, bool(count)


#: `--build-arg` names the TEMPLATES own, never the user's Build Arguments form.
#: java-gradle / java-maven hard-code JAR_FILE and PROFILE; the Go path adds
#: AWS_SECRETS_MANAGER_NAME and CONFIG_ENV when go_use_aws_secrets is on. They
#: share the one `docker build` command with the custom args, so a rewrite that
#: could not tell them apart would silently break every Java and Go build.
TEMPLATE_OWNED_BUILD_ARGS = frozenset({
    "JAR_FILE", "PROFILE",
    "AWS_SECRETS_MANAGER_NAME", "CONFIG_ENV",
})

_BUILD_ARG_LINE_RE = re.compile(r'^[ \t]*--build-arg[ \t]+')
_BUILD_ARG_NAME_RE = re.compile(r'--build-arg[ \t]+([A-Za-z_][A-Za-z0-9_]*)[=\s]')


def replace_custom_build_args(content: str, new_block: str) -> Tuple[str, bool]:
    """Rewrite the user's `--build-arg` lines inside the `docker build` command.

    The create path renders that whole block from config; this is its update
    half, so editing Build Arguments on a service whose workflow already exists
    reaches the file instead of producing a no-op commit ("no diff detected").

    Ownership is by ARGUMENT NAME, the same discipline _remove_trigger_paths
    uses: only lines whose name is outside TEMPLATE_OWNED_BUILD_ARGS are
    touched, so JAR_FILE / PROFILE / the Go secrets pair survive untouched, and
    so does anything else in the command (-t tags, -f flag, the context dot).

    `new_block` is _build_custom_build_args' output: zero or more
    "            --build-arg NAME=\"value\" \\" lines, newline-terminated.
    Empty means the user removed them all, which is a real change and clears
    the lines rather than leaving them.

    The command is delimited by shell line-continuations: it runs from the
    `docker build` line to the first line that does NOT end in a backslash.
    New lines are inserted immediately before that terminator, which is where
    both template shapes put them (go.yml ends with a bare "."; the others end
    with the -t/context line).

    Returns (content, changed).
    """
    lines = content.splitlines(keepends=True)

    start = next(
        (i for i, ln in enumerate(lines) if re.search(r'\bdocker build\b', ln)),
        None,
    )
    if start is None:
        return content, False

    # End of the shell command: the first line that does not continue.
    end = start
    while end < len(lines) - 1 and lines[end].rstrip("\n").rstrip().endswith("\\"):
        end += 1
    if end == start:
        # Single-line `docker build ... .` — no continuation to insert into.
        return content, False

    body = lines[start + 1:end]          # candidates, excluding the terminator

    def _ours(ln: str) -> bool:
        """A --build-arg line devlift may rewrite.

        Every name on the line is inspected, not just the first: a hand-packed
        `--build-arg USER=x --build-arg JAR_FILE=a.jar \\` must be KEPT, since
        dropping it would take the template's JAR_FILE with it. When in doubt
        the line stays — a stale user arg is recoverable, a broken Java build
        is not. A line with no parseable name (e.g. the `--build-arg=K=v`
        spelling) is likewise left alone.
        """
        if not _BUILD_ARG_LINE_RE.match(ln):
            return False
        names = _BUILD_ARG_NAME_RE.findall(ln)
        return bool(names) and not any(n in TEMPLATE_OWNED_BUILD_ARGS for n in names)

    kept = [ln for ln in body if not _ours(ln)]

    # Each added line must end in a newline or the last one runs into the
    # terminator and yields "\\            -t ..." — a broken continuation.
    added = [
        ln if ln.endswith("\n") else ln + "\n"
        for ln in (new_block.splitlines() if new_block else [])
        if ln.strip()
    ]
    rebuilt = lines[:start + 1] + kept + added + lines[end:]

    new_content = "".join(rebuilt)
    return new_content, new_content != content


# ─────────────────────────── trigger paths ───────────────────────────

# Dot-directories the org's repos actually carry. A leading-dot name is a
# file far more often than a folder (.dockerignore, .gitignore, .env, .npmrc,
# the *rc family), so the folders are the exception list.
_DOT_DIRECTORIES = {
    ".github", ".circleci", ".gitlab", ".vscode", ".idea", ".devcontainer",
    ".husky", ".mvn", ".gradle", ".terraform",
}

# Files without an extension that still name a file, not a folder.
_EXTENSIONLESS_FILES = {
    "dockerfile", "makefile", "jenkinsfile", "procfile", "license", "readme",
    "changelog", "codeowners", "notice",
}


def clean_trigger_path(p) -> "str | None":
    """A path as an on.push.paths pattern would spell it: trailing slashes and
    any leading ./ stripped — GitHub matches repo-relative paths with NO ./
    normalization, so './cmd/**' never fires. '.' / './' mean the repo root,
    which as a filter means "every push", the same as no filter: None."""
    p = (p or "").strip().rstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return None if p in ("", ".") else p


def trigger_path_pattern(p) -> "str | None":
    """An other_path as the on.push.paths entry the generators write for it.

    A folder gets `/**` — that is what "additional trigger path" has always
    meant, and what people type in the UI: `shared`, `docker/api-server`.

    Not everything is a folder. The sync tool records what a hand-written
    workflow already triggers on, and that includes files (`.dockerignore`,
    `requirements.txt`, `.github/workflows/deploy.yml`) and globs (`*.py`).
    Gluing `/**` onto those produced `.dockerignore/**`, an entry that matches
    nothing, appended beside the `.dockerignore` the file already carried. So
    a glob is written as it is, and so is anything that names a file: an
    extension, a leading dot outside the known dot-directories, or one of the
    conventional extensionless names (Dockerfile, Makefile, …).
    """
    clean = clean_trigger_path(p)
    if clean is None:
        return None
    if "*" in clean:
        return clean
    last = clean.rsplit("/", 1)[-1]
    lower = last.lower()
    if lower in _EXTENSIONLESS_FILES or lower.startswith("dockerfile"):
        return clean
    if last.startswith("."):
        return f"{clean}/**" if lower in _DOT_DIRECTORIES else clean
    if "." in last:
        return clean
    return f"{clean}/**"
