"""Final cleanup of remaining duplicates

Revision ID: 015_final_cleanup
Revises: 014_normalize_merge
Create Date: 2025-11-05 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '015_final_cleanup'
down_revision: Union[str, None] = '014_normalize_merge'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Final cleanup: Merge any remaining duplicates by comparing base names without LTS/Stable suffixes.
    Keep the entry with LTS/Stable in the name and shortest code.
    """

    op.execute("""
        -- Find and merge final duplicates
        WITH duplicate_groups AS (
            SELECT
                TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')) as base_name,
                version,
                -- Keep entry with LTS/Stable if available, then shortest code
                (ARRAY_AGG(
                    id ORDER BY
                    CASE
                        WHEN name LIKE '%LTS%' OR name LIKE '%Stable%' THEN 0
                        ELSE 1
                    END,
                    LENGTH(code),
                    id
                ))[1] as keep_id,
                ARRAY_AGG(id) as all_ids,
                -- Pick best name (prefer LTS/Stable)
                (ARRAY_AGG(
                    name ORDER BY
                    CASE
                        WHEN name LIKE '%LTS%' OR name LIKE '%Stable%' THEN 0
                        ELSE 1
                    END,
                    LENGTH(name) DESC
                ))[1] as best_name
            FROM language_ref
            WHERE is_active = true
            GROUP BY
                TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')),
                version
            HAVING COUNT(*) > 1
        ),
        merged_templates AS (
            SELECT
                dg.keep_id,
                dg.best_name,
                jsonb_object_agg(key, value) as merged_yaml
            FROM duplicate_groups dg
            JOIN language_ref lr ON lr.id = ANY(dg.all_ids)
            CROSS JOIN LATERAL jsonb_each_text(lr.yaml_templates)
            GROUP BY dg.keep_id, dg.best_name
        )
        -- Update kept records
        UPDATE language_ref lr
        SET
            name = mt.best_name,
            yaml_templates = mt.merged_yaml
        FROM merged_templates mt
        WHERE lr.id = mt.keep_id;

        -- Update pipeline_mst references
        WITH duplicate_groups AS (
            SELECT
                TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')) as base_name,
                version,
                (ARRAY_AGG(
                    id ORDER BY
                    CASE WHEN name LIKE '%LTS%' OR name LIKE '%Stable%' THEN 0 ELSE 1 END,
                    LENGTH(code),
                    id
                ))[1] as keep_id,
                ARRAY_AGG(id) as all_ids
            FROM language_ref
            WHERE is_active = true
            GROUP BY
                TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')),
                version
            HAVING COUNT(*) > 1
        )
        UPDATE pipeline_mst pm
        SET language_ref_code = (
            SELECT lr_keep.code
            FROM language_ref lr_keep
            WHERE lr_keep.id = (
                SELECT dg.keep_id
                FROM duplicate_groups dg
                JOIN language_ref lr_old ON lr_old.id = ANY(dg.all_ids)
                WHERE lr_old.code = pm.language_ref_code
                LIMIT 1
            )
        )
        WHERE language_ref_code IN (
            SELECT lr.code
            FROM language_ref lr
            JOIN duplicate_groups dg ON lr.id = ANY(dg.all_ids) AND lr.id != dg.keep_id
        );

        -- Delete duplicates
        WITH duplicate_groups AS (
            SELECT
                TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')) as base_name,
                version,
                (ARRAY_AGG(id ORDER BY CASE WHEN name LIKE '%LTS%' THEN 0 ELSE 1 END, LENGTH(code)))[1] as keep_id,
                ARRAY_AGG(id) as all_ids
            FROM language_ref
            WHERE is_active = true
            GROUP BY TRIM(REGEXP_REPLACE(name, ' LTS| Stable', '')), version
            HAVING COUNT(*) > 1
        )
        DELETE FROM language_ref
        WHERE id IN (
            SELECT UNNEST(all_ids)
            FROM duplicate_groups
        )
        AND id NOT IN (
            SELECT keep_id
            FROM duplicate_groups
        );
    """)


def downgrade() -> None:
    """Cannot revert merged data automatically"""
    pass
