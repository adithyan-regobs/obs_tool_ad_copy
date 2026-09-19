from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, JSON
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, PipelineAgentEnum


class PipelineVendorMstModel(BaseModel):
    """
    Pipeline Vendor Master table.
    Stores pipeline vendor/agent configurations for CI/CD systems.
    """

    __tablename__ = "pipeline_vendor_mst"

    # Foreign Key to tenants_mst
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Foreign Key to applications_mst (nullable - for app-level override)
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Foreign Key to resource_group_mst (nullable - for RG-level override)
    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Foreign Key to services_mst (nullable - for service-level override)
    service_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Environment (dev, staging, prod)
    environment = Column(
        SqlEnum(EnvironmentEnum, name='environment_enum'),
        nullable=False,
    )

    # Authentication configuration (JSON)
    auth_config = Column(
        JSON,
        nullable=True,
    )

    # Runner/agent information configuration (JSON)
    runner_info_config = Column(
        JSON,
        nullable=True,
    )

    # Pipeline agent type (gitaction, jenkins, etc.)
    pipeline_agent_enum = Column(
        SqlEnum(PipelineAgentEnum, name='pipeline_agent_enum'),
        nullable=False,
    )

    # Relationship to TenantsMstModel
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    # Relationship to ApplicationsMstModel
    application = relationship(
        "ApplicationsMstModel",
        foreign_keys=[applications_mst_code],
    )

    # Relationship to ResourceGroupMstModel
    resource_group = relationship(
        "ResourceGroupMstModel",
        foreign_keys=[resource_group_mst_code],
    )

    # Relationship to ServicesMstModel
    service = relationship(
        "ServicesMstModel",
        foreign_keys=[service_mst_code],
    )

    # Relationship to PipelineMstModel
    pipelines = relationship(
        "PipelineMstModel",
        back_populates="pipeline_vendor",
    )

    def __repr__(self):
        return (
            f"<PipelineVendorMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', environment='{self.environment.value}', "
            f"pipeline_agent='{self.pipeline_agent_enum.value}')>"
        )
