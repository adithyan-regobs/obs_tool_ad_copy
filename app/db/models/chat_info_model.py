from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, InfraVendorEnum


class ChatInfoModel(BaseModel):
    """
    Chat Info table.
    Stores chat conversation information linked to tenant, application, resource group, and service.
    Tracks the context and environment for each chat session.
    """

    __tablename__ = "chat_info"

    # Foreign Key to TenantsMstModel
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Foreign Key to ApplicationsMstModel (Optional)
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Foreign Key to ResourceGroupMstModel (Optional)
    resource_group_mst_code = Column(
        String(100),
        ForeignKey("resource_group_mst.code", ondelete="CASCADE"),
        nullable=True,
    )

    # Foreign Key to ServicesMstModel
    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,  # Optional: Chat may not always be service-specific
    )

    # Foreign Key to UserMstModel
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Environment enum (dev, staging, prod) - Optional
    environment_enum = Column(
        SqlEnum(EnvironmentEnum, name='environment_enum'),
        nullable=True,
    )

    # Infrastructure vendor/provider (aws, gcp, azure, on_prem)
    infra_vendor_enum = Column(
        SqlEnum(InfraVendorEnum, name='infra_vendor_enum'),
        nullable=False,
        default=InfraVendorEnum.aws,
    )

    # Foreign Key to GeoLocMstModel (Optional for backward compatibility)
    # Used by service config chat to form unique context hash
    # Now also used by langchat for chat history grouping
    geo_loc_mst_code = Column(
        String(100),
        ForeignKey("geo_loc_mst.code", ondelete="SET NULL"),
        nullable=True,
    )

    # Foreign Key to CaseTypeRefModel (Optional)
    # Used to group chat sessions by case type for history display
    case_type_ref_code = Column(
        String(100),
        ForeignKey("case_type_ref.code", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships - Child side uses singular names
    tenant = relationship(
        "TenantsMstModel",
        back_populates="chat_infos",
        foreign_keys=[tenants_mst_code],
    )

    application = relationship(
        "ApplicationsMstModel",
        back_populates="chat_infos",
        foreign_keys=[applications_mst_code],
    )

    resource_group = relationship(
        "ResourceGroupMstModel",
        back_populates="chat_infos",
        foreign_keys=[resource_group_mst_code],
    )

    service = relationship(
        "ServicesMstModel",
        back_populates="chat_infos",
        foreign_keys=[services_mst_code],
    )

    user = relationship(
        "UserMstModel",
        back_populates="chat_infos",
        foreign_keys=[user_mst_code],
    )

    geo_loc = relationship(
        "GeoLocMstModel",
        foreign_keys=[geo_loc_mst_code],
    )

    case_type_ref = relationship(
        "CaseTypeRefModel",
        foreign_keys=[case_type_ref_code],
    )

    chat_messages = relationship(
        "ChatMessageModel",
        back_populates="chat_info",
        cascade="all, delete-orphan"
    )

    chat_summaries = relationship(
        "ChatSummaryModel",
        back_populates="chat_info",
        cascade="all, delete-orphan"
        # One-to-many relationship (one summary per case_code per chat)
    )

    def __repr__(self):
        return (
            f"<ChatInfo(id={self.id}, code='{self.code}', "
            f"name='{self.name}', tenant_code='{self.tenants_mst_code}', "
            f"application_code='{self.applications_mst_code}', "
            f"resource_group_code='{self.resource_group_mst_code}', "
            f"service_code='{self.services_mst_code}', "
            f"geo_loc_code='{self.geo_loc_mst_code}', "
            f"case_type_code='{self.case_type_ref_code}', "
            f"environment='{self.environment_enum.value if self.environment_enum else None}', "
            f"vendor='{self.infra_vendor_enum.value}')>"
        )