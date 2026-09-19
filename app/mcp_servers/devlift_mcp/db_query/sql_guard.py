"""Validate an LLM-written SELECT before it reaches Postgres.

The database has its own guards (SELECT-only role, READ ONLY transaction,
search_path pinned to mcp_ro, statement timeout). This module exists so a bad
query is refused with a message the LLM can act on, and so the application does
not rely on the role grants alone when a deployment runs without DB_RO_USER.

No dependency on a SQL parser: a small tokenizer that understands strings,
quoted identifiers, dollar quoting and comments, followed by checks on the
token stream. Anything the tokenizer cannot make sense of is rejected.
"""

import re
from dataclasses import dataclass


class SqlRejected(ValueError):
    """The query was refused before execution. str(exc) is LLM-facing."""


@dataclass(frozen=True)
class Token:
    kind: str  # word | qword | number | string | punct
    value: str
    pos: int


@dataclass(frozen=True)
class TableRef:
    parts: tuple[str, ...]
    pos: int

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)


@dataclass(frozen=True)
class GuardedSql:
    sql: str
    wrapped_sql: str
    tables: tuple[str, ...]
    ctes: tuple[str, ...]


# Statement types and commands that have no place in a read-only query. A
# READ ONLY transaction would refuse most of these anyway; rejecting up front
# gives the LLM a clear message instead of a database error.
FORBIDDEN_KEYWORDS = frozenset({
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "call", "do", "execute", "prepare", "deallocate",
    "lock", "vacuum", "analyze", "analyse", "reindex", "cluster", "listen",
    "notify", "unlisten", "set", "reset", "show", "begin", "commit", "rollback",
    "abort", "savepoint", "release", "declare", "move", "close", "refresh",
    "security", "comment", "explain", "import", "into", "load", "checkpoint",
    "discard", "disable", "enable",
})

# Functions that read server state, files or other databases, or that could
# change the tenant setting the views filter on.
FORBIDDEN_FUNCTIONS = frozenset({
    "set_config", "current_setting", "dblink", "dblink_connect",
    "dblink_connect_u", "dblink_exec", "dblink_open", "dblink_fetch",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_create", "lo_unlink",
    "query_to_xml", "query_to_xml_and_xmlschema", "cursor_to_xml",
    "database_to_xml", "database_to_xmlschema", "schema_to_xml", "table_to_xml",
    "inet_server_addr", "inet_server_port", "inet_client_addr",
    "inet_client_port", "current_query", "txid_current",
})

# Set-returning functions that are harmless as a FROM source.
FROM_FUNCTIONS = frozenset({
    "unnest", "generate_series", "generate_subscripts",
    "jsonb_array_elements", "jsonb_array_elements_text", "jsonb_each",
    "jsonb_each_text", "jsonb_object_keys", "jsonb_to_recordset",
    "jsonb_to_record", "json_array_elements", "json_array_elements_text",
    "json_each", "json_each_text", "json_object_keys",
    "regexp_split_to_table", "regexp_matches", "string_to_table",
})

# `FROM` inside these functions separates arguments; it does not start a table
# source. EXTRACT(YEAR FROM x), SUBSTRING(x FROM 2), TRIM(BOTH ' ' FROM x).
FROM_CONTEXT_FUNCTIONS = frozenset({"extract", "substring", "trim", "overlay", "position"})
EXTRACT_FIELDS = frozenset({
    "century", "day", "decade", "dow", "doy", "epoch", "hour", "isodow",
    "isoyear", "julian", "microseconds", "millennium", "milliseconds", "minute",
    "month", "quarter", "second", "timezone", "timezone_hour",
    "timezone_minute", "week", "year",
})

JOIN_WORDS = frozenset({"join", "left", "right", "inner", "outer", "full", "cross", "natural"})

