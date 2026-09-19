"""Merge duplicate language entries with different codes

Revision ID: 013_merge_duplicates
Revises: 012_yaml_templates_jsonb
Create Date: 2025-11-05 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '013_merge_duplicates'
down_revision: Union[str, None] = '012_yaml_templates_jsonb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Merge duplicate language entries that have the same name and version
    but different codes (e.g., PYTHON_3_12 and PYTHON_3_12_AWS_CODEPIPELINE).

    Strategy:
    1. Find all languages with same name+version
    2. Keep the shortest code (without platform suffix)
    3. Merge all yaml_templates into one JSONB
    4. Delete the duplicates
    """

    op.execute("""
        -- Create a temp table with merged data
        CREATE TEMP TABLE language_ref_merged AS
        WITH grouped_languages AS (
            SELECT
                name,
                version,
                -- Pick the row with shortest code (most likely without platform suffix)
                (ARRAY_AGG(id ORDER BY LENGTH(code), id))[1] as keep_id,
                -- Merge all yaml_templates
                jsonb_object_agg(key, value) as merged_templates
            FROM language_ref,
            LATERAL jsonb_each_text(yaml_templates)
            WHERE is_active = true
            GROUP BY name, version
        )
        SELECT
            gl.keep_id,
            lr.code,
            lr.name,
            lr.description,
            lr.version,
            lr.created_at,
            lr.updated_at,
            lr.is_deleted,
            lr.is_active,
            gl.merged_templates as yaml_templates
        FROM grouped_languages gl
        JOIN language_ref lr ON lr.id = gl.keep_id;

        -- Update the kept records with merged templates
        UPDATE language_ref lr
        SET yaml_templates = lrm.yaml_templates
        FROM language_ref_merged lrm
        WHERE lr.id = lrm.keep_id;

        -- Update any pipeline_mst references to point to the kept record
        UPDATE pipeline_mst pm
        SET language_ref_code = lrm.code
        FROM language_ref_merged lrm
        JOIN language_ref lr_old ON (lr_old.name = lrm.name AND lr_old.version = lrm.version)
        WHERE pm.language_ref_code = lr_old.code
        AND lr_old.id != lrm.keep_id;

        -- Delete duplicate records (keep only the ones in merged table)
        DELETE FROM language_ref
        WHERE id NOT IN (SELECT keep_id FROM language_ref_merged)
        AND (name, version) IN (
            SELECT name, version
            FROM language_ref
            GROUP BY name, version
            HAVING COUNT(*) > 1
        );
    """)


def downgrade() -> None:
    """
    Cannot automatically revert merging without losing data about which
    specific codes were used. Manual intervention required if rollback needed.
    """
    pass
