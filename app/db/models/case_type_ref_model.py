from sqlalchemy import Column, Enum
from app.db.models.base_model import BaseModel
from app.core.enum import InfraVendorEnum


class CaseTypeRefModel(BaseModel):
    __tablename__ = "case_type_ref"

    vendor_provider = Column(
        Enum(InfraVendorEnum, name="infra_vendor_enum"),
        nullable=True,
    )