# A word in this set directly after a table name is a clause, not an alias.
CLAUSE_WORDS = JOIN_WORDS | frozenset({
    "on", "using", "where", "group", "order", "limit", "offset", "union",
    "intersect", "except", "having", "window", "fetch", "for", "tablesample",
    "with", "returning", "lateral", "into", "values", "select", "from", "as",
    "and", "or", "not", "is", "in", "like", "ilike", "between", "case", "when",
    "then", "else", "end",
})

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER_RE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_DOLLAR_PARAM_RE = re.compile(r"\$\d+")


# ============================================================
# Tokenizer
# ============================================================


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]

        if c.isspace():
            i += 1
            continue

        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            continue

        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise SqlRejected("Unterminated comment")
            i = j
            continue

        if c == "'":
            j, _ = _read_quoted(sql, i, "'", backslash=False)
            tokens.append(Token("string", sql[i + 1:j - 1], i))
            i = j
            continue

        if c == '"':
            j, value = _read_quoted(sql, i, '"', backslash=False)
            tokens.append(Token("qword", value, i))
            i = j
            continue

        if c == "$":
            if _DOLLAR_PARAM_RE.match(sql, i):
                raise SqlRejected("Positional parameters ($1) are not supported; inline literal values")
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end == -1:
                    raise SqlRejected("Unterminated dollar-quoted string")
                tokens.append(Token("string", sql[m.end():end], i))
                i = end + len(tag)
                continue
            tokens.append(Token("punct", c, i))
            i += 1
            continue

        m = _WORD_RE.match(sql, i)
        if m:
            word = m.group(0)
            # E'...' strings use backslash escapes; B'..' / X'..' do not.
            if len(word) == 1 and word.lower() in ("e", "b", "x") and i + 1 < n and sql[i + 1] == "'":
                j, _ = _read_quoted(sql, i + 1, "'", backslash=word.lower() == "e")
                tokens.append(Token("string", sql[i + 2:j - 1], i))
                i = j
                continue
            tokens.append(Token("word", word.lower(), i))
            i = m.end()
            continue

        m = _NUMBER_RE.match(sql, i)
        if m and (c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit())):
            tokens.append(Token("number", m.group(0), i))
            i = m.end()
            continue

        tokens.append(Token("punct", c, i))
        i += 1

    return tokens


def _read_quoted(sql: str, start: int, quote: str, *, backslash: bool) -> tuple[int, str]:
    """Return (index after closing quote, unescaped content)."""
    j = start + 1
    out: list[str] = []
    n = len(sql)
    while j < n:
        c = sql[j]
        if backslash and c == "\\" and j + 1 < n:
            out.append(sql[j + 1])
            j += 2
            continue
        if c == quote:
            if j + 1 < n and sql[j + 1] == quote:
                out.append(quote)
                j += 2
                continue
            return j + 1, "".join(out)
        out.append(c)
        j += 1
    kind = "string" if quote == "'" else "quoted identifier"
    raise SqlRejected(f"Unterminated {kind}")


# ============================================================
# Token helpers
# ============================================================


def _is_word(tokens: list[Token], i: int, value: str) -> bool:
    return i < len(tokens) and tokens[i].kind == "word" and tokens[i].value == value


def _is_punct(tokens: list[Token], i: int, value: str) -> bool:
    return i < len(tokens) and tokens[i].kind == "punct" and tokens[i].value == value


def _is_ident(tokens: list[Token], i: int) -> bool:
    return i < len(tokens) and tokens[i].kind in ("word", "qword")


def _matching_paren(tokens: list[Token], i: int) -> int:
    """tokens[i] is '('; return the index of its matching ')'."""
    depth = 0
    for j in range(i, len(tokens)):
        t = tokens[j]
        if t.kind == "punct" and t.value == "(":
            depth += 1
        elif t.kind == "punct" and t.value == ")":
            depth -= 1
            if depth == 0:
                return j
    raise SqlRejected("Unbalanced parentheses")


# ============================================================
# Structure checks
# ============================================================


