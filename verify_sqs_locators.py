#!/usr/bin/env python3
"""
Verify devlift `infrastructure_mst` SQS locator rows against the Terragrunt
source of truth in infrastructure-v2.

Read-only. Never writes to the DB.

For every queue stack under
  environment/core-prod-01/eu-west-2/queues/*/terragrunt.hcl
it derives the expected AWS queue name / ARN / URL (mirroring layers/queue
local.name + terraform-aws-modules/sqs/aws), then compares against the DB row
whose locator->>'identifier' matches.

WARNING: the nexus devlift DB credentials are hardcoded in HARDCODED_DB below.
Do not commit this file. Requires VPN.

Usage:
  # uses the hardcoded nexus credentials
  python verify_sqs_locators.py --json report.json

  # override any part of it
  python verify_sqs_locators.py --db-host H --db-user U --db-name D --json report.json

  # or a full DSN
  python verify_sqs_locators.py --db-url postgresql://user:pass@host:5432/dbname

  # or an env file with DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME
  python verify_sqs_locators.py --env-file ~/.devlift-prod.env

  # no DB at all: emit a standalone SQL check to paste into psql/DBeaver
  python verify_sqs_locators.py --emit-sql > check.sql

Other flags:
  --only <stack-dir>   limit to one or more stacks (repeatable)
  --show-ok            also print the clean queues
  --include-deleted    do not skip is_deleted rows

Exit code is 1 if any queue is MISSING or FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit(
        "psycopg2 not installed.\n"
        "Run with the obs_tool venv:\n"
        "  /Users/adhi/Documents/devlift/obs_tool/venv/bin/python "
        + __file__
    )

# ---------------------------------------------------------------- defaults ---

INFRA_REPO = Path("/Users/adhi/Documents/Aspora/infrastructure-v2")
QUEUES_DIR = INFRA_REPO / "environment/core-prod-01/eu-west-2/queues"
ENV_HCL = INFRA_REPO / "environment/core-prod-01/eu-west-2/env.hcl"

INFRA_TYPE = "sqs_infrastructuretype_ref"

# Nexus devlift Aurora cluster. Plaintext on purpose, at the operator's request.
# DO NOT COMMIT THIS FILE. The password below is 28 chars; the backslash that
# appears in the .env copy (\$0) is a Next.js env-loader escape, not part of the
# secret -- Python does no interpolation, so the literal belongs here unescaped.
HARDCODED_DB = {
    "host": "vance-nexus-london-01-devlift-db.cluster-c7m4ymwimvbv.eu-west-2.rds.amazonaws.com",
    "port": "5432",
    "user": "nexus_pg_admin",
    "password": "t#01UN-MoNado$0*qa8t3WX-D1DP",
    "dbname": "app_db",
}

# Tokens that must never appear inside a core-prod-01/eu-west-2 locator.
FOREIGN_TOKENS = [
    "ap-south-1", "us-east-1", "us-east-2", "eu-west-1", "eu-central-1",
    "mumbai", "ohio", "virginia", "vergenia", "verginia", "nvirginia",
    "stage", "staging", "-qa-", "sandbox", "trial", "demo", "scratch",
    "localhost", "example.com", "111111122222", "123456789012",
]

LOCATOR_STR_KEYS = [
    "region", "cloudRegion", "cloudRegionId", "accountId",
    "queue_name", "queue_arn", "queue_url",
    "dlq_name", "dlq_arn", "dlq_url",
    "identifier",
]

# Keys the devlift writer derives rather than taking from terragrunt.
DERIVED_LOCATOR_KEYS = {
    "region", "cloudRegion", "cloudRegionId", "accountId",
    "queue_name", "queue_arn", "queue_url", "dlq_name", "dlq_arn", "dlq_url",
}

# Scalar layer inputs worth cross-checking when the stack sets them explicitly.
CONFIG_KEYS = [
    "max_receive_count", "visibility_timeout_seconds", "message_retention_seconds",
    "delay_seconds", "max_message_size", "receive_wait_time_seconds",
    "dlq_delay_seconds", "dlq_visibility_timeout_seconds", "dlq_message_retention_seconds",
    "content_based_deduplication", "encryption", "enable_cross_account_access",
]

LAYER_VARS: set[str] = set()  # filled from layers/queue/variables.tf in main()

# --------------------------------------------------------------- hcl parse ---


def layer_variable_names(repo: Path) -> set[str]:
    """Every input layers/queue accepts. A locator key outside this set plus the
    derived keys is genuinely foreign, not just something we forgot to model."""
    vf = repo / "layers/queue/variables.tf"
    if not vf.is_file():
        return set()
    return set(re.findall(r'^variable\s+"([^"]+)"', vf.read_text(), re.M))


def _hcl_scalar(body: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", body, re.M)
    return m.group(1).strip() if m else None


def parse_env_hcl(path: Path) -> dict[str, str]:
    body = path.read_text()
    out = {}
    for key in ("organization", "env", "account_id", "region", "region_code", "index"):
        raw = _hcl_scalar(body, key)
        if raw is None:
            sys.exit(f"env.hcl missing `{key}`: {path}")
        out[key] = raw.strip('"')
    return out


def parse_queue_stack(tg_path: Path) -> dict[str, Any]:
    body = tg_path.read_text()
    dirname = tg_path.parent.name

    raw_ident = _hcl_scalar(body, "identifier")
    if raw_ident is None or "basename(get_terragrunt_dir())" in raw_ident:
        identifier = dirname
        ident_src = "basename"
    else:
        identifier = raw_ident.strip('"')
        ident_src = "literal"

    def flag(key: str, default: bool) -> bool:
        raw = _hcl_scalar(body, key)
        return default if raw is None else raw.strip().lower() == "true"

    def num(key: str) -> int | None:
        raw = _hcl_scalar(body, key)
        if raw is None:
            return None
        try:
            return int(raw.strip())
        except ValueError:
            return None

    config: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        raw = _hcl_scalar(body, key)
        if raw is None:
            continue
        low = raw.strip().lower()
        if low in ("true", "false"):
            config[key] = low == "true"
        else:
            n = num(key)
            if n is not None:
                config[key] = n

    return {
        "dir": dirname,
        "path": str(tg_path.relative_to(INFRA_REPO)),
        "identifier": identifier,
        "identifier_source": ident_src,
        "fifo_queue": flag("fifo_queue", False),
        "create_dlq": flag("create_dlq", False),
        "config": config,
    }


# ------------------------------------------------------------ name resolve ---
# Mirrors obs_tool app/domain/factories/infrastructure_mst_factory.py for the
# enterprise (non-PaaS) tenant path: layers/queue local.name.


def base_name(identifier: str, env: str, index: str) -> str:
    return f"{identifier}-{env}-{index}" if env == "prod" else f"{identifier}-{index}"


def expected_locator(stack: dict[str, Any], envc: dict[str, str]) -> dict[str, Any]:
    base = base_name(stack["identifier"], envc["env"], envc["index"])
    qname = f"{base}.fifo" if stack["fifo_queue"] else base
    region, acct = envc["region"], envc["account_id"]

    exp = {
        "region": region,
        "cloudRegion": region,
        "cloudRegionId": f"cr-{acct}-{region}",
        "accountId": acct,
        "identifier": stack["identifier"],
        "fifo_queue": stack["fifo_queue"],
        "create_dlq": stack["create_dlq"],
        "queue_name": qname,
        "queue_arn": f"arn:aws:sqs:{region}:{acct}:{qname}",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{acct}/{qname}",
    }
    if stack["create_dlq"]:
        dbase = f"{base}-dlq"
        dname = f"{dbase}.fifo" if stack["fifo_queue"] else dbase
        exp["dlq_name"] = dname
        exp["dlq_arn"] = f"arn:aws:sqs:{region}:{acct}:{dname}"
        exp["dlq_url"] = f"https://sqs.{region}.amazonaws.com/{acct}/{dname}"
    exp.update(stack["config"])
    return exp


# ----------------------------------------------------------------- checks ----


def split_arn(arn: str) -> tuple[str, str, str] | None:
    parts = arn.split(":")
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != "sqs":
        return None
    return parts[3], parts[4], parts[5]  # region, account, name


def split_url(url: str) -> tuple[str, str, str] | None:
    m = re.fullmatch(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com/(\d{12})/(.+)", url)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def check_row(stack, exp, loc, row, envc) -> list[dict[str, str]]:
    """Return a list of {severity, code, detail}."""
    issues: list[dict[str, str]] = []

    def fail(code, detail):
        issues.append({"severity": "FAIL", "code": code, "detail": detail})

    def warn(code, detail):
        issues.append({"severity": "WARN", "code": code, "detail": detail})

    # 1. field comparison. Identity fields (name/arn/url/account/region) are
    #    FAIL; tuneable config that drifted is only WARN.
    for key, want in exp.items():
        soft = key in CONFIG_KEYS
        bad = warn if soft else fail
        if key not in loc:
            bad("missing_field", f"{key}: absent (expected {want!r})")
            continue
        got = loc[key]
        if isinstance(want, bool):
            if bool(got) is not want:
                bad("flag_mismatch", f"{key}: db={got!r} tf={want!r}")
        elif isinstance(want, int):
            try:
                if int(got) != want:
                    bad("value_mismatch", f"{key}: db={got!r} tf={want!r}")
            except (TypeError, ValueError):
                fail("type_mismatch", f"{key}: db={got!r} not an int")
        else:
            if got != want:
                bad("value_mismatch", f"{key}: db={got!r} expected={want!r}")

    # 2. DLQ keys present when they should not be
    if not stack["create_dlq"]:
        for k in ("dlq_name", "dlq_arn", "dlq_url"):
            if loc.get(k):
                fail("unexpected_dlq", f"{k}={loc[k]!r} but create_dlq=false in terragrunt")

    # 3. internal ARN/URL consistency (catches a hand-patched row)
    for nk, ak, uk in (("queue_name", "queue_arn", "queue_url"),
                       ("dlq_name", "dlq_arn", "dlq_url")):
        name, arn, url = loc.get(nk), loc.get(ak), loc.get(uk)
        if arn:
            p = split_arn(str(arn))
            if not p:
                fail("malformed_arn", f"{ak}={arn!r}")
            else:
                r, a, n = p
                if r != envc["region"]:
                    fail("arn_wrong_region", f"{ak} region={r!r} expected={envc['region']!r}")
                if a != envc["account_id"]:
                    fail("arn_wrong_account", f"{ak} account={a!r} expected={envc['account_id']!r}")
                if name and n != name:
                    fail("arn_name_drift", f"{ak} name={n!r} != {nk}={name!r}")
        if url:
            p = split_url(str(url))
            if not p:
                fail("malformed_url", f"{uk}={url!r}")
            else:
                r, a, n = p
                if r != envc["region"]:
                    fail("url_wrong_region", f"{uk} region={r!r} expected={envc['region']!r}")
                if a != envc["account_id"]:
                    fail("url_wrong_account", f"{uk} account={a!r} expected={envc['account_id']!r}")
                if name and n != name:
                    fail("url_name_drift", f"{uk} name={n!r} != {nk}={name!r}")

    # 4. .fifo suffix rule
    for k in ("queue_name", "dlq_name"):
        v = loc.get(k)
        if not v:
            continue
        has = str(v).endswith(".fifo")
        if has != stack["fifo_queue"]:
            fail("fifo_suffix", f"{k}={v!r} but fifo_queue={stack['fifo_queue']} in terragrunt")

    # 5. junk / foreign tokens anywhere in the locator strings
    for k in LOCATOR_STR_KEYS:
        v = loc.get(k)
        if not isinstance(v, str):
            continue
        if v != v.strip():
            fail("whitespace", f"{k}={v!r} has leading/trailing whitespace")
        if re.search(r"[^\x20-\x7e]", v):
            fail("nonprintable", f"{k}={v!r} has non-printable characters")
        low = v.lower()
        for tok in FOREIGN_TOKENS:
            if tok in low and tok not in str(exp.get(k, "")).lower():
                fail("foreign_token", f"{k}={v!r} contains {tok!r}")
        if "--" in v:
            warn("double_dash", f"{k}={v!r}")
        if v.lower() != v:
            warn("uppercase", f"{k}={v!r} is not lowercase")

    # 6. locator keys that are neither a layer input nor a derived field
    for k in loc:
        if k in exp or k in DERIVED_LOCATOR_KEYS or k in LAYER_VARS:
            continue
        warn("extra_key", f"{k}={loc[k]!r} is not a layers/queue input")

    # 7. row-level sanity
    if row.get("is_deleted"):
        warn("soft_deleted", "row is_deleted=true")
    if row.get("is_active") is False:
        warn("inactive", "row is_active=false")
    if row.get("name") and row["name"] != exp["queue_name"]:
        warn("name_column_drift", f"name column={row['name']!r} != queue_name={exp['queue_name']!r}")

    return issues


# --------------------------------------------------------------------- db ---


def load_dotenv(path: Path) -> dict[str, str]:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def connect(args) -> tuple[Any, str]:
    """Return (connection, human-readable target). Read-only session."""
    read_only = "-c default_transaction_read_only=on"

    if args.db_url:
        conn = psycopg2.connect(args.db_url, connect_timeout=15, options=read_only)
        target = re.sub(r"//[^@]*@", "//***@", args.db_url)
        return conn, target

    env: dict[str, str] = {}
    if args.env_file:
        env.update(load_dotenv(args.env_file))
        if not env:
            sys.exit(f"no key=value pairs read from {args.env_file}")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("DB_", "PG"))})

    hc = {} if args.no_hardcoded else HARDCODED_DB
    host = args.db_host or env.get("DB_HOST") or env.get("PGHOST") or hc.get("host")
    port = args.db_port or env.get("DB_PORT") or env.get("PGPORT") or hc.get("port") or "5432"
    user = args.db_user or env.get("DB_USER") or env.get("PGUSER") or hc.get("user")
    name = args.db_name or env.get("DB_NAME") or env.get("PGDATABASE") or hc.get("dbname")
    pwd = args.db_password or env.get("DB_PASSWORD") or env.get("PGPASSWORD") or hc.get("password")

    missing = [n for n, v in (("host", host), ("user", user), ("dbname", name)) if not v]
    if missing:
        sys.exit(
            "missing DB settings: " + ", ".join(missing) + "\n\n"
            "Supply them one of these ways:\n"
            "  --db-url postgresql://user:pass@host:5432/dbname\n"
            "  --db-host H --db-user U --db-name D           (password prompted)\n"
            "  --env-file /path/to/.env                       (DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME)\n"
            "  export PGHOST=... PGUSER=... PGDATABASE=... PGPASSWORD=...\n"
        )
    if not pwd:
        import getpass
        pwd = getpass.getpass(f"password for {user}@{host}/{name}: ")

    try:
        conn = psycopg2.connect(
            host=host, port=int(port), user=user, password=pwd, dbname=name,
            connect_timeout=15, options=read_only,
        )
    except psycopg2.OperationalError as exc:
        msg = str(exc).strip()
        hint = ""
        if "password authentication failed" in msg:
            hint = (f"\nThe server was reachable, so this is the credential, not the network.\n"
                    f"Password used was {len(pwd)} chars for user {user!r}.\n")
        elif "timeout expired" in msg or "could not connect" in msg:
            hint = "\nServer unreachable -- check VPN.\n"
        sys.exit(f"database connection failed:\n{msg}{hint}")

    return conn, f"{user}@{host}:{port}/{name}"


PROBE_SQL = """
SELECT count(*) FILTER (WHERE infrastructuretype_ref_code = %(itype)s) AS sqs_rows,
       count(*) FILTER (WHERE infrastructuretype_ref_code = %(itype)s
                          AND environments_enum = 'prod') AS sqs_prod_rows,
       count(*) FILTER (WHERE infrastructuretype_ref_code = %(itype)s
                          AND environments_enum = 'prod'
                          AND locator->>'region' = %(region)s) AS sqs_prod_region_rows
