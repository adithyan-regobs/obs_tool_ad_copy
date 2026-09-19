"""Add unique service name per (tenant, application) to services_mst

Revision ID: 153_add_unique_service_name_per_app
Revises: 152_add_unique_owner_key_to_variable_mst
Create Date: 2026-07-24

Changes:
  - Soft-delete existing duplicate live rows per
    (tenants_mst_code, applications_mst_code, lower(name)), keeping the newest
    row (highest id). Mirrors the app's soft-delete semantics — no data loss.
  - Create partial unique index uq_services_mst_tenant_app_name on
    (tenants_mst_code, applications_mst_code, lower(name)) WHERE
    is_deleted IS NOT TRUE, so a service name is unique per application
    (case-insensitive). Soft-deleted rows are excluded, so deleting a service
    never blocks re-creating the same name.
"""

from alembic import op
import sqlalchemy as sa

revision = "153_add_unique_service_name_per_app"
down_revision = "152_add_unique_owner_key_to_variable_mst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe live rows so the unique index can be created. Keep, per
    # (tenants_mst_code, applications_mst_code, lower(name)), the newest row by
    # id; soft-delete the rest (same as the app's delete path).
    op.execute(
        sa.text(
            """
            UPDATE services_mst
               SET is_deleted = TRUE,
                   is_active  = FALSE
             WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY tenants_mst_code,
                                            applications_mst_code,
                                            lower(name)
                               ORDER BY id DESC
                           ) AS rn
                      FROM services_mst
                     WHERE is_deleted IS NOT TRUE
                ) ranked
                WHERE rn > 1
             )
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_services_mst_tenant_app_name
                ON services_mst (tenants_mst_code, applications_mst_code, lower(name))
             WHERE is_deleted IS NOT TRUE
            """
        )
    )


def downgrade() -> None:
    # Rows soft-deleted by the dedup above are not restored — they are
    # indistinguishable from user-deleted rows.
    op.drop_index("uq_services_mst_tenant_app_name", table_name="services_mst")
