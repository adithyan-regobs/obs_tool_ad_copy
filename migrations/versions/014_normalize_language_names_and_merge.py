"""Normalize language names and merge truly duplicate entries

Revision ID: 014_normalize_merge
Revises: 013_merge_duplicates
Create Date: 2025-11-05 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '014_normalize_merge'
down_revision: Union[str, None] = '013_merge_duplicates'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Normalize language names and merge entries with same language family and version.

    Examples:
    - "Java 17" and "Java 17 LTS" → both become "Java 17 LTS"
    - "Node.js 20" and "Node.js 20 LTS" → both become "Node.js 20 LTS"
    - Keep the more descriptive name (with LTS if available)
    """

    op.execute("""
        -- Step 1: Extract base language and version for grouping
        CREATE TEMP TABLE language_grouping AS
        SELECT
            id,
            code,
            name,
            version,
            yaml_templates,
            -- Extract base language name (first word(s) before version number)
            TRIM(REGEXP_REPLACE(name, E'\\s*\\d+.*$', '')) as base_language,
            -- Extract version from name if present
            REGEXP_REPLACE(name, E'^[^0-9]*(\\d+[.\\d]*).*$', '\\1') as name_version,
            is_active
        FROM language_ref
        WHERE is_active = true;

        -- Step 2: Find duplicates based on base_language + version
        CREATE TEMP TABLE language_duplicates AS
        SELECT
            base_language,
            version,
            COUNT(*) as dup_count,
            -- Keep the entry with "LTS" in name if available, otherwise shortest code
            (ARRAY_AGG(
                id ORDER BY
                CASE WHEN name LIKE '%LTS%' THEN 0 ELSE 1 END,
                LENGTH(code),
                id
            ))[1] as keep_id,
            -- Collect all IDs to delete
            ARRAY_AGG(id) as all_ids,
            -- Pick the best name (prefer LTS if available)
            (ARRAY_AGG(
                name ORDER BY
                CASE WHEN name LIKE '%LTS%' THEN 0 ELSE 1 END,
                LENGTH(name) DESC,
                name
            ))[1] as best_name,
            -- Merge all yaml_templates
            (
                SELECT jsonb_object_agg(key, value)
                FROM (
                    SELECT DISTINCT key, value
                    FROM language_grouping lg2,
                    LATERAL jsonb_each_text(lg2.yaml_templates)
                    WHERE lg2.base_language = lg.base_language
                    AND lg2.version = lg.version
                ) merged
            ) as merged_templates
        FROM language_grouping lg
        GROUP BY base_language, version
        HAVING COUNT(*) > 1;

        -- Step 3: Update the kept records with best name and merged templates
        UPDATE language_ref lr
        SET
            name = ld.best_name,
            yaml_templates = ld.merged_templates
        FROM language_duplicates ld
        WHERE lr.id = ld.keep_id;

        -- Step 4: Update pipeline_mst references to point to kept records
        UPDATE pipeline_mst pm
        SET language_ref_code = (
            SELECT lr.code
            FROM language_ref lr
            JOIN language_duplicates ld ON lr.id = ld.keep_id
            JOIN language_grouping lg_old ON lg_old.id = ANY(ld.all_ids)
            WHERE lg_old.code = pm.language_ref_code
            LIMIT 1
        )
        WHERE language_ref_code IN (
            SELECT lg.code
            FROM language_grouping lg
            JOIN language_duplicates ld ON (
                lg.base_language = ld.base_language
                AND lg.version = ld.version
                AND lg.id != ld.keep_id
            )
        );

        -- Step 5: Delete duplicate records
        DELETE FROM language_ref
        WHERE id IN (
            SELECT UNNEST(all_ids)
            FROM language_duplicates
        )
        AND id NOT IN (
            SELECT keep_id
            FROM language_duplicates
        );

        -- Step 6: Show summary
        DO $$
        DECLARE
            deleted_count INTEGER;
            remaining_count INTEGER;
        BEGIN
            GET DIAGNOSTICS deleted_count = ROW_COUNT;
            SELECT COUNT(*) INTO remaining_count FROM language_ref WHERE is_active = true;
            RAISE NOTICE 'Deleted % duplicate language entries', deleted_count;
            RAISE NOTICE 'Remaining unique language entries: %', remaining_count;
        END $$;
    """)


def downgrade() -> None:
    """
    Cannot automatically split merged entries back.
    Manual intervention required if rollback needed.
    """
    pass
