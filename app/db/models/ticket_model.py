from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class TicketModel(BaseModel):
    """
    Ticket table.
    Stores support/work tickets with multi-tenancy support.
    """

    __tablename__ = "ticket"

    # Foreign key to tenant
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant code (FK to tenants_mst.code)"
    )

    # Foreign key to user (assigned user for this ticket)
    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="User code (FK to user_mst.code) - assigned user for this ticket"
    )

    # Globally unique ticket number
    ticket_number = Column(
        String(100),
        unique=True,
        nullable=False,
        comment="Globally unique ticket number (e.g., ticket-001)"
    )

    # Source tracking fields
    source = Column(
        String(100),
        nullable=True,
        comment="Source system where ticket originated (e.g., portal, api, monitoring)"
    )

    source_ref_id = Column(
        String(255),
        nullable=True,
        comment="Reference ID from source system"
    )

    # Relationships
    tenant = relationship(
        "TenantsMstModel",
        back_populates="tickets",
        foreign_keys=[tenants_mst_code]
    )

    assigned_user = relationship(
        "UserMstModel",
        back_populates="assigned_tickets",
        foreign_keys=[user_mst_code]
    )

    def __repr__(self):
        return (
            f"<Ticket(id={self.id}, code='{self.code}', "
            f"ticket_number='{self.ticket_number}', "
            f"tenants_mst_code='{self.tenants_mst_code}', "
            f"user_mst_code='{self.user_mst_code}')>"
        )
