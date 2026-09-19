from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, Boolean
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import ServiceTypeEnum


class ServicesMstModel(BaseModel):
    """
    Service Master table.
    Stores service information linked to an application.
    """

    __tablename__ = "services_mst"

    # Foreign Key to application_code in ApplicationsMst
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    resource_group_mst_code = Column(
         String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Service type (API or Background Service)
    service_type = Column(
        SqlEnum(ServiceTypeEnum, name='servicetypeenum'),
        nullable=False,
    )

    # NOTE: infrastructuretype_ref_code has been moved to service_config table
    # to allow different infra types per environment/config

    # Reference to the actual infrastructure instance where this service is deployed
    # Links to infrastructure_mst which contains vendor account info
    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="RESTRICT"),
        nullable=True,
    )

    # NOTE: infra_vendor_enum has been moved to service_config table
    # to allow different vendors per environment/config

    # Whether service is publicly accessible (has public endpoints)
    is_public_facing = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    # Owner of the service (who is responsible / point of contact).
    # Nullable: legacy services may have no owner until assigned.
    owner_user_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationship to owning user (UserMstModel)
    owner = relationship(
        "UserMstModel",
        foreign_keys=[owner_user_code],
    )

    # Relationship to TenantsMstModel
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
    )

    # Relationship to ApplicationsMstModel
    application = relationship(
        "ApplicationsMstModel",
        back_populates="services",
        foreign_keys=[applications_mst_code],
    )

    # Relationship to ResourceGroupMstModel
    resource_group = relationship(
        "ResourceGroupMstModel",
        back_populates="services",
        foreign_keys=[resource_group_mst_code],
    )

    # Relationship to ServiceDependencyMapModel
    dependencies = relationship(
        "ServiceDependencyMapModel",
        back_populates="service"
    )

    # NOTE: infrastructure_type relationship removed - now in service_config

    # Relationship to InfrastructureMstModel
    infrastructure = relationship(
        "InfrastructureMstModel",
        foreign_keys=[infrastructure_mst_code],
    )

    # Relationship to ServiceConfigModel
    service_configs = relationship(
        "ServiceConfigModel",
        back_populates="service",
        cascade="all, delete-orphan"
    )

    chat_infos = relationship(
        "ChatInfoModel",
        back_populates="service",
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return (
            f"<ServicesMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}')>"
        )