def _collect_ctes(tokens: list[Token]) -> set[str]:
    """Names introduced by WITH ... AS (...), at any nesting level."""
    names: set[str] = set()
    i = 0
    while i < len(tokens):
        if _is_word(tokens, i, "with"):
            j = i + 1
            if _is_word(tokens, j, "recursive"):
                j += 1
            while _is_ident(tokens, j):
                name = tokens[j].value
                j += 1
                if _is_punct(tokens, j, "("):
                    j = _matching_paren(tokens, j) + 1
                if not _is_word(tokens, j, "as"):
                    break
                j += 1
                if _is_word(tokens, j, "not"):
                    j += 1
                if _is_word(tokens, j, "materialized"):
                    j += 1
                if not _is_punct(tokens, j, "("):
                    break
                names.add(name)
                j = _matching_paren(tokens, j) + 1
                if not _is_punct(tokens, j, ","):
                    break
                j += 1
        i += 1
    return names


def _from_is_argument_separator(tokens: list[Token], i: int, paren_context: list) -> bool:
    prev = tokens[i - 1] if i > 0 else None
    if prev is not None and prev.kind == "word" and (prev.value in EXTRACT_FIELDS or prev.value == "distinct"):
        return True
    return bool(paren_context) and paren_context[-1] in FROM_CONTEXT_FUNCTIONS


def _collect_table_refs(tokens: list[Token]) -> list[TableRef]:
    """Every name used as a table source, in FROM/JOIN at any depth."""
    refs: list[TableRef] = []
    paren_context: list = []  # word before each open paren, or None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "punct" and t.value == "(":
            prev = tokens[i - 1] if i > 0 else None
            paren_context.append(prev.value if prev is not None and prev.kind == "word" else None)
        elif t.kind == "punct" and t.value == ")":
            if not paren_context:
                raise SqlRejected("Unbalanced parentheses")
            paren_context.pop()
        elif t.kind == "word" and t.value in ("from", "join"):
            if not (t.value == "from" and _from_is_argument_separator(tokens, i, paren_context)):
                i = _parse_table_items(tokens, i + 1, refs)
                continue
        i += 1
    if paren_context:
        raise SqlRejected("Unbalanced parentheses")
    return refs


def _parse_table_items(tokens: list[Token], j: int, refs: list[TableRef]) -> int:
    """Consume the comma-separated table items starting at j; return the next index."""
    while True:
        if _is_word(tokens, j, "lateral"):
            j += 1
        if _is_word(tokens, j, "only"):
            j += 1

        if _is_punct(tokens, j, "("):
            k = _matching_paren(tokens, j)
            refs.extend(_collect_table_refs(tokens[j + 1:k]))
            j = k + 1
        elif _is_ident(tokens, j):
            parts = [tokens[j]]
            j += 1
            while _is_punct(tokens, j, ".") and _is_ident(tokens, j + 1):
                parts.append(tokens[j + 1])
                j += 2
            if _is_punct(tokens, j, "("):
                name = parts[-1].value
                if len(parts) != 1 or name not in FROM_FUNCTIONS:
                    raise SqlRejected(
                        f"'{'.'.join(p.value for p in parts)}(...)' cannot be used as a table source"
                    )
                k = _matching_paren(tokens, j)
                refs.extend(_collect_table_refs(tokens[j + 1:k]))
                j = k + 1
                if _is_word(tokens, j, "with") and _is_word(tokens, j + 1, "ordinality"):
                    j += 2
            else:
                refs.append(TableRef(parts=tuple(p.value for p in parts), pos=parts[0].pos))
        else:
            raise SqlRejected("Expected a view name after FROM / JOIN")

        # Optional alias, with or without AS, optionally with a column list.
        if _is_word(tokens, j, "as"):
            j += 1
            if not _is_ident(tokens, j):
                raise SqlRejected("Expected an alias after AS")
            j += 1
        elif _is_ident(tokens, j) and tokens[j].value not in CLAUSE_WORDS:
            j += 1
        if _is_punct(tokens, j, "("):
            j = _matching_paren(tokens, j) + 1

        # Join condition: skip to the next comma at this depth or the next clause.
        if _is_word(tokens, j, "on"):
            j = _skip_join_condition(tokens, j + 1, refs)
        elif _is_word(tokens, j, "using"):
            j += 1
            if _is_punct(tokens, j, "("):
                j = _matching_paren(tokens, j) + 1

        if _is_punct(tokens, j, ","):
            j += 1
            continue
        return j


