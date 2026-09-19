"""
Temporal Worker — run this as a separate process alongside the FastAPI app.

Registers:
  - TenantCoordinatorWorkflow
  - DeploymentWorkflow
  - All deploy activities (run_script_pr_workflow, post_apply_comment, etc.)

Usage:
  python worker.py

Environment variables (same .env as the FastAPI app):
  TEMPORAL_HOST      (default: localhost)
  TEMPORAL_PORT      (default: 7233)
  TEMPORAL_NAMESPACE (default: default)
  TEMPORAL_TASK_QUEUE (default: deploy-queue)
  DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_BASE64, etc.
"""

import asyncio
import logging
import sys
import os

# Ensure the project root is on the path so `app.*` imports resolve
sys.path.insert(0, os.path.dirname(__file__))

from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from app.core.config import settings
from app.core.logging_config import setup_logging

# Import all DB models so SQLAlchemy registers them
import app.db.models  # noqa: F401

from app.temporal.workflows.coordinator_workflow import TenantCoordinatorWorkflow
from app.temporal.workflows.deployment_workflow import DeploymentWorkflow
from app.temporal.workflows.deployment_orchestrator_workflow import DeploymentOrchestratorWorkflow
from app.temporal.workflows.variable_deploy_workflow import VariableDeployWorkflow
from app.temporal.workflows.production_deployment_workflow import ProductionDeploymentWorkflow
from app.temporal.activities.multiple_deploy_activities import (
    prepare_multiple_deploy,
    deploy_variables_activity,
)
from app.temporal.activities.admin_alert_activities import report_stale_queue_wait
from app.temporal.activities.production_deploy_activities import (
    ensure_promotion_pr,
    merge_pr_direct,
    production_track_upsert,
    production_track_set_status,
    production_track_delete_dirs,
    evaluate_merge_gate,
    merge_promotion_pr,
    check_manual_commit_on_dirs,
    resolve_deployment_dirs,
)
from app.temporal.activities.deploy_activities import (
    run_script_pr_workflow,
    post_apply_comment,
    post_plan_comment,
    approve_pr,
    close_pr_activity,
    update_queue_status,
    update_deployment_status,
    update_resource_status,
    record_gateway_routes_pr_created,
    check_for_manual_commit,
    poll_for_plan_status,
    poll_for_apply_status,
    poll_pr_approval_status,
    poll_pr_state,
    check_and_merge_pr,
    resolve_conflict_and_merge,
    send_p0_alert,
    send_atlantis_lock_alert,
    get_waiting_users_for_dirs,
    send_lock_timeout_queued_alert,
    mark_iac_locked,
    clear_iac_locked,
    merge_secondary_pr,
    send_deployment_started_dm,
    send_deployment_completed_dm,
    update_pipeline_run_track_stage,
    update_pipeline_run_track_deploy_result,
    save_service_alb_url,
    verify_plan_with_ai,
    post_plan_verification_comment,
)

setup_logging(log_level=settings.log_level, log_file="./logs/worker.log")
logger = logging.getLogger(__name__)


async def _register_search_attributes(client) -> None:
    """
    Ensure Temporal search attributes exist for deployment workflow queries.
    Called once at worker startup — idempotent, errors are silently ignored.
    """
    from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
    from temporalio.api.enums.v1 import IndexedValueType

    attrs = {
        "DeployPrNumber":    IndexedValueType.INDEXED_VALUE_TYPE_INT,
        "DeployTenantCode":  IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "DeployUserCode":    IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        "DeployQueueCodes":  IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
        # Prod promotion PR routing: comment events resolve by
        # (DeployPrNumber, DeployProjectName) — several projects share the
        # one stage→main PR, the project name disambiguates.
        "DeployProjectName": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    }
    try:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                search_attributes=attrs,
                namespace=settings.temporal_namespace,
            )
        )
        logger.info("Temporal search attributes registered ✓")
    except Exception as exc:
        # Already registered or not supported on this Temporal version — non-fatal
        logger.debug("Search attribute registration skipped: %s", exc)


async def main():
    target = f"{settings.temporal_host}:{settings.temporal_port}"
    logger.info(f"Connecting to Temporal at {target} (namespace={settings.temporal_namespace})")

    tls = TLSConfig(domain=settings.temporal_tls_domain) if settings.temporal_tls_enabled else False
    client = await Client.connect(target, namespace=settings.temporal_namespace, tls=tls)

    await _register_search_attributes(client)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            TenantCoordinatorWorkflow,
            DeploymentWorkflow,
            DeploymentOrchestratorWorkflow,
            VariableDeployWorkflow,
            ProductionDeploymentWorkflow,
        ],
        activities=[
            prepare_multiple_deploy,
            deploy_variables_activity,
            ensure_promotion_pr,
            merge_pr_direct,
            production_track_upsert,
            production_track_set_status,
            production_track_delete_dirs,
            evaluate_merge_gate,
            merge_promotion_pr,
            check_manual_commit_on_dirs,
            resolve_deployment_dirs,
            run_script_pr_workflow,
            post_apply_comment,
            post_plan_comment,
            approve_pr,
            close_pr_activity,
            update_queue_status,
            update_deployment_status,
            update_resource_status,
            record_gateway_routes_pr_created,
            check_for_manual_commit,
            poll_for_plan_status,
            poll_for_apply_status,
            poll_pr_approval_status,
            poll_pr_state,
            check_and_merge_pr,
            resolve_conflict_and_merge,
            send_p0_alert,
            send_atlantis_lock_alert,
            get_waiting_users_for_dirs,
            send_lock_timeout_queued_alert,
            mark_iac_locked,
            clear_iac_locked,
            merge_secondary_pr,
            send_deployment_started_dm,
            send_deployment_completed_dm,
            update_pipeline_run_track_stage,
            update_pipeline_run_track_deploy_result,
            save_service_alb_url,
            verify_plan_with_ai,
            post_plan_verification_comment,
            report_stale_queue_wait,
        ],
    )

    logger.info(f"Worker started — task_queue={settings.temporal_task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())