from sqlalchemy import Column, String, Index
from app.db.models.base_model import BaseModel
from app.core.enum import ProductionDeploymentStatusEnum
from sqlalchemy import Enum as SqlEnum


class ProductionDeploymentTrackModel(BaseModel):
    """
    One row per prod terragrunt dir that is somewhere between "merged to stage"
    and "merged to main" — the merge gate's source of truth for the shared
    stage→main promotion PR.

    Lifecycle (written by ProductionDeploymentWorkflow):
      IN_PROGRESS  — content merged to stage, not applied yet (row inserted
                     BEFORE the feature PR merges, so "in the promotion diff
                     with no row" reliably means non-DevLift/manual content)
      APPLIED      — apply succeeded; live in AWS, safe to merge into main
      RESUMING     — a parked deploy's manual plan succeeded and it is queued
                     again for apply (still unapplied — still blocks merges,
                     but alerts read "recovering", not "failed")
      FAILED       — deploy terminated/orphaned with content unapplied on
                     stage; blocks merges loudly until a redeploy succeeds

    Rows are DELETED when the promotion PR containing their dirs merges.
    A dir has at most one live row — a new deploy of the same dir claims the
    existing row (same-dir deploys are serialized by the coordinator locks).
    """

    __tablename__ = "production_deployment_track"

    # The terragrunt project dir on stage/main, e.g.
    # "environment/core-prod-01/eu-west-2/services/service-1"
    dir = Column(String(512), nullable=False, unique=True)

    # The ProductionDeploymentWorkflow currently claiming this dir — used by
    # the merge gate's liveness check (row IN_PROGRESS/RESUMING but workflow
    # not Running → orphan → flip to FAILED).
    workflow_id = Column(String(255), nullable=False)

    tenant_code = Column(String(100), nullable=False)
    repo_full_name = Column(String(200), nullable=False)

    status = Column(
        SqlEnum(ProductionDeploymentStatusEnum, name="production_deployment_status_enum"),
        nullable=False,
        default=ProductionDeploymentStatusEnum.IN_PROGRESS,
    )

    __table_args__ = (
        Index("ix_production_deployment_track_repo", "repo_full_name"),
    )
