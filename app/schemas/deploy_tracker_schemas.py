from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class CountBucket(BaseModel):
    key: str
    count: int


class TopService(BaseModel):
    name: str
    count: int
    completed: int
    failed: int
    # Primary resource type the service deploys to (its infrastructuretype_ref
    # code, e.g. ECS/EKS) — the FE colors each service row by it.
    resource: Optional[str] = None


class TopUser(BaseModel):
    name: str
    email: Optional[str] = None
    # user_mst.code — the FE uses it to fetch that user's deployments.
    code: Optional[str] = None
    count: int
    completed: int
    failed: int


class DeployTrackerTotals(BaseModel):
    deployments: int
    completed: int
    failed: int
    running: int
    timed_out: int


class SlackReportRequest(BaseModel):
    date_from: "datetime"
    date_to: "datetime"


class SlackReportResponse(BaseModel):
    ok: bool
    channel: str


class DeployTrackerStatsResponse(BaseModel):
    totals: DeployTrackerTotals
    # Same-length window immediately before [date_from, date_to]. Only
    # computed when ?include_previous=true — the FE shows counts without
    # deltas today, so the default skips a second full-window scan.
    previous_totals: Optional[DeployTrackerTotals] = None
    by_resource: list[CountBucket]
    by_environment: list[CountBucket]
    by_type: list[CountBucket]
    top_services: list[TopService]
    top_users: list[TopUser] = []