FROM public.infrastructure_mst
"""


FETCH_SQL = """
SELECT id, code, name, locator, environments_enum::text AS environments_enum,
       applications_mst_code, is_deleted, is_active, status::text AS status,
       geo_loc_mst_code
FROM public.infrastructure_mst
WHERE infrastructuretype_ref_code = %(itype)s
  AND environments_enum = 'prod'
  AND locator->>'identifier' = ANY(%(idents)s)
ORDER BY locator->>'identifier', id
"""


# ---------------------------------------------------------------- sql mode ---


def emit_sql(stacks: list[dict], envc: dict[str, str]) -> str:
    """Standalone SQL: run in any psql/DBeaver session against the devlift DB."""
    def q(v):
        return "'" + str(v).replace("'", "''") + "'"

    rows = []
    for s in stacks:
        e = expected_locator(s, envc)
        rows.append(
            "    ({}, {}, {}, {}, {}, {}, {})".format(
                q(s["dir"]), q(s["identifier"]), q(e["queue_name"]),
                q(e["queue_arn"]), q(e["queue_url"]),
                str(s["create_dlq"]).lower(), str(s["fifo_queue"]).lower(),
            )
        )
    tokens = ", ".join(q(t) for t in FOREIGN_TOKENS)

    return f"""-- Read-only verification of infrastructure_mst SQS locators against
