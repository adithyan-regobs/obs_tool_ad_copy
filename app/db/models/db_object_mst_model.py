from sqlalchemy import Column, String, BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class DbObjectMstModel(BaseModel):
    """
    Hierarchical tree of database objects on a K8s-hosted server.

    Supports any level of nesting via self-referential parent_id:
      server
       └── database  (parent_id = null)
            └── schema  (parent_id = database.id)
                 └── table / view / sequence / function  (parent_id = schema.id)

    type  : free-text — "database", "schema", "table", "view", "sequence", etc.
    metadata : JSONB for type-specific properties (encoding, owner, collation, etc.)
    """

    __tablename__ = "db_object_mst"

    infrastructure_mst_code = Column(
        String(100),
        ForeignKey("infrastructure_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="FK to the server (infrastructure_mst) that owns this object",
    )

    type = Column(
        String(50),
        nullable=False,
        comment="Object type: database, schema, table, view, sequence, function, etc.",
    )

    parent_id = Column(
        BigInteger,
        ForeignKey("db_object_mst.id", ondelete="CASCADE"),
        nullable=True,
        comment="Self-referential FK — null for top-level databases, set for schemas/tables/etc.",
    )

    metadata_json = Column(
        "metadata",
        JSONB,
        nullable=True,
        default={},
        comment="Type-specific properties: encoding, owner, collation, etc.",
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

    # Relationships
    parent = relationship(
        "DbObjectMstModel",
        remote_side="DbObjectMstModel.id",
        foreign_keys=[parent_id],
        back_populates="children",
    )

    children = relationship(
        "DbObjectMstModel",
        foreign_keys=[parent_id],
        back_populates="parent",
    )

    permissions = relationship(
        "DbPermissionMstModel",
        back_populates="db_object",
        foreign_keys="DbPermissionMstModel.db_object_mst_id",
    )

    def __repr__(self):
        return (
            f"<DbObjectMst(id={self.id}, code='{self.code}', "
            f"name='{self.name}', type='{self.type}', "
            f"server='{self.infrastructure_mst_code}', parent_id={self.parent_id})>"
        )