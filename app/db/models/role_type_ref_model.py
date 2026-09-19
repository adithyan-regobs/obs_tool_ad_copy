"""
Role Type Reference Model

Stores available role types in the system (BE, FE, Admin, etc.).
This is a lookup/reference table - not user-role assignments.

Example data:
| code  | name           | description                |
|-------|----------------|----------------------------|
| be    | BE             | Backend Engineer           |
| fe    | FE             | Frontend Engineer          |
| admin | Admin          | Administrator              |
| user  | User           | Regular User               |
"""

from app.db.models.base_model import BaseModel


class RoleTypeRefModel(BaseModel):
    """
    Role Type Reference table.

    Stores all role types available in the system.
    Used by service_user_permission for role-based permissions.

    This is different from role_mst which stores user-role assignments.
    """

    __tablename__ = "role_type_ref"

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"<RoleTypeRef(code='{self.code}', name='{self.name}')>"