-- infrastructure-v2 environment/core-prod-01/eu-west-2/queues
-- expected values generated from terragrunt at {envc['region']} / account {envc['account_id']}
WITH expected(stack_dir, identifier, queue_name, queue_arn, queue_url, create_dlq, fifo_queue) AS (
  VALUES
{",\n".join(rows)}
),
exp AS (
  SELECT *,
         CASE WHEN create_dlq THEN
           CASE WHEN fifo_queue
                THEN left(queue_name, length(queue_name) - 5) || '-dlq.fifo'
                ELSE queue_name || '-dlq' END
         END AS dlq_name
  FROM expected
),
exp2 AS (
  SELECT *,
         CASE WHEN dlq_name IS NOT NULL
              THEN 'arn:aws:sqs:{envc['region']}:{envc['account_id']}:' || dlq_name END AS dlq_arn,
         CASE WHEN dlq_name IS NOT NULL
              THEN 'https://sqs.{envc['region']}.amazonaws.com/{envc['account_id']}/' || dlq_name END AS dlq_url
  FROM exp
),
db AS (
  SELECT code, name, applications_mst_code, is_deleted, is_active,
         locator->>'identifier' AS identifier,
         locator->>'region'     AS region,
         locator->>'accountId'  AS account_id,
         locator->>'cloudRegion'   AS cloud_region,
         locator->>'cloudRegionId' AS cloud_region_id,
         locator->>'queue_name' AS queue_name,
         locator->>'queue_arn'  AS queue_arn,
         locator->>'queue_url'  AS queue_url,
         locator->>'dlq_name'   AS dlq_name,
         locator->>'dlq_arn'    AS dlq_arn,
         locator->>'dlq_url'    AS dlq_url,
         (locator->>'create_dlq')::bool  AS create_dlq,
         (locator->>'fifo_queue')::bool  AS fifo_queue,
         locator
  FROM public.infrastructure_mst
  WHERE infrastructuretype_ref_code = 'sqs_infrastructuretype_ref'
    AND environments_enum = 'prod'
    AND is_deleted = false
    AND locator->>'region' = '{envc['region']}'
)
SELECT e.stack_dir,
       d.code,
       d.applications_mst_code,
       e.queue_name AS expected_queue_name,
       d.queue_name AS db_queue_name,
       array_remove(ARRAY[
         CASE WHEN d.code IS NULL                       THEN 'no_row' END,
         CASE WHEN d.queue_name IS DISTINCT FROM e.queue_name THEN 'queue_name' END,
         CASE WHEN d.queue_arn  IS DISTINCT FROM e.queue_arn  THEN 'queue_arn'  END,
         CASE WHEN d.queue_url  IS DISTINCT FROM e.queue_url  THEN 'queue_url'  END,
         CASE WHEN d.dlq_name   IS DISTINCT FROM e2.dlq_name  THEN 'dlq_name'   END,
         CASE WHEN d.dlq_arn    IS DISTINCT FROM e2.dlq_arn   THEN 'dlq_arn'    END,
         CASE WHEN d.dlq_url    IS DISTINCT FROM e2.dlq_url   THEN 'dlq_url'    END,
         CASE WHEN d.create_dlq IS DISTINCT FROM e.create_dlq THEN 'create_dlq_flag' END,
         CASE WHEN d.fifo_queue IS DISTINCT FROM e.fifo_queue THEN 'fifo_flag'  END,
         CASE WHEN d.account_id IS DISTINCT FROM '{envc['account_id']}' THEN 'accountId' END,
         CASE WHEN d.cloud_region IS DISTINCT FROM '{envc['region']}'   THEN 'cloudRegion' END,
         CASE WHEN d.cloud_region_id IS DISTINCT FROM 'cr-{envc['account_id']}-{envc['region']}'
              THEN 'cloudRegionId' END,
         CASE WHEN d.queue_arn IS NOT NULL
               AND d.queue_arn <> 'arn:aws:sqs:{envc['region']}:{envc['account_id']}:' || d.queue_name
              THEN 'arn_not_derived_from_name' END,
         CASE WHEN d.queue_url IS NOT NULL
               AND d.queue_url <> 'https://sqs.{envc['region']}.amazonaws.com/{envc['account_id']}/' || d.queue_name
              THEN 'url_not_derived_from_name' END,
         CASE WHEN d.queue_name ~ '\\s' OR d.queue_arn ~ '\\s' OR d.queue_url ~ '\\s'
              THEN 'whitespace' END,
         CASE WHEN EXISTS (
                SELECT 1 FROM unnest(ARRAY[{tokens}]) t
                WHERE lower(d.locator::text) LIKE '%' || t || '%'
              ) THEN 'foreign_token' END
       ], NULL) AS problems
