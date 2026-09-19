"""Add unique (transaction_code, table_name, key) to variable_mst

Revision ID: 152_add_unique_owner_key_to_variable_mst
Revises: 151_add_owner_to_services_mst
Create Date: 2026-07-18

Changes:
  - Soft-delete existing duplicate live rows per (transaction_code,
    table_name, key), keeping the best candidate: prefer the row that is
    linked to a cloud store (variable_cloud_identifier set), then the newest
    (highest id). Mirrors the app's soft-delete semantics — no data loss.
  - Create partial unique index uq_variable_mst_owner_key on
    (transaction_code, table_name, key) WHERE is_deleted IS NOT TRUE, so an
    owning resource can hold each key only once. Soft-deleted rows are
    excluded, so deleting a variable never blocks re-creating the same key.
    GLOBAL variables (NULL owner columns) are unaffected: Postgres treats
    NULLs as distinct in unique indexes.
"""

from alembic import op
import sqlalchemy as sa

revision = "152_add_unique_owner_key_to_variable_mst"
down_revision = "151_add_owner_to_services_mst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe live rows so the unique index can be created. Keep, per
    # (transaction_code, table_name, key): cloud-linked row first, then the
    # newest by id; soft-delete the rest (same as the app's delete path).
    op.execute(
        sa.text(
            """
            UPDATE variable_mst
               SET is_deleted = TRUE,
                   is_active  = FALSE
             WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY transaction_code, table_name, key
                               ORDER BY (variable_cloud_identifier IS NOT NULL) DESC,
                                        id DESC
                           ) AS rn
                      FROM variable_mst
                     WHERE is_deleted IS NOT TRUE
                       AND transaction_code IS NOT NULL
                       AND table_name IS NOT NULL
                ) ranked
                WHERE rn > 1
             )
            """
        )
    )

    op.create_index(
        "uq_variable_mst_owner_key",
        "variable_mst",
        ["transaction_code", "table_name", "key"],
        unique=True,
        postgresql_where=sa.text("is_deleted IS NOT TRUE"),
    )


def downgrade() -> None:
    # Rows soft-deleted by the dedup above are not restored — they are
    # indistinguishable from user-deleted rows.
    op.drop_index("uq_variable_mst_owner_key", table_name="variable_mst")
