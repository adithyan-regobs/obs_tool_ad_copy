"""
Policy Reference Model

Stores available policy types with their capabilities.
This is a lookup/reference table for all policies in the system.

Example data:
| code            | policy_category | can_read | can_write | can_manage |
|-----------------|-----------------|----------|-----------|------------|
| service_admin   | service         | true     | true      | true       |
| service_manager | service         | true     | true      | false      |
| service_support | service         | true     | false     | false      |
"""

from sqlalchemy import Column, String, Boolean
from app.db.models.base_model import BaseModel


class PolicyRefModel(BaseModel):
    """
    Policy Reference table.

    Stores all policy types available in the system.
    Each policy has capabilities (can_read, can_write, can_manage).

    Attributes:
        policy_category: Type of policy - "service", "infrastructure", "alert"
        can_read: Can view resources
        can_write: Can edit resources
        can_manage: Can grant/revoke permissions to others
    """

    __tablename__ = "policy_ref"

    # Category to distinguish policy types
    # Examples: "service", "infrastructure", "alert"
    policy_category = Column(
        String(50),
        nullable=False,
        default="service",
        comment="Category: service, infrastructure, alert, etc."
    )

    # Capability: Can view resources
    can_read = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Can view resources"
    )

    # Capability: Can edit resources
    can_write = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Can edit resources"
    )

    # Capability: Can grant/revoke permissions
    can_manage = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Can grant/revoke permissions to others"
    )

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"<PolicyRef(code='{self.code}', category='{self.policy_category}')>"