"""Pydantic schemas for Observability Logs API endpoints."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Request Schemas ──────────────────────────────────────────────────

class LogsQueryParams(BaseModel):
    """Query parameters for fetching log events."""
    service_name: str = Field(..., description="Service name to filter log streams by prefix")
    start_time: datetime = Field(..., description="Start time (ISO 8601 UTC)")
    end_time: datetime = Field(..., description="End time (ISO 8601 UTC)")
    search: Optional[str] = Field(None, description="Free-text search filter")
    level: Optional[str] = Field(None, description="Log level filter: debug, info, warn, error")
    limit: int = Field(500, ge=1, le=1000, description="Max events per page (max 1000)")
    next_token: Optional[str] = Field(None, description="Pagination token from previous response")
    newest_first: bool = Field(False, description="Return newest events first (uses provider-specific reverse query)")


class LogStreamsQueryParams(BaseModel):
    """Query parameters for listing log streams (pods)."""
    service_name: Optional[str] = Field(None, description="Filter streams by service name prefix")


class LogVolumeQueryParams(BaseModel):
    """Query parameters for log volume histogram."""
    service_name: str = Field(..., description="Service name")
    start_time: datetime = Field(..., description="Start time (ISO 8601 UTC)")
    end_time: datetime = Field(..., description="End time (ISO 8601 UTC)")
    interval_minutes: int = Field(5, ge=1, le=60, description="Bucket interval in minutes")


# ── Response Schemas ─────────────────────────────────────────────────

class LogEvent(BaseModel):
    """Single log event from CloudWatch."""
    timestamp: int = Field(..., description="Epoch milliseconds")
    message: str = Field(..., description="Log line content")
    log_stream_name: str = Field(..., description="Pod name (log stream)")
    event_id: str = Field(default="", description="Unique event identifier")


class LogEventsResponse(BaseModel):
    """Paginated log events response."""
    events: list[LogEvent] = Field(default_factory=list)
    next_token: Optional[str] = Field(None, description="Token for fetching next page")
    has_older: bool = Field(False, description="Whether older logs exist beyond this page")
    oldest_timestamp: Optional[int] = Field(None, description="Epoch ms of oldest event in this page — cursor for loading older")


class LogStream(BaseModel):
    """A log stream (pod) in a log group."""
    log_stream_name: str = Field(..., description="Pod name")
    last_event_timestamp: int = Field(0, description="Last log event epoch ms")
    first_event_timestamp: int = Field(0, description="First log event epoch ms")


class LogStreamsResponse(BaseModel):
    """List of log streams (pods) for a service."""
    streams: list[LogStream] = Field(default_factory=list)


class LogVolumeEntry(BaseModel):
    """A single bucket in the log volume histogram."""
    timestamp: str = Field(..., description="Bucket start time")
    count: int = Field(..., description="Number of log events in this bucket")


class LogVolumeResponse(BaseModel):
    """Log volume over time for histogram visualization."""
    volume: list[LogVolumeEntry] = Field(default_factory=list)


class LogsFeatureStatusResponse(BaseModel):
    """Status of the logs feature."""
    enabled: bool = Field(..., description="Whether CloudWatch Logs is enabled")
    healthy: bool = Field(False, description="Whether CloudWatch Logs is reachable")
