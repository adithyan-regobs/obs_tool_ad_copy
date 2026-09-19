from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class TenantsMstModel(BaseModel):
    """
    Tenant Master table.
    Stores tenant information.
    """

    __tablename__ = "tenants_mst"

    # Organization subdomain (from Clerk)
    subdomain = Column(String(100), unique=True, nullable=True)

    # Tenant-specific configuration (GitHub repos, branches, etc.)
    # Structure: {"github": {"infra_repository": "org/repo", "infra_branch": "main"}}
    config = Column(JSONB, nullable=True, default={})

    # Relationship — link by tenant_code instead of tenant_id
    applications = relationship(
        "ApplicationsMstModel",
        back_populates="tenant",
        cascade="all, delete-orphan"
    )

    chat_infos = relationship(
        "ChatInfoModel",
        back_populates="tenant",
        cascade="all, delete-orphan"
    )

    tickets = relationship(
        "TicketModel",
        back_populates="tenant",
        # No cascade - DB handles it via FK ondelete="CASCADE"
    )

    def __repr__(self):
        return (
            f"<TenantsMst(id={self.id}, tenant_code='{self.code}', "
            f"tenant_name='{self.name}')>"
        )
