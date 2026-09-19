"""
Namespace Master Model

Stores Kubernetes namespaces associated with infrastructure instances (EKS clusters).
Used for dropdown selection in EKS deployment configuration.
"""
from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class NamespaceMstModel(BaseModel):
    """
    Namespace Master table for managing Kubernetes namespaces per infrastructure.

    Each EKS cluster (infrastructure_mst) can have multiple namespaces.
    Users can select from existing namespaces or add new ones.
    """
    __tablename__ = "namespace_mst"

    # Foreign key to infrastructure_mst (EKS cluster)
    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Infrastructure code (EKS cluster) this namespace belongs to"
    )

    # Kubernetes namespace name
    namespace = Column(
        String(255),
        nullable=False,
        comment="Kubernetes namespace name (e.g., default, production, staging)"
    )

    # Relationship to infrastructure
    infrastructure = relationship(
        "InfrastructureMstModel",
        foreign_keys=[infrastructure_mst_code]
    )

    def __repr__(self):
        return (
            f"<NamespaceMst(id={self.id}, code='{self.code}', "
            f"infrastructure='{self.infrastructure_mst_code}', "
            f"namespace='{self.namespace}')>"
        )
