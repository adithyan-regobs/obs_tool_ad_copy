"""Refactor language_ref to use JSONB for yaml_templates

Revision ID: 012_yaml_templates_jsonb
Revises: 011_fix_pipeline_run_track
Create Date: 2025-11-05 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '012_yaml_templates_jsonb'
down_revision: Union[str, None] = '011_fix_pipeline_run_track'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Refactor language_ref table to use JSONB for yaml_templates.
    This eliminates duplicate rows for each pipeline agent.
    """

    # Step 1: Make pipeline_agent_enum and yaml_url nullable (preparing for removal)
    op.alter_column('language_ref', 'pipeline_agent_enum', nullable=True)
    op.alter_column('language_ref', 'yaml_url', nullable=True)

    # Step 2: Add new yaml_templates JSONB column
    op.add_column('language_ref',
        sa.Column('yaml_templates', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )

    # Step 3: Migrate existing data into JSONB format
    # Group by base code (removing _GITHUB, _GITLAB suffixes) and aggregate templates
    op.execute("""
        -- Create temporary table with aggregated data
        CREATE TEMP TABLE language_ref_temp AS
        SELECT
            MIN(id) as id,
            REGEXP_REPLACE(code, '_(GITHUB|GITLAB|CIRCLECI|BITBUCKET|TRAVISCI|DRONE|WERCKER|BUILDKITE|AZURE|AWS)$', '') as new_code,
            name,
            description,
            version,
            created_at,
            updated_at,
            is_deleted,
            is_active,
            jsonb_object_agg(
                LOWER(pipeline_agent_enum::text),
                yaml_url
            ) FILTER (WHERE yaml_url IS NOT NULL) as yaml_templates
        FROM language_ref
        GROUP BY
            REGEXP_REPLACE(code, '_(GITHUB|GITLAB|CIRCLECI|BITBUCKET|TRAVISCI|DRONE|WERCKER|BUILDKITE|AZURE|AWS)$', ''),
            name,
            description,
            version,
            created_at,
            updated_at,
            is_deleted,
            is_active;

        -- Delete all existing rows
        DELETE FROM language_ref;

        -- Insert aggregated data back
        INSERT INTO language_ref (id, code, name, description, version, created_at, updated_at, is_deleted, is_active, yaml_templates)
        SELECT id, new_code, name, description, version, created_at, updated_at, is_deleted, is_active, yaml_templates
        FROM language_ref_temp;

        -- Update pipeline_mst references to new codes (remove suffixes)
        UPDATE pipeline_mst
        SET language_ref_code = REGEXP_REPLACE(language_ref_code, '_(GITHUB|GITLAB|CIRCLECI|BITBUCKET|TRAVISCI|DRONE|WERCKER|BUILDKITE|AZURE|AWS)$', '')
        WHERE language_ref_code IS NOT NULL;
    """)

    # Step 4: Make yaml_templates NOT NULL now that data is migrated
    op.alter_column('language_ref', 'yaml_templates', nullable=False)

    # Step 5: Drop old columns
    op.drop_column('language_ref', 'pipeline_agent_enum')
    op.drop_column('language_ref', 'yaml_url')

    # Step 6: Create GIN index on JSONB column for performance
    op.create_index(
        'idx_language_ref_yaml_templates_gin',
        'language_ref',
        ['yaml_templates'],
        postgresql_using='gin'
    )


def downgrade() -> None:
    """
    Revert to the old schema with separate rows per pipeline agent.
    """

    # Drop GIN index
    op.drop_index('idx_language_ref_yaml_templates_gin', 'language_ref')

    # Add back old columns
    op.add_column('language_ref',
        sa.Column('pipeline_agent_enum',
                  postgresql.ENUM('aws_codepipeline', 'azure_devops', 'github_actions', 'gitlab_ci',
                                  'bitbucket_pipelines', 'circleci', 'travisci', 'drone', 'wercker', 'buildkite',
                                  name='pipeline_agent_enum', create_type=False),
                  nullable=True)
    )
    op.add_column('language_ref',
        sa.Column('yaml_url', sa.String(length=500), nullable=True)
    )

    # Expand JSONB back into separate rows
    op.execute("""
        -- Create temp table with expanded data
        CREATE TEMP TABLE language_ref_expanded AS
        SELECT
            id,
            code || '_' || UPPER(key) as expanded_code,
            name,
            description,
            version,
            created_at,
            updated_at,
            is_deleted,
            is_active,
            key::pipeline_agent_enum as pipeline_agent_enum,
            value::text as yaml_url
        FROM language_ref,
        LATERAL jsonb_each_text(yaml_templates);

        -- Delete aggregated rows
        DELETE FROM language_ref;

        -- Insert expanded rows
        INSERT INTO language_ref (code, name, description, version, created_at, updated_at, is_deleted, is_active, pipeline_agent_enum, yaml_url)
        SELECT expanded_code, name, description, version, created_at, updated_at, is_deleted, is_active, pipeline_agent_enum, yaml_url
        FROM language_ref_expanded;

        -- Restore pipeline_mst references
        UPDATE pipeline_mst pm
        SET language_ref_code = lr.code
        FROM language_ref lr
        WHERE REGEXP_REPLACE(pm.language_ref_code, '_(GITHUB|GITLAB|CIRCLECI|BITBUCKET|TRAVISCI|DRONE|WERCKER|BUILDKITE|AZURE|AWS)$', '') =
              REGEXP_REPLACE(lr.code, '_(GITHUB|GITLAB|CIRCLECI|BITBUCKET|TRAVISCI|DRONE|WERCKER|BUILDKITE|AZURE|AWS)$', '')
        AND lr.pipeline_agent_enum = 'github_actions'
        LIMIT 1;
    """)

    # Make old columns NOT NULL
    op.alter_column('language_ref', 'pipeline_agent_enum', nullable=False)

    # Drop JSONB column
    op.drop_column('language_ref', 'yaml_templates')
