"""
Database model for model_registry_mst table.

Tracks which HuggingFace models have been downloaded to EFS,
their EFS path, and download status. Shared (tenant_code=NULL) records
are accessible by all tenants and are downloaded only once.
"""
from sqlalchemy import Column, String, Text, Float, Index
from app.db.models.base_model import BaseModel


class ModelRegistryModel(BaseModel):
    """
    Model registry table for tracking EFS-cached HuggingFace models.

    download_status lifecycle: pending → downloading → ready | failed

    BaseModel provides: id, code, name, description,
                        created_at, updated_at, is_deleted, is_active
    """

    __tablename__ = "model_registry_mst"

    # Model identity
    model_id = Column(
        String(512),
        nullable=False,
        comment="HuggingFace model ID (e.g., meta-llama/Llama-3.1-8B-Instruct)",
    )
    revision = Column(
        String(40),
        nullable=False,
        comment="Resolved HuggingFace commit SHA (40 chars) or 'main'",
    )
    efs_path = Column(
        String(512),
        nullable=False,
        comment="PVC-root-relative path (no leading slash). e.g. shared/meta-llama--Llama-3.1-8B-Instruct/abc123",
    )

    # Status
    download_status = Column(
        String(20),
        nullable=False,
        default="pending",
        comment="pending | downloading | ready | failed",
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Error detail when download_status=failed",
    )

    # Ownership — NULL means shared across all tenants (public models)
    tenant_code = Column(
        String(100),
        nullable=True,
        comment="Tenant code for private models; NULL for shared public models",
    )

    # Optional metadata
    size_gb = Column(
        Float,
        nullable=True,
        comment="Approximate model size in GB after download",
    )

    __table_args__ = (
        # Prevent duplicate downloads for the same model/revision/tenant combination.
        # COALESCE(tenant_code, '') normalises NULL so the unique constraint fires correctly.
        # Defined as a partial unique index via Alembic migration instead of here,
        # because SQLAlchemy UniqueConstraint doesn't support COALESCE directly.
        {"comment": "Tracks EFS-cached HuggingFace models per tenant (or shared)"},
    )

    def __repr__(self) -> str:
        return (
            f"<ModelRegistry(model_id='{self.model_id}', revision='{self.revision}', "
            f"status='{self.download_status}', tenant='{self.tenant_code}')>"
        )
