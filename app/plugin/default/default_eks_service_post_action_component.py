"""
Default EKS Service Post-Action Component

Dispatches on ``queue_dict["case_ref_code"]``:

  delete_service  →  cascade-soft-delete variable_mst rows owned by the
                     service_config, flip service_configs.status to
                     SOFT_DELETED, stage queue is_deleted=True. Delegates to
                     the shared cascade helper. DB-only soft-delete — does NOT
                     remove deployed K8s resources or alter the infra repo.

The create / update cases (``update_service``) flow through the existing
EKS workflow in script_pr_workflow_service; no post-action work happens at
that point.

NOTE: For EKS service, the queue's ``transaction_code`` is the
``service_configs.code`` (table_name = SERVICE_CONFIG), unlike infra-side
post-actions where it's ``infrastructure_mst.code`` (table_name =
INFRASTRUCTURE).
"""

import logging


class DefaultEksServicePostActionComponent:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        workflow_context,
        db=None,
    ) -> None:
        case = queue_dict.get("case_ref_code") or ""
        if case == "delete_service":
            await self._handle_delete(
                tenant=tenant,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                db=db,
            )
        else:
            self.logger.warning(
                "[EksServicePostAction] Unknown case_ref_code=%r — skipping", case
            )

    async def _handle_delete(
        self,
        tenant: str,
        queue_dict: dict,
        workflow_context,
        db,
    ) -> None:
        from app.core.enum import WorkflowSourceTableEnum
        from app.plugin.default._infra_delete_shared import cascade_soft_delete_resource

        # For service_configs the row code IS the queue's transaction_code,
        # not something inside config_snapshot.
        service_config_code = queue_dict.get("transaction_code") or ""
        await cascade_soft_delete_resource(
            db=db,
            workflow_context=workflow_context,
            queue_dict=queue_dict,
            table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
            transaction_code=service_config_code,
            log_prefix="[EksServicePostAction]",
            logger=self.logger,
        )
