"""
Shared cascade-soft-delete helper for resource post-action components.

Used by every per-resource ``_handle_delete`` to perform the same three steps:

  1. Cascade soft-delete `variable_mst` rows owned by the resource (parent +
     any rows whose `referenced_variable_id` points at one of those parents)
     in a single bulk UPDATE.
  2. Flip the resource row's `status` to `SOFT_DELETED`
     (infrastructure_mst.status or service_configs.status, depending on table).
  3. Stage `queue_status_updates[queue_code] = {"is_deleted": True}` so the
     workflow's end-of-run flush soft-deletes the queue item itself.

Idempotent — re-runs find no var rows to delete and just re-flip the status.
Validation early-returns do not stage the queue is_deleted (so a malformed
delete request can be retried after fixing the inputs).
"""

from app.core.enum import (
    EnvironmentEnum,
    ResourceStatusEnum,
    WorkflowSourceTableEnum,
)


async def cascade_soft_delete_resource(
    *,
    db,
    workflow_context,
    queue_dict: dict,
    table_name: WorkflowSourceTableEnum,
    transaction_code: str,
    log_prefix: str,
    logger,
) -> None:
    """Run the standard cascade soft-delete for a single resource.

    Args:
        db: Async SQLAlchemy session.
        workflow_context: PRWorkflowContext (for staging queue is_deleted).
        queue_dict: The queue item dict being processed.
        table_name: WorkflowSourceTableEnum.INFRASTRUCTURE for infra-side
            resources (S3, SQS, DynamoDB, Redis, K8s Postgres) or
            WorkflowSourceTableEnum.SERVICE_CONFIG for service-side resources
            (EKS service, ECS service).
        transaction_code: The resource row's primary code (infra_mst.code or
            service_configs.code).
        log_prefix: Per-component log prefix (e.g. "[SQSPostAction]").
        logger: Component logger.
    """
    environment = (
        queue_dict.get("environment")
        or (queue_dict.get("config_snapshot") or {}).get("environment")
        or (queue_dict.get("config_snapshot") or {}).get("environment_enum", "")
    )

    if not all([db, transaction_code, environment]):
        logger.warning(
            "%s Missing required fields for delete — skipping. "
            "transaction_code=%r env=%r",
            log_prefix, transaction_code, environment,
        )
        return

    try:
        EnvironmentEnum(environment)
    except ValueError:
        logger.warning(
            "%s Unknown environment '%s' — skipping delete",
            log_prefix, environment,
        )
        return

    # ── Cascade soft-delete variable_mst rows ──────────────────────────────
    # Via devlift-secret-config-manager's internal API — obs_tool no longer
    # touches variable_mst directly (variable-mst-isolation-spec). The
    # endpoint walks owner rows + referencing children, same as before.
    from app.integrations.secret_config_client import SecretConfigClient
    result = await SecretConfigClient().cascade_delete_variables(
        table_name=table_name,
        transaction_code=transaction_code,
        environment=environment,
    )

    if result.get("deleted"):
        logger.info(
            "%s Soft-deleted %s variable_mst rows for %s=%s env=%s (ids=%s)",
            log_prefix, result["deleted"], table_name.value, transaction_code,
            environment, result.get("ids"),
        )
    else:
        logger.info(
            "%s No variable_mst rows to delete for %s=%s env=%s",
            log_prefix, table_name.value, transaction_code, environment,
        )

    # ── Flip the resource row's status → SOFT_DELETED ──────────────────────
    if table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        repo = InfrastructureMstRepository(db)
    elif table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
        from app.repository.service_config_repository import ServiceConfigRepository
        repo = ServiceConfigRepository(db)
    else:
        logger.warning(
            "%s Unsupported table_name=%r for soft-delete — skipping status flip",
            log_prefix, table_name,
        )
        return

    updated = await repo.update_status(
        code=transaction_code,
        status=ResourceStatusEnum.SOFT_DELETED,
    )
    logger.info(
        "%s Marked %s=%s → status=SOFT_DELETED (rows=%s)",
        log_prefix, table_name.value, transaction_code, updated,
    )

    # ── Stage queue item is_deleted=True ───────────────────────────────────
    queue_code = queue_dict.get("code")
    if queue_code and workflow_context is not None:
        workflow_context.queue_status_updates[queue_code] = {"is_deleted": True}
