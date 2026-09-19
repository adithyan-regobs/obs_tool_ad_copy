from sqlalchemy import Boolean, Column, String, ForeignKey, Integer, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum


class KongRouteGroupModel(BaseModel):
    """
    Kong Route Group.

    One row per Kong route OBJECT — the unit plugins actually attach to.
    Terragrunt renders `kong_configs["<group>"].routes["<METHOD>"] = [paths...]`
    and targets plugins with `target_keys = ["<group>-<method>"]`, so every path
    sharing a (group, method) necessarily shares one plugin set. Storing plugins
    here makes that structural instead of something the UI has to police.
    """
    __tablename__ = "kong_route_groups"

    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Service this group belongs to (NULL for global/plugin groups)"
    )
    geo_loc_mst_code = Column(
        String(100),
        ForeignKey("geo_loc_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Geographic location code - matches kong_route_configs"
    )
    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=True,
        comment="Environment (dev/staging/qa/prod) - matches kong_route_configs"
    )

    route_group_key = Column(
        String(200),
        nullable=False,
        comment="Terragrunt kong_configs group key"
    )
    http_method = Column(
        String(10),
        nullable=False,
        comment='HTTP method — part of the group identity ("<group>-<method>")'
    )
    api_name = Column(
        String(100),
        nullable=True,
        comment="API identifier in kong_configs"
    )

    plugins = Column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="Name-only Kong plugins applied to every path in this group"
    )
    regex_priority = Column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="Kong regex_priority for this route-group"
    )

    # Carries the terragrunt `service { }` block; the other groups of the same
    # service emit `existing_service = "<owner label>"`. Stored, not derived —
    # guessing the owner by name let a rename or a public-first service move it.
    # Set on every row sharing the owner's label, since a kong_configs entry is
    # keyed by label and spans all methods.
    is_service_owner = Column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="True if this group carries the service{} block for its service"
    )

    # RELATIONSHIPS
    routes = relationship(
        "KongRouteConfigModel",
        back_populates="route_group",
        foreign_keys="[KongRouteConfigModel.kong_route_group_id]",
    )

    # Indexes (the identity uniqueness is an expression index — see migration 155)
    __table_args__ = (
        Index('idx_kong_route_groups_service', 'services_mst_code'),
    )

    def __repr__(self):
        return (
            f"<KongRouteGroup(id={self.id}, key='{self.route_group_key}', "
            f"method='{self.http_method}', plugins={self.plugins})>"
        )
