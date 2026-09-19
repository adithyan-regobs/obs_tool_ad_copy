"""Add chat_info table

Revision ID: 001_chat_info
Revises: 6b1adf98577d
Create Date: 2025-11-04 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001_chat_info'
down_revision: Union[str, None] = '6b1adf98577d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create chat_info table"""
    # Create environment_enum type if it doesn't exist
    from sqlalchemy.dialects import postgresql
    environment_enum = postgresql.ENUM('dev', 'staging', 'prod', name='environment_enum', create_type=False)

    op.create_table(
        'chat_info',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('tenants_mst_code', sa.String(length=100), nullable=False),
        sa.Column('applications_mst_code', sa.String(length=100), nullable=False),
        sa.Column('resource_group_mst_code', sa.String(length=100), nullable=False),
        sa.Column('services_mst_code', sa.String(length=100), nullable=True),
        sa.Column('environment_enum', environment_enum, nullable=False, server_default='dev'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
        sa.ForeignKeyConstraint(['tenants_mst_code'], ['tenants_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['applications_mst_code'], ['applications_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resource_group_mst_code'], ['resource_group_mst.code'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['services_mst_code'], ['services_mst.code'], ondelete='CASCADE'),
    )

    # Create indexes for better query performance
    op.create_index('idx_chat_info_tenant', 'chat_info', ['tenants_mst_code'])
    op.create_index('idx_chat_info_application', 'chat_info', ['applications_mst_code'])
    op.create_index('idx_chat_info_resource_group', 'chat_info', ['resource_group_mst_code'])
    op.create_index('idx_chat_info_service', 'chat_info', ['services_mst_code'])
    op.create_index('idx_chat_info_environment', 'chat_info', ['environment_enum'])
    op.create_index('idx_chat_info_created_at', 'chat_info', ['created_at'])
    op.create_index('idx_chat_info_active', 'chat_info', ['is_active'])


def downgrade() -> None:
    """Drop chat_info table"""
    op.drop_index('idx_chat_info_active', table_name='chat_info')
    op.drop_index('idx_chat_info_created_at', table_name='chat_info')
    op.drop_index('idx_chat_info_environment', table_name='chat_info')
    op.drop_index('idx_chat_info_service', table_name='chat_info')
    op.drop_index('idx_chat_info_resource_group', table_name='chat_info')
    op.drop_index('idx_chat_info_application', table_name='chat_info')
    op.drop_index('idx_chat_info_tenant', table_name='chat_info')
    op.drop_table('chat_info')
