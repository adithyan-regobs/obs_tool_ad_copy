from typing import Optional
from sqlalchemy import Column, String, ForeignKey, BigInteger, TIMESTAMP, Index, Integer, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy import Enum as SqlEnum
from app.db.models.base_model import BaseModel
from app.core.enum import DeploymentStatusEnum, EnvironmentEnum


class KongRouteConfigModel(BaseModel):
    """
    Kong Route Configuration Model
    Represents Kong Gateway routes deployed via GitOps PR workflow.
    """
    __tablename__ = "kong_route_configs"

    # Foreign Keys (nullable for plugin/global routes not tied to a service)
    services_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Service this route belongs to (NULL for plugin/global routes)"
    )

    # Geographic Location (matches infrastructure_mst.geo_loc_mst_code)
    geo_loc_mst_code = Column(
        String(100),
        ForeignKey("geo_loc_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Geographic location code - matches infrastructure_mst.geo_loc_mst_code"
    )

    # Environment (matches infrastructure_mst.environments_enum)
    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=True,
        comment="Environment (dev/staging/prod) - matches infrastructure_mst.environments_enum"
    )

    # Kong Route Configuration
    api_name = Column(
        String(100),
        nullable=False,
        comment="API identifier in kong_configs"
    )
    http_method = Column(
        String(10),
        nullable=False,
        comment="HTTP method (GET, POST, PUT, DELETE, PATCH, OPTIONS)"
    )
    route_path = Column(
        String(500),
        nullable=False,
        comment="Kong route pattern (regex with $ suffix)"
    )

    # NOTE: plugins, regex_priority and route_group_key used to live here. They are
    # properties of the Kong route OBJECT — one per (group, method) — not of a single
    # path, so they moved to kong_route_groups (migration 156 dropped the columns).
    # Read them through `route_group`.

    # PR WORKFLOW TRACKING (following alert_configs pattern)
    creation_status = Column(
        SqlEnum(DeploymentStatusEnum, name="deployment_status_enum"),
        nullable=True,
        default=DeploymentStatusEnum.INITIATED,
        comment="Deployment workflow status"
    )
    creation_status_updated_by = Column(
        String(255),
        nullable=True,
        comment="Email or GitHub username of user/system that updated status"
    )
    creation_status_updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Timestamp of last status update"
    )
    gitops_workflow_id = Column(
        BigInteger,
        ForeignKey("gitops_workflow_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="Foreign key to gitops_workflow_detail (MANY routes → ONE workflow)"
    )
    resource_identifier = Column(
        String(500),
        nullable=True,
        comment="Kong route ID (populated after deployment)"
    )
    creation_error = Column(
        String(500),
        nullable=True,
        comment="Error message if route creation/deployment failed"
    )

    # Group this path belongs to (one Kong route object = one group row).
    # Nullable during the dual-write phase: the plugins / regex_priority /
    # route_group_key columns above stay the source of truth until generation is
    # switched over, and pre-existing rows are linked by the step-2 backfill.
    kong_route_group_id = Column(
        BigInteger,
        ForeignKey("kong_route_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment="FK to kong_route_groups (MANY paths → ONE group)"
    )

    # RELATIONSHIPS
    service = relationship(
        "ServicesMstModel",
        foreign_keys=[services_mst_code]
    )
    geo_loc = relationship(
        "GeoLocMstModel",
        foreign_keys=[geo_loc_mst_code]
    )
    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        back_populates="kong_route_configs",
        foreign_keys=[gitops_workflow_id]
    )
    route_group = relationship(
        "KongRouteGroupModel",
        back_populates="routes",
        foreign_keys=[kong_route_group_id]
    )

    # Indexes
    __table_args__ = (
        Index('idx_kong_route_configs_geo_loc_mst_code', 'geo_loc_mst_code'),
    )

    def __repr__(self):
        return (
            f"<KongRouteConfig(id={self.id}, api_name='{self.api_name}', "
            f"method='{self.http_method}', route='{self.route_path}', "
            f"status='{self.creation_status}')>"
        )
