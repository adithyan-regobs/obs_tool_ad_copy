"""
CI/CD Template Reference Model
Stores CI/CD workflow templates with their configuration and step definitions.
"""
from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models.base_model import BaseModel


class CicdTemplateRefModel(BaseModel):
    """
    CI/CD Template Reference Model

    Represents a CI/CD workflow template that can be used to generate GitHub Actions workflows.
    Each template contains a configuration with steps that define the build and deployment process.
    """
    __tablename__ = "cicd_template_ref"

    # Config JSONB column containing workflow steps and configuration
    config = Column(JSONB, nullable=True, comment="Workflow configuration including steps array")

    # Foreign Keys
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Application service this template belongs to"
    )

    tenant_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Tenant this template belongs to (null = global/template for all tenants)"
    )

    # Relationships
    application = relationship(
        "ApplicationsMstModel",
        foreign_keys=[applications_mst_code]
    )

    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenant_mst_code]
    )

    def __repr__(self):
        return f"<CicdTemplateRefModel(id={self.id}, code={self.code}, name={self.name})>"
