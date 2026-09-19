from sqlalchemy import (
    Column,
    String,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ServiceDependencyMapModel(BaseModel):
    """
    Service Dependency Mapping table.
    Defines dependency relationships between services and infrastructure components
    within the same application.
    """

    __tablename__ = "service_dependency_map"

    # Foreign Keys
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationships
    application = relationship("ApplicationsMstModel", back_populates="service_dependencies")
    service = relationship("ServicesMstModel", back_populates="dependencies")
    infrastructure = relationship("InfrastructureMstModel", back_populates="dependents")

    # Table constraints — prevent duplicate dependency entries
    __table_args__ = (
        UniqueConstraint(
            "applications_mst_code",
            "services_mst_code",
            "infrastructure_mst_code",
            name="uq_service_dependency_unique",
        ),
    )

    def __repr__(self):
        return (
            f"<ServiceDependencyMap(id={self.id}, "
            f"applications_mst_code='{self.applications_mst_code}', "
            f"services_mst_code='{self.services_mst_code}', "
            f"infrastructure_mst_code='{self.infrastructure_mst_code}')>"
        )
