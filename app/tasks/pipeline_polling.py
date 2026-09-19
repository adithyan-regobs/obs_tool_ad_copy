"""
Background task for polling pipeline run status from GitHub.

This module provides functionality to start a background task that polls
GitHub Actions API for workflow status updates and updates the database
accordingly.
"""

import asyncio
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import PipelineRunStatusEnum
from app.integrations.github_integration import GitHubIntegration
from app.repository.pipeline_run_track_repository import PipelineRunTrackRepository
from app.utils.github_status_mapper import map_github_status
from app.core.config import settings

logger = logging.getLogger(__name__)


def start_pipeline_polling(
    run_code: str,
    pipeline_code: str,
    commit_sha: str,
    owner: str,
    repo: str,
    db_session: AsyncSession,
    github_token: str,
    poll_interval_seconds: int = 15,
    max_duration_minutes: int = 60
):
    """
    Start background task to poll pipeline status from GitHub.

    This function creates an asyncio task that runs independently and
    doesn't block the caller. The task will poll GitHub API periodically
    until the workflow reaches a terminal status or times out.

    Args:
        run_code: Unique code of the pipeline run
        pipeline_code: Code of the pipeline
        commit_sha: Git commit SHA that triggered the workflow
        owner: GitHub repository owner
        repo: GitHub repository name
        db_session: Database session for updates
        github_token: GitHub Personal Access Token for API calls
        poll_interval_seconds: Seconds between polls (default: 15)
        max_duration_minutes: Maximum time to poll (default: 60)

    Example:
        >>> start_pipeline_polling(
        ...     run_code="pipeline_auth_dev_123_run_456",
        ...     pipeline_code="pipeline_auth_dev_123",
        ...     commit_sha="abc123def456",
        ...     owner="my-org",
        ...     repo="my-service",
        ...     db_session=db,
        ...     github_token="ghp_xxx"
        ... )
    """
    asyncio.create_task(
        _poll_pipeline_status(
            run_code=run_code,
            pipeline_code=pipeline_code,
            commit_sha=commit_sha,
            owner=owner,
            repo=repo,
            db_session=db_session,
            github_token=github_token,
            poll_interval_seconds=poll_interval_seconds,
            max_duration_minutes=max_duration_minutes
        )
    )
    logger.info(f"Background status polling started for run {run_code}")


async def _poll_pipeline_status(
    run_code: str,
    pipeline_code: str,
    commit_sha: str,
    owner: str,
    repo: str,
    db_session: AsyncSession,
    github_token: str,
    poll_interval_seconds: int,
    max_duration_minutes: int
):
    """
    Background task to poll GitHub and update pipeline run status.

    This task runs in the background and polls GitHub API periodically
    until the workflow completes or times out. Updates are written to
    pipeline_run_track table.

    Args:
        run_code: Unique code of the pipeline run
        pipeline_code: Code of the pipeline
        commit_sha: Git commit SHA that triggered the workflow
        owner: GitHub repository owner
        repo: GitHub repository name
        db_session: Database session
        github_token: GitHub Personal Access Token for API calls
        poll_interval_seconds: Seconds between polls
        max_duration_minutes: Maximum time to poll
    """
    max_iterations = (max_duration_minutes * 60) // poll_interval_seconds
    # Stop polling after N consecutive "not found" attempts (5 min at 15s interval = 20 attempts)
    max_not_found_attempts = 20
    consecutive_not_found = 0

    logger.info(
        f"Starting background status polling for run {run_code} "
        f"(max {max_duration_minutes}m, interval {poll_interval_seconds}s)"
    )

    # Use the provided GitHub token (fetched from pipeline_vendor via hierarchical lookup)
    if not github_token:
        logger.error("GitHub token not provided for polling")
        return

    for iteration in range(1, max_iterations + 1):
        try:
            # Wait before polling (don't poll immediately after trigger)
            await asyncio.sleep(poll_interval_seconds)

            # Query GitHub for workflow status
            workflow_run = GitHubIntegration.get_workflow_runs_by_commit(
                token=github_token,
                base_url=settings.github_base_url,
                owner=owner,
                repo=repo,
                commit_sha=commit_sha
            )

            if not workflow_run:
                consecutive_not_found += 1
                logger.debug(
                    f"Poll #{iteration}: No workflow run found yet for {run_code} "
                    f"(attempt {consecutive_not_found}/{max_not_found_attempts})"
                )
                # Stop polling if workflow never appeared after max attempts
                if consecutive_not_found >= max_not_found_attempts:
                    logger.warning(
                        f"Stopping polling for {run_code}: No workflow found after "
                        f"{consecutive_not_found} attempts (~{consecutive_not_found * poll_interval_seconds}s). "
                        f"The repository may not have a matching GitHub Actions workflow."
                    )
                    break
                continue

            # Reset counter when workflow is found
            consecutive_not_found = 0

            # Extract status info
            workflow_status = workflow_run.get("status")
            conclusion = workflow_run.get("conclusion")
            workflow_run_url = workflow_run.get("run_url")
            github_run_id = workflow_run.get("run_id")

            # Map to our status
            new_status = map_github_status(workflow_status, conclusion)

            # Update database
            await _update_run_status_in_db(
                db_session=db_session,
                run_code=run_code,
                status=new_status,
                log_url=workflow_run_url,
                github_run_id=github_run_id
            )

            logger.info(
                f"Poll #{iteration}: Updated run {run_code} → {new_status.value} "
                f"(GitHub: {workflow_status}/{conclusion})"
            )

            # Check if workflow reached terminal status
            if new_status in [
                PipelineRunStatusEnum.COMPLETED,
                PipelineRunStatusEnum.FAILED,
                PipelineRunStatusEnum.CANCELLED,
                PipelineRunStatusEnum.TIMEOUT
            ]:
                logger.info(
                    f"Run {run_code} completed with status {new_status.value}. "
                    f"Stopping polling after {iteration} iterations."
                )
                break

        except Exception as e:
            logger.error(
                f"Error polling run {run_code} (iteration {iteration}): {str(e)}",
                exc_info=True
            )
            # Continue polling even if one iteration fails
            continue

    else:
        # Loop completed without breaking (max iterations reached)
        logger.warning(
            f"Polling for run {run_code} reached max duration "
            f"({max_duration_minutes} minutes, {max_iterations} iterations)"
        )


async def _update_run_status_in_db(
    db_session: AsyncSession,
    run_code: str,
    status: PipelineRunStatusEnum,
    log_url: Optional[str] = None,
    github_run_id: Optional[str] = None
):
    """
    Update pipeline run track status in database.

    Args:
        db_session: Database session
        run_code: Pipeline run code
        status: New status to set
        log_url: Workflow run URL (optional)
        github_run_id: GitHub Actions run ID (optional)
    """
    run_track_repo = PipelineRunTrackRepository(db_session)
    await run_track_repo.update(
        code=run_code,
        status=status,
        log_url=log_url,
        github_run_id=github_run_id
    )
