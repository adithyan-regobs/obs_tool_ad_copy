"""Audit-trail archival: partition the 3 audit tables, archive to S3, 14-day retention

Revision ID: 160_audit_archival_partitioning
Revises: 159_add_update_variables_case_ref
Create Date: 2026-08-13

Rebuilds audit_actor / audit_event / audit_field_change as range-partitioned
tables (1-day partitions) managed by pg_partman, with a pg_cron-scheduled
pipeline that keeps only 14 days in the DB:

  daily 02:00 UTC  partman maintenance: premake partitions, DETACH expired ones
  daily 02:15 UTC  archive_audit_partitions(): export each detached partition
                   to s3://$ARCHIVE_BUCKET/<table>/dt=YYYY-MM-DD/<partition>.jsonl
                   via aws_s3.query_export_to_s3, then DROP it

DESTRUCTIVE: drops and recreates the 3 tables (approved - audit data is
disposable in every env at rollout time). Schema deltas vs the old tables:
  - audit_actor / audit_field_change gain created_at (their partition key)
  - PKs become composite with the partition key (Postgres requirement)
  - the two FKs pointing AT audit tables are gone (Postgres cannot FK to a
    partitioned table without including the partition key, and the FK would
    block the daily partition drops); FKs to user_mst / tenants_mst remain

Requires (already live on dev, Terraform-managed on prod):
  - shared_preload_libraries=pg_cron, cron.database_name=<this db>
  - an IAM role on the instance/cluster with the s3Export feature
  - env vars at migration time: ARCHIVE_BUCKET, ARCHIVE_REGION

Safe to run on a DB that already has the POC setup (5-minute retention):
unschedules the POC cron jobs, clears partman config and drops the POC
partitioned tables before recreating everything at the production cadence.
"""

import os

from alembic import op

revision = "160_audit_archival_partitioning"
down_revision = "159_add_update_variables_case_ref"
branch_labels = None
depends_on = None

AUDIT_TABLES = ["audit_field_change", "audit_event", "audit_actor"]
CRON_JOBS = ["audit-partman-maintenance", "audit-archive-s3"]


def _cleanup_existing() -> None:
    """Remove POC/previous partman + cron state so the migration is re-runnable."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                PERFORM cron.unschedule(jobid)
                FROM cron.job
                WHERE jobname IN ('audit-partman-maintenance', 'audit-archive-s3');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DELETE FROM partman.part_config
        WHERE parent_table IN ('public.audit_actor',
                               'public.audit_event',
                               'public.audit_field_change');
        """
    )
    for table in AUDIT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS partman.template_public_{table};")
        op.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE;")


