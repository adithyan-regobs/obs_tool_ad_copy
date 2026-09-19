"""Add nullable parent_code to infrastructure_mst

Revision ID: 131_add_parent_code_to_infrastructure_mst
Revises: 130_add_resource_status_to_service_config_and_infra
Create Date: 2026-04-27

Adds a self-referencing ``parent_code`` column to ``infrastructure_mst`` so
that an infrastructure record can point to a parent infrastructure record
(e.g. a pod/namespace nested under its cluster). The column is nullable
because most infrastructure records have no parent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "131_add_parent_code_to_infrastructure_mst"
down_revision: Union[str, None] = "130_add_resource_status_to_service_config_and_infra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "infrastructure_mst",
        sa.Column(
            "parent_code",
            sa.String(length=100),
            nullable=True,
            comment="Self-reference to parent infrastructure_mst.code",
        ),
    )
    op.create_foreign_key(
        "fk_infrastructure_mst_parent_code",
        "infrastructure_mst",
        "infrastructure_mst",
        ["parent_code"],
        ["code"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_infrastructure_mst_parent_code",
        "infrastructure_mst",
        type_="foreignkey",
    )
    op.drop_column("infrastructure_mst", "parent_code")

