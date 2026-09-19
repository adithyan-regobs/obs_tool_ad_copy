"""Add log_provider_config table and log_provider column to service_configs

Revision ID: 122_add_log_provider_config
Revises: 121_add_model_serving_service_type
Create Date: 2026-04-05

Adds a tenant-level log provider configuration table for storing
credentials per provider (CloudWatch IAM role, Datadog API keys, etc.).
Also adds a log_provider column to service_configs for per-service override.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "122_add_log_provider_config"
down_revision: Union[str, None] = "121_add_model_serving_service_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type via raw SQL (drop first in case of partial prior run)
    op.execute("DROP TYPE IF EXISTS log_provider_enum")
    op.execute("CREATE TYPE log_provider_enum AS ENUM ('CLOUDWATCH', 'DATADOG')")

    # Create log_provider_config table via raw SQL to avoid SQLAlchemy enum auto-creation
    op.execute("""
        CREATE TABLE log_provider_config (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            description VARCHAR(500),
            tenants_mst_code VARCHAR(100) NOT NULL REFERENCES tenants_mst(code) ON DELETE CASCADE,
            provider log_provider_enum NOT NULL,
            auth_config JSONB NOT NULL DEFAULT '{}',
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_log_provider_config_tenant_provider UNIQUE (tenants_mst_code, provider)
        )
    """)

    # Add log_provider column to service_configs
    op.execute("""
        ALTER TABLE service_configs
        ADD COLUMN log_provider log_provider_enum DEFAULT NULL
    """)
    op.execute("COMMENT ON COLUMN service_configs.log_provider IS 'Override log provider for this service. NULL = auto-detect'")


def downgrade() -> None:
    op.drop_column("service_configs", "log_provider")
    op.drop_table("log_provider_config")
    op.execute("DROP TYPE IF EXISTS log_provider_enum")
