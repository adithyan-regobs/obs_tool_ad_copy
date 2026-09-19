"""Create sales_inquiries table

Revision ID: 125_create_sales_inquiries_table
Revises: 124_add_api_dropdown_cache_to_chat_session
Create Date: 2026-04-08

Creates the sales_inquiries table for storing interest form submissions
from unknown/unapproved users during signup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "125_create_sales_inquiries_table"
down_revision: Union[str, None] = "124_add_api_dropdown_cache_to_chat_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_inquiries",
        # BaseModel columns
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False, comment="Full name of the user"),
        sa.Column("description", sa.String(500), nullable=True, comment="Briefly describe your requirement"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column("is_deleted", sa.Boolean(), default=False),
        sa.Column("is_active", sa.Boolean(), default=True),

        # Contact details
        sa.Column("email", sa.String(255), nullable=False, comment="User email address"),
        sa.Column("phone_number", sa.String(50), nullable=True, comment="Phone number"),

        # Organization details
        sa.Column("company_name", sa.String(255), nullable=True, comment="Company or organization name"),
        sa.Column("role_title", sa.String(255), nullable=True, comment="Role or job title"),
        sa.Column("team_department", sa.String(255), nullable=True, comment="Team or department"),
        sa.Column("country", sa.String(100), nullable=False, comment="Country"),

        # Interest details
        sa.Column("use_case", sa.String(1000), nullable=False, comment="What they want to use DevLift.ai for"),

        # Additional info
        sa.Column("preferred_contact_method", sa.String(50), nullable=True, comment="Email, Phone, or Either"),
        sa.Column("referral_source", sa.String(255), nullable=True, comment="How they heard about DevLift.ai"),
        sa.Column("additional_notes", sa.Text(), nullable=True, comment="Any additional notes"),

        # Consent
        sa.Column("consent_given", sa.Boolean(), nullable=False, default=False, comment="Agreed to be contacted"),

        # Sales tracking
        sa.Column("status", sa.String(50), nullable=False, server_default="new", comment="new, contacted, qualified, closed"),

        sa.PrimaryKeyConstraint("id"),
    )

    # Indexes
    op.create_index("ix_sales_inquiries_email", "sales_inquiries", ["email"])
    op.create_index("ix_sales_inquiries_status", "sales_inquiries", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sales_inquiries_status", table_name="sales_inquiries")
    op.drop_index("ix_sales_inquiries_email", table_name="sales_inquiries")
    op.drop_table("sales_inquiries")
