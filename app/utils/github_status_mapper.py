"""
GitHub workflow status mapping utilities.

Maps GitHub Actions workflow statuses and conclusions to our internal
pipeline run status enumeration.
"""

from typing import Optional
from app.core.enum import PipelineRunStatusEnum


def map_github_status(
    workflow_status: str,
    conclusion: Optional[str] = None
) -> PipelineRunStatusEnum:
    """
    Map GitHub workflow status to pipeline run status.

    GitHub provides two pieces of information:
    - status: Current state of the workflow (queued, in_progress, completed)
    - conclusion: Final result if completed (success, failure, cancelled, timed_out, skipped)

    Args:
        workflow_status: GitHub workflow status
        conclusion: GitHub workflow conclusion (only relevant when status=completed)

    Returns:
        Mapped PipelineRunStatusEnum

    Examples:
        >>> map_github_status("queued")
        <PipelineRunStatusEnum.PENDING: 'PENDING'>

        >>> map_github_status("in_progress")
        <PipelineRunStatusEnum.RUNNING: 'RUNNING'>

        >>> map_github_status("completed", "success")
        <PipelineRunStatusEnum.COMPLETED: 'COMPLETED'>

        >>> map_github_status("completed", "failure")
        <PipelineRunStatusEnum.FAILED: 'FAILED'>
    """
    if workflow_status == "in_progress":
        return PipelineRunStatusEnum.RUNNING

    elif workflow_status == "completed":
        # Check conclusion for completed runs
        if conclusion == "success":
            return PipelineRunStatusEnum.COMPLETED
        elif conclusion == "failure":
            return PipelineRunStatusEnum.FAILED
        elif conclusion == "cancelled":
            return PipelineRunStatusEnum.CANCELLED
        elif conclusion == "timed_out":
            return PipelineRunStatusEnum.TIMEOUT
        else:
            # For other conclusions (skipped, action_required, etc.)
            return PipelineRunStatusEnum.COMPLETED

    elif workflow_status == "queued":
        return PipelineRunStatusEnum.PENDING

    else:
        # Unknown status, default to PENDING
        return PipelineRunStatusEnum.PENDING
