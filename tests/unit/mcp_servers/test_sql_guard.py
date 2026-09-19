"""sql_guard - the pre-database checks on LLM-written SELECTs.

Pure: no DB, no settings. `ALLOWED` stands in for the mcp_ro view catalog and
`DENIED` for the base-table names the executor derives from Base.metadata.
"""
import pytest

from app.mcp_servers.devlift_mcp.db_query.sql_guard import SqlRejected, guard_sql, tokenize

ALLOWED = frozenset({"services", "infrastructure", "service_configs", "applications", "tickets"})
DENIED = frozenset({"services_mst", "infrastructure_mst", "ticket", "user_mst", "public", "information_schema"})


def guard(sql, max_rows=100):
    return guard_sql(sql, allowed_tables=ALLOWED, schema="mcp_ro", max_rows=max_rows, denied_words=DENIED)


def rejects(sql, fragment):
    with pytest.raises(SqlRejected) as exc:
        guard(sql)
    assert fragment.lower() in str(exc.value).lower(), str(exc.value)


# ---------------------------------------------------------------- accepts

def test_simple_select_is_wrapped_with_row_cap():
    g = guard("SELECT name FROM services WHERE environment = 'stage'", max_rows=50)
    assert g.tables == ("services",)
    assert g.wrapped_sql.startswith("SELECT * FROM (")
    assert g.wrapped_sql.rstrip().endswith("LIMIT 51")
    assert g.sql == "SELECT name FROM services WHERE environment = 'stage'"


def test_trailing_semicolon_is_stripped():
    g = guard("SELECT name FROM services;")
    assert g.sql == "SELECT name FROM services"
    assert ";" not in g.wrapped_sql


def test_joins_aliases_and_schema_qualified_names():
    g = guard(
        "SELECT s.name, i.name AS cluster_name "
        "FROM mcp_ro.services AS s "
        "LEFT JOIN infrastructure i ON i.code = s.infrastructure_code "
        "JOIN service_configs sc USING (code)"
    )
    assert set(g.tables) == {"mcp_ro.services", "infrastructure", "service_configs"}


def test_cte_names_are_allowed_as_sources():
    g = guard(
        "WITH recent AS (SELECT * FROM services WHERE created_at > now() - interval '7 days'), "
        "counts(app, n) AS (SELECT application_name, count(*) FROM recent GROUP BY 1) "
        "SELECT * FROM counts ORDER BY n DESC"
    )
    assert g.ctes == ("counts", "recent")
    assert "services" in g.tables


def test_extract_and_substring_from_are_not_table_sources():
    guard("SELECT EXTRACT(YEAR FROM created_at) AS y, SUBSTRING(name FROM 1 FOR 3) FROM services")
    guard("SELECT TRIM(BOTH ' ' FROM name) FROM services WHERE status IS DISTINCT FROM 'FAILED'")


def test_subqueries_anywhere_are_checked():
    guard("SELECT name FROM services WHERE code IN (SELECT service_code FROM service_configs)")
    rejects("SELECT name FROM services WHERE code IN (SELECT code FROM ticket)", "ticket")


def test_set_returning_function_in_from_is_allowed():
    guard("SELECT e.value FROM infrastructure i, jsonb_array_elements(i.locator) AS e")
    guard("SELECT * FROM generate_series(1, 3) WITH ORDINALITY AS g(n, ord)")


def test_string_literals_and_comments_are_not_keywords():
    guard("SELECT name FROM services WHERE name = 'delete me' -- update later\n AND description <> 'DROP'")
    guard("SELECT $$insert$$ AS label, E'dele\\'te' AS x FROM services")


def test_quoted_identifiers_tokenize():
    g = guard('SELECT "name" FROM "services"')
    assert g.tables == ("services",)


# ---------------------------------------------------------------- rejects

@pytest.mark.parametrize("sql,fragment", [
    ("UPDATE services SET name = 'x'", "only select"),
    ("DELETE FROM services", "only select"),
    ("INSERT INTO services VALUES (1)", "only select"),
    ("EXPLAIN SELECT 1", "only select"),
    ("SELECT 1; DELETE FROM services", "one statement"),
    ("SELECT 1; -- \n DROP TABLE services", "one statement"),
    ("SELECT name INTO tmp FROM services", "'into'"),
    ("WITH x AS (DELETE FROM services RETURNING *) SELECT * FROM x", "'delete'"),
    ("SELECT * FROM services FOR UPDATE", "'update'"),
    ("SELECT set_config('app.tenant_code', 'other', true)", "set_config"),
    ("SELECT current_setting('app.tenant_code') FROM services", "current_setting"),
    ("SELECT pg_sleep(10) FROM services", "pg_*"),
    ("SELECT * FROM pg_catalog.pg_tables", "pg_*"),
    ("SELECT * FROM information_schema.tables", "pg_*"),
    ("SELECT * FROM public.services", "'public'"),
    ("SELECT * FROM services_mst", "'services_mst'"),
    ("SELECT * FROM services AS ticket", "'ticket'"),
    ('SELECT * FROM "ticket"', "'ticket'"),
    ("SELECT * FROM (SELECT 1) x, ticket t", "'ticket'"),
    ("SELECT * FROM services s JOIN infrastructure i ON i.code = s.code, ticket t", "'ticket'"),
    ("SELECT * FROM secrets", "not a queryable view"),
    ("SELECT * FROM other_schema.services", "not a queryable view"),
    ("SELECT * FROM unknown_fn(1)", "cannot be used as a table source"),
    ("SELECT name FROM services WHERE code = $1", "positional parameters"),
    ("SELECT 'unterminated FROM services", "unterminated string"),
    ("SELECT /* open comment FROM services", "unterminated comment"),
    ("SELECT (name FROM services", "unbalanced"),
    ("", "empty"),
    (";", "empty"),
    ("SELECT name FROM", "expected a view name"),
])
def test_rejected(sql, fragment):
    rejects(sql, fragment)


def test_nul_byte_rejected():
    rejects("SELECT 1\x00 FROM services", "nul")


def test_max_rows_must_be_positive():
    with pytest.raises(ValueError):
        guard("SELECT 1 FROM services", max_rows=0)


# ---------------------------------------------------------------- tokenizer

def test_tokenizer_kinds():
    toks = tokenize("SELECT a.b, 'it''s', 1.5e3, \"Quoted\" FROM x -- c\n/* d */")
    kinds = [(t.kind, t.value) for t in toks]
    assert kinds == [
        ("word", "select"), ("word", "a"), ("punct", "."), ("word", "b"), ("punct", ","),
        ("string", "it''s"), ("punct", ","), ("number", "1.5e3"), ("punct", ","),
        ("qword", "Quoted"), ("word", "from"), ("word", "x"),
    ]
