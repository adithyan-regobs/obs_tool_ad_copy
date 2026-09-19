from sqlalchemy import Column, String, BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class DbPermissionMstModel(BaseModel):
    """
    Access control for database objects on a K8s-hosted server.

    db_object_mst_id = null  → server-level permission (admin/superuser for entire server)
    db_object_mst_id = <id>  → permission scoped to that specific object (database/schema/table)

    permissions JSONB examples:
      "admin"            → full server admin
      ["read", "write"]  → granular object-level access
      ["read"]           → read-only access
    """

    __tablename__ = "db_permission_mst"

    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="FK to the server (infrastructure_mst) this permission belongs to",
    )

    db_object_mst_id = Column(
        BigInteger,
        ForeignKey("db_object_mst.id", ondelete="CASCADE"),
        nullable=True,
        comment="FK to db_object_mst — null means server-level permission",
    )

    username = Column(
        String(255),
        nullable=False,
        comment="PostgreSQL username this permission applies to",
    )

    permissions = Column(
        JSONB,
        nullable=False,
        comment='Permission value — e.g. "admin" or ["read", "write"]',
    )

    tenant_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )

    environment = Column(
        String(50),
        nullable=False,
    )

    metadata_json = Column(
        JSONB,
        nullable=True,
        default={},
        comment="Additional metadata (e.g. encrypted password for user management)",
    )

    # Relationships
    db_object = relationship(
        "DbObjectMstModel",
        back_populates="permissions",
        foreign_keys=[db_object_mst_id],
    )

    def __repr__(self):
        scope = f"object={self.db_object_mst_id}" if self.db_object_mst_id else "server-level"
        return (
            f"<DbPermissionMst(id={self.id}, user='{self.username}', "
            f"permissions={self.permissions}, scope={scope}, "
            f"server='{self.infrastructure_mst_code}')>"
        )