def upgrade() -> None:
    bucket = os.environ.get("ARCHIVE_BUCKET")
    region = os.environ.get("ARCHIVE_REGION")
    if not bucket or not region:
        raise RuntimeError(
            "Migration 160_audit_archival_partitioning needs ARCHIVE_BUCKET and "
            "ARCHIVE_REGION in the environment (the S3 bucket the DB exports "
            "expired audit partitions to). Set them on the service/task "
            "environment and redeploy."
        )

    # 1) Extensions (pg_cron must be created in the cron.database_name DB,
    #    which is this DB in every env)
    op.execute("CREATE SCHEMA IF NOT EXISTS partman;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_cron;")
    op.execute("CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE;")

    # 2) Clear any POC-era jobs/config and drop the old tables
    _cleanup_existing()

    # 3) Recreate the 3 tables, range-partitioned
    op.execute(
        """
        CREATE TABLE audit_actor (
            actor_id   bigserial,
            user_id    bigint REFERENCES user_mst(id) ON DELETE SET NULL,
            username   varchar(100),
            role       varchar(100),
            ip_address inet,
            user_agent varchar(500),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (actor_id, created_at)
        ) PARTITION BY RANGE (created_at);
        """
    )
    op.execute("CREATE INDEX idx_audit_actor_user_id ON audit_actor (user_id);")
    op.execute("CREATE INDEX idx_audit_actor_username ON audit_actor (username);")
    op.execute("CREATE INDEX idx_audit_actor_ip ON audit_actor (ip_address);")

    op.execute(
        """
        CREATE TABLE audit_event (
            event_id         bigserial,
            event_time       timestamptz NOT NULL DEFAULT now(),
            event_name       audit_action_enum NOT NULL,
            event_source     varchar(255),
            actor_id         bigint NOT NULL,
            resource_type    varchar(255) NOT NULL,
            resource_id      varchar(255),
            status           varchar(50),
            correlation_id   varchar(100),
            tenants_mst_code varchar(100) NOT NULL
                             REFERENCES tenants_mst(code) ON DELETE CASCADE,
            request_payload  jsonb,
            response_payload jsonb,
            PRIMARY KEY (event_id, event_time)
        ) PARTITION BY RANGE (event_time);
        """
    )
    op.execute("CREATE INDEX idx_audit_event_time ON audit_event (event_time);")
    op.execute("CREATE INDEX idx_audit_event_name ON audit_event (event_name);")
    op.execute("CREATE INDEX idx_audit_event_actor ON audit_event (actor_id);")
    op.execute(
        "CREATE INDEX idx_audit_event_resource ON audit_event (resource_type, resource_id);"
    )
    op.execute("CREATE INDEX idx_audit_event_tenant ON audit_event (tenants_mst_code);")
    op.execute(
        "CREATE INDEX idx_audit_event_tenant_time ON audit_event (tenants_mst_code, event_time);"
    )
    op.execute(
        "CREATE INDEX idx_audit_event_correlation ON audit_event (correlation_id);"
    )
    op.execute("CREATE INDEX idx_audit_event_status ON audit_event (status);")

    op.execute(
        """
        CREATE TABLE audit_field_change (
            id         bigserial,
            event_id   bigint NOT NULL,
            field_name varchar(200) NOT NULL,
            old_value  text,
            new_value  text,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);
        """
    )
    op.execute(
        "CREATE INDEX idx_audit_field_change_event ON audit_field_change (event_id);"
    )
    op.execute(
        "CREATE INDEX idx_audit_field_change_field ON audit_field_change (field_name);"
    )

    # 4) pg_partman: daily partitions, 14-day retention, detach (not drop) so
    #    the archive job exports before anything is destroyed.
    #    p_type differs between pg_partman majors: v4 wants 'native', v5 'range'.
    op.execute(
        """
        DO $$
        DECLARE
            v_major int := split_part(
                (SELECT extversion FROM pg_extension WHERE extname = 'pg_partman'),
                '.', 1)::int;
            v_type  text := CASE WHEN v_major >= 5 THEN 'range' ELSE 'native' END;
            v_parent record;
        BEGIN
            FOR v_parent IN
                SELECT * FROM (VALUES
                    ('public.audit_actor',        'created_at'),
                    ('public.audit_event',        'event_time'),
                    ('public.audit_field_change', 'created_at')
                ) AS t(parent_table, control)
            LOOP
                PERFORM partman.create_parent(
                    p_parent_table := v_parent.parent_table,
                    p_control      := v_parent.control,
                    p_interval     := '1 day',
                    p_type         := v_type,
                    p_premake      := 4
                );
            END LOOP;
        END $$;
        """
    )
    op.execute(
        """
        UPDATE partman.part_config
        SET retention                = '14 days',
            retention_keep_table     = true,
            infinite_time_partitions = true
        WHERE parent_table IN ('public.audit_actor',
                               'public.audit_event',
                               'public.audit_field_change');
        """
    )

    # 5) Archive function: export every DETACHED audit partition to S3 as JSON
    #    Lines, then drop it. Children before parents (field_change -> event ->
    #    actor) so a partial run never leaves field_changes pointing at events
    #    that are gone from the DB. Empty partitions are dropped without an
    #    export (no 0-byte S3 objects). The advisory lock makes overlapping
    #    runs skip instead of double-exporting; export failures abort before
    #    the DROP, so data is never lost, and the next run retries.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.archive_audit_partitions(
            p_bucket text,
            p_region text
        )
        RETURNS TABLE(archived_table text, rows_exported bigint) AS $fn$
        DECLARE
            parent     text;
            t          text;
            dt_digits  text;
            dt         text;
            n_rows     bigint;
            n_uploaded bigint;
        BEGIN
            IF NOT pg_try_advisory_lock(hashtext('archive_audit_partitions')) THEN
                RETURN;
            END IF;

            FOREACH parent IN ARRAY
                ARRAY['audit_field_change', 'audit_event', 'audit_actor']
            LOOP
                FOR t IN
                    SELECT c.relname
                    FROM   pg_class c
                    JOIN   pg_namespace n ON n.oid = c.relnamespace
                    WHERE  n.nspname = 'public'
                      AND  c.relkind = 'r'
                      AND  c.relname LIKE parent || '_p%'
                      AND  c.relname NOT IN (
                             SELECT inhrelid::regclass::text
                             FROM   pg_inherits
                             WHERE  inhparent = ('public.' || parent)::regclass)
                    ORDER BY c.relname
                LOOP
                    EXECUTE format('SELECT count(*) FROM public.%I', t) INTO n_rows;
                    n_uploaded := 0;

                    IF n_rows > 0 THEN
                        -- partition-name suffix differs by pg_partman major
                        -- (v5: _p20260813, v4: _p2026_08_13); digits are the
                        -- stable part
                        dt_digits := regexp_replace(
                            substring(t from '_p(.*)$'), '\D', '', 'g');
                        IF length(dt_digits) >= 8 THEN
                            dt := to_char(
                                to_date(left(dt_digits, 8), 'YYYYMMDD'),
                                'YYYY-MM-DD');
                        ELSE
                            dt := to_char(now(), 'YYYY-MM-DD');
                        END IF;
                        SELECT rows_uploaded INTO n_uploaded
                        FROM aws_s3.query_export_to_s3(
                            format('SELECT row_to_json(x)::text FROM public.%I x', t),
                            aws_commons.create_s3_uri(
                                p_bucket,
                                parent || '/dt=' || dt || '/' || t || '.jsonl',
                                p_region),
                            options := 'format text');
                    END IF;

                    EXECUTE format('DROP TABLE public.%I', t);
                    archived_table := t;
                    rows_exported  := n_uploaded;
                    RETURN NEXT;
                END LOOP;
            END LOOP;

            PERFORM pg_advisory_unlock(hashtext('archive_audit_partitions'));
        END;
        $fn$ LANGUAGE plpgsql;
        """
    )

    # 6) Daily schedule (UTC). lock_timeout keeps a detach from queueing behind
    #    a stuck session for minutes (the POC stall); a failed tick just
    #    retries the next day and the archive job tolerates the backlog.
    #    run_maintenance() (function), not run_maintenance_proc(): the proc
    #    commits internally, which fails inside pg_cron's multi-statement
    #    implicit transaction once SET is prepended.
    op.execute(
        """
        SELECT cron.schedule(
            'audit-partman-maintenance',
            '0 2 * * *',
            $job$SET lock_timeout = '30s'; SELECT partman.run_maintenance();$job$
        );
        """
    )
    op.execute(
        f"""
        SELECT cron.schedule(
            'audit-archive-s3',
            '15 2 * * *',
            $job$SELECT public.archive_audit_partitions('{bucket}', '{region}');$job$
        );
        """
    )


def downgrade() -> None:
    """Back to plain (non-partitioned) audit tables. All audit rows are lost."""
    _cleanup_existing()
    op.execute("DROP FUNCTION IF EXISTS public.archive_audit_partitions(text, text);")

    # Original 021 shape with the later widenings (056/058) applied
    op.execute(
        """
        CREATE TABLE audit_actor (
            actor_id   bigserial PRIMARY KEY,
            user_id    bigint REFERENCES user_mst(id) ON DELETE SET NULL,
            username   varchar(100),
            role       varchar(100),
            ip_address inet,
            user_agent varchar(500)
        );
        """
    )
    op.execute("CREATE INDEX idx_audit_actor_user_id ON audit_actor (user_id);")
    op.execute("CREATE INDEX idx_audit_actor_username ON audit_actor (username);")
    op.execute("CREATE INDEX idx_audit_actor_ip ON audit_actor (ip_address);")

    op.execute(
        """
        CREATE TABLE audit_event (
            event_id         bigserial PRIMARY KEY,
            event_time       timestamptz NOT NULL DEFAULT now(),
            event_name       audit_action_enum NOT NULL,
            event_source     varchar(255),
            actor_id         bigint NOT NULL
                             REFERENCES audit_actor(actor_id) ON DELETE CASCADE,
            resource_type    varchar(255) NOT NULL,
            resource_id      varchar(255),
            status           varchar(50),
            correlation_id   varchar(100),
            tenants_mst_code varchar(100) NOT NULL
                             REFERENCES tenants_mst(code) ON DELETE CASCADE,
            request_payload  jsonb,
            response_payload jsonb
        );
        """
    )
    op.execute("CREATE INDEX idx_audit_event_time ON audit_event (event_time);")
    op.execute("CREATE INDEX idx_audit_event_name ON audit_event (event_name);")
    op.execute("CREATE INDEX idx_audit_event_actor ON audit_event (actor_id);")
    op.execute(
        "CREATE INDEX idx_audit_event_resource ON audit_event (resource_type, resource_id);"
    )
    op.execute("CREATE INDEX idx_audit_event_tenant ON audit_event (tenants_mst_code);")
    op.execute(
        "CREATE INDEX idx_audit_event_tenant_time ON audit_event (tenants_mst_code, event_time);"
    )
    op.execute(
        "CREATE INDEX idx_audit_event_correlation ON audit_event (correlation_id);"
    )
    op.execute("CREATE INDEX idx_audit_event_status ON audit_event (status);")

    op.execute(
        """
        CREATE TABLE audit_field_change (
            id         bigserial PRIMARY KEY,
            event_id   bigint NOT NULL
                       REFERENCES audit_event(event_id) ON DELETE CASCADE,
            field_name varchar(200) NOT NULL,
            old_value  text,
            new_value  text
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_audit_field_change_event ON audit_field_change (event_id);"
    )
    op.execute(
        "CREATE INDEX idx_audit_field_change_field ON audit_field_change (field_name);"
    )