FROM exp2 e2
JOIN exp e USING (stack_dir)
LEFT JOIN db d ON d.identifier = e.identifier
WHERE d.code IS NULL
   OR d.queue_name IS DISTINCT FROM e.queue_name
   OR d.queue_arn  IS DISTINCT FROM e.queue_arn
   OR d.queue_url  IS DISTINCT FROM e.queue_url
   OR d.dlq_name   IS DISTINCT FROM e2.dlq_name
   OR d.dlq_arn    IS DISTINCT FROM e2.dlq_arn
   OR d.dlq_url    IS DISTINCT FROM e2.dlq_url
   OR d.create_dlq IS DISTINCT FROM e.create_dlq
   OR d.fifo_queue IS DISTINCT FROM e.fifo_queue
   OR d.account_id IS DISTINCT FROM '{envc['account_id']}'
   OR d.cloud_region_id IS DISTINCT FROM 'cr-{envc['account_id']}-{envc['region']}'
   OR EXISTS (SELECT 1 FROM unnest(ARRAY[{tokens}]) t
              WHERE lower(d.locator::text) LIKE '%' || t || '%')
ORDER BY e.stack_dir;
"""


# ------------------------------------------------------------------- main ---


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queues-dir", type=Path, default=QUEUES_DIR)
    ap.add_argument("--env-hcl", type=Path, default=ENV_HCL)
    ap.add_argument("--only", action="append", help="limit to these stack dir names (repeatable)")
    ap.add_argument("--json", type=Path, help="write full report JSON here")
    ap.add_argument("--show-ok", action="store_true", help="also print clean queues")
    ap.add_argument("--include-deleted", action="store_true", help="do not skip is_deleted rows")
    ap.add_argument("--emit-sql", action="store_true",
                    help="print a standalone read-only SQL check (no DB connection needed) and exit")

    db = ap.add_argument_group("database (read-only)")
    db.add_argument("--db-url", help="postgresql://user:pass@host:5432/dbname")
    db.add_argument("--env-file", type=Path, help=".env with DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME")
    db.add_argument("--db-host")
    db.add_argument("--db-port")
    db.add_argument("--db-user")
    db.add_argument("--db-password", help="omit to use the hardcoded one, else prompted")
    db.add_argument("--db-name")
    db.add_argument("--no-hardcoded", action="store_true",
                    help="ignore the built-in nexus credentials")
    args = ap.parse_args()

    envc = parse_env_hcl(args.env_hcl)

    global LAYER_VARS
    LAYER_VARS = layer_variable_names(INFRA_REPO)

    stacks = [
        parse_queue_stack(p)
        for p in sorted(args.queues_dir.glob("*/terragrunt.hcl"))
    ]
    total_stacks = len(stacks)
    if args.only:
        wanted = set(args.only)
        stacks = [s for s in stacks if s["dir"] in wanted]
    if not stacks:
        sys.exit(f"no queue stacks found under {args.queues_dir}")

    by_ident: dict[str, list[dict]] = {}
    for s in stacks:
        by_ident.setdefault(s["identifier"], []).append(s)

    dupes = {k: [s["dir"] for s in v] for k, v in by_ident.items() if len(v) > 1}

    if args.emit_sql:
        print(emit_sql(stacks, envc))
        return 0

    conn, target = connect(args)
    with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT current_database() AS db, current_user AS usr")
        who = dict(cur.fetchone())
        cur.execute(PROBE_SQL, {"itype": INFRA_TYPE, "region": envc["region"]})
        probe = dict(cur.fetchone())
        cur.execute(FETCH_SQL, {"itype": INFRA_TYPE, "idents": list(by_ident)})
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    rows_by_ident: dict[str, list[dict]] = {}
    for r in rows:
        loc = r.get("locator") or {}
        rows_by_ident.setdefault(loc.get("identifier"), []).append(r)

    results = []
    for stack in stacks:
        exp = expected_locator(stack, envc)
        ident = stack["identifier"]
        cands = rows_by_ident.get(ident, [])
        if not args.include_deleted:
            cands = [r for r in cands if not r.get("is_deleted")]

        # A prod identifier can legitimately exist in another region too.
        in_region = [r for r in cands if (r.get("locator") or {}).get("region") == envc["region"]]
        other_region = [r for r in cands if r not in in_region]

        res = {
            "dir": stack["dir"],
            "identifier": ident,
            "path": stack["path"],
            "expected": exp,
            "status": "OK",
            "issues": [],
            "db_rows": len(in_region),
            "other_region_rows": [
                {"code": r["code"], "region": (r.get("locator") or {}).get("region")}
                for r in other_region
            ],
        }

        if not in_region:
            res["status"] = "MISSING"
            res["issues"].append({
                "severity": "FAIL", "code": "no_row",
                "detail": f"no prod/{envc['region']} sqs row with locator->>'identifier'={ident!r}",
            })
        else:
            if len(in_region) > 1:
                res["issues"].append({
                    "severity": "FAIL", "code": "duplicate_rows",
                    "detail": "codes=" + ", ".join(r["code"] for r in in_region),
                })
            for r in in_region:
                for iss in check_row(stack, exp, r.get("locator") or {}, r, envc):
                    res["issues"].append({**iss, "row_code": r["code"]})
            res["row_codes"] = [r["code"] for r in in_region]
            res["applications"] = sorted({r["applications_mst_code"] for r in in_region})
            res["actual"] = in_region[0].get("locator")

        if res["status"] == "OK":
            sev = {i["severity"] for i in res["issues"]}
            res["status"] = "FAIL" if "FAIL" in sev else ("WARN" if "WARN" in sev else "OK")
        results.append(res)

    # ------------------------------------------------------------- output ---
    order = {"MISSING": 0, "FAIL": 1, "WARN": 2, "OK": 3}
    bad = [r for r in results if r["status"] != "OK"]

    print(f"\ndatabase   : {target}  (db={who['db']} user={who['usr']}, read-only session)")
    print(f"queues dir : {args.queues_dir}")
    print(f"tenant     : {envc['organization']}  env={envc['env']}  "
          f"region={envc['region']}  index={envc['index']}  account={envc['account_id']}")
    print(f"db content : sqs={probe['sqs_rows']}  sqs+prod={probe['sqs_prod_rows']}  "
          f"sqs+prod+{envc['region']}={probe['sqs_prod_region_rows']}")
    print(f"stacks     : {len(stacks)}   db rows matched: {len(rows)}\n")

    if probe["sqs_prod_region_rows"] < total_stacks / 2:
        print(f"!! Only {probe['sqs_prod_region_rows']} prod/{envc['region']} SQS rows exist in this")
        print(f"!! database, but {total_stacks} queue stacks are defined. This is very likely the")
        print("!! WRONG database (a dev/sandbox devlift instance). Check the connection before")
        print("!! acting on anything below.\n")

    if dupes:
        print("Duplicate identifiers across stack dirs (expected 1:1):")
        for k, v in dupes.items():
            print(f"  {k}: {', '.join(v)}")
        print()

    for r in sorted(results, key=lambda x: (order[x["status"]], x["dir"])):
        if r["status"] == "OK" and not args.show_ok:
            continue
        mark = {"OK": "ok  ", "WARN": "warn", "FAIL": "FAIL", "MISSING": "MISS"}[r["status"]]
        print(f"[{mark}] {r['dir']}")
        print(f"        identifier : {r['identifier']}")
        print(f"        expected   : {r['expected']['queue_name']}")
        if r.get("row_codes"):
            print(f"        row(s)     : {', '.join(r['row_codes'])}")
        if r.get("applications"):
            print(f"        app(s)     : {', '.join(r['applications'])}")
        for iss in r["issues"]:
            print(f"          - [{iss['severity']}] {iss['code']}: {iss['detail']}")
        if r["other_region_rows"]:
            extra = ", ".join(f"{o['code']}({o['region']})" for o in r["other_region_rows"])
            print(f"        also in    : {extra}")
        print()

    counts = {s: sum(1 for r in results if r["status"] == s) for s in order}
    print("-" * 60)
    print(f"OK={counts['OK']}  WARN={counts['WARN']}  FAIL={counts['FAIL']}  MISSING={counts['MISSING']}")

    if args.json:
        args.json.write_text(json.dumps(
            {"env": envc, "duplicate_identifiers": dupes, "results": results},
            indent=2, default=str,
        ))
        print(f"report: {args.json}")

    return 1 if any(r["status"] in ("FAIL", "MISSING") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

