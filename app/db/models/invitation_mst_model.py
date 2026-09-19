"""
Database model for user invitations
"""
from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean
from app.db.models.base_model import BaseModel


class InvitationMstModel(BaseModel):
    """
    Invitation Master table for tracking user invitations

    Stores invitations sent to users to join organizations.
    Used for validation and tracking invitation status.
    """

    __tablename__ = "invitations_mst"

    # Invitation details
    email = Column(String(255), nullable=False, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)

    # User role and ownership (maps to Clerk public_metadata)
    role = Column(String(50), nullable=False)  # "Admin" or "User" → userRole
    is_org_owner = Column(Boolean, default=False, nullable=False)  # → isOrganizationOwner

    # Relationships
    tenant_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    invited_by_user_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="SET NULL"),
        nullable=True
    )

    # Invitation status tracking
    status = Column(
        String(50),
        default="pending",
        nullable=False,
        index=True
    )  # "pending", "accepted", "expired", "cancelled"

    # Timestamps
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return (
            f"<InvitationMst(code='{self.code}', email='{self.email}', "
            f"status='{self.status}', tenant_code='{self.tenant_code}')>"
        )