_CONDITION_STOP_WORDS = JOIN_WORDS | frozenset({
    "where", "group", "order", "limit", "offset", "union", "intersect",
    "except", "having", "window", "fetch", "for", "returning",
})


def _skip_join_condition(tokens: list[Token], j: int, refs: list[TableRef]) -> int:
    while j < len(tokens):
        t = tokens[j]
        if t.kind == "punct" and t.value == "(":
            k = _matching_paren(tokens, j)
            refs.extend(_collect_table_refs(tokens[j + 1:k]))
            j = k + 1
            continue
        if t.kind == "punct" and t.value in (",", ")"):
            return j
        if t.kind == "word" and t.value in _CONDITION_STOP_WORDS:
            return j
        j += 1
    return j


# ============================================================
# Entry point
# ============================================================


def guard_sql(
    sql: str,
    *,
    allowed_tables: frozenset[str],
    schema: str,
    max_rows: int,
    denied_words: frozenset[str] = frozenset(),
) -> GuardedSql:
    """Validate `sql` and return it wrapped with a hard row cap.

    `allowed_tables` are the view names the query may read (unqualified or
    `schema`-qualified). `denied_words` are identifiers that must not appear
    anywhere, typically every base table name plus other schema names.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if "\x00" in sql:
        raise SqlRejected("Query contains a NUL byte")

    tokens = tokenize(sql)
    if not tokens:
        raise SqlRejected("Empty query")

    end = len(sql)
    if tokens[-1].kind == "punct" and tokens[-1].value == ";":
        end = tokens[-1].pos
        tokens = tokens[:-1]
        if not tokens:
            raise SqlRejected("Empty query")
    for t in tokens:
        if t.kind == "punct" and t.value == ";":
            raise SqlRejected("One statement per call; remove the extra ';'")

    first = tokens[0]
    if not (first.kind == "word" and first.value in ("select", "with")):
        raise SqlRejected("Only SELECT queries (optionally starting with WITH) are allowed")

    for t in tokens:
        if t.kind == "word":
            if t.value in FORBIDDEN_KEYWORDS:
                raise SqlRejected(f"'{t.value.upper()}' is not allowed; query_data is read-only SELECT only")
            if t.value in FORBIDDEN_FUNCTIONS:
                raise SqlRejected(f"'{t.value}()' is not allowed")
            if t.value.startswith("pg_") or t.value == "information_schema":
                raise SqlRejected("System catalogs (pg_*, information_schema) are not accessible")
        if t.kind in ("word", "qword"):
            candidates = {t.value, t.value.lower()}
            if candidates & denied_words:
                raise SqlRejected(
                    f"'{t.value}' is not a queryable view. Use describe_data_schema for the list of views."
                )

    ctes = _collect_ctes(tokens)
    refs = _collect_table_refs(tokens)

    seen: list[str] = []
    for ref in refs:
        parts = ref.parts
        if len(parts) == 1:
            ok = parts[0] in ctes or parts[0] in allowed_tables
        elif len(parts) == 2:
            ok = parts[0] == schema and parts[1] in allowed_tables
        else:
            ok = False
        if not ok:
            raise SqlRejected(
                f"'{ref.dotted}' is not a queryable view. Allowed: {', '.join(sorted(allowed_tables))}."
            )
        if ref.dotted not in seen:
            seen.append(ref.dotted)

    clean = sql[:end].strip()
    wrapped = f"SELECT * FROM (\n{clean}\n) AS _mcp_q LIMIT {max_rows + 1}"
    return GuardedSql(sql=clean, wrapped_sql=wrapped, tables=tuple(seen), ctes=tuple(sorted(ctes)))
