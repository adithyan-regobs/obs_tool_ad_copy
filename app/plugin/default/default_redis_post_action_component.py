"""
Default ElastiCache Redis Post-Action Component

Dispatches on ``queue_dict["case_ref_code"]``:

  delete_redis  →  cascade-soft-delete variable_mst rows owned by the infra,
                   flip infrastructure_mst.status to SOFT_DELETED, stage queue
                   is_deleted=True. Delegates to the shared cascade helper.

create_redis is currently a no-op at the post-action layer (Redis didn't
have a post-action component before this; create-time variable seeding
can be added later if needed).
"""

import logging


class DefaultRedisPostActionComponent:

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
        if case == "delete_redis":
            await self._handle_delete(
                tenant=tenant,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                db=db,
            )
        else:
            self.logger.warning(
                "[RedisPostAction] Unknown case_ref_code=%r — skipping", case
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

        infrastructure_mst_code = (queue_dict.get("config_snapshot") or {}).get(
            "infrastructure_mst_code", ""
        )
        await cascade_soft_delete_resource(
            db=db,
            workflow_context=workflow_context,
            queue_dict=queue_dict,
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            transaction_code=infrastructure_mst_code,
            log_prefix="[RedisPostAction]",
            logger=self.logger,
        )
