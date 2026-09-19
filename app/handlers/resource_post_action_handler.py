"""
Resource Post-Action Handler

Dispatch layer for post-script-gen actions.
Called once per queue item — post-actions are queue-level, not per-file.

Post-action components handle side-effects that are outside the scope
of script generation — e.g. saving connection variables to Secrets Manager
and variable_mst, or any future DB bookkeeping tied to resource provisioning.

Dispatch key: case_ref_code (from the queue item's case_ref_code column)

Lookup order:
  1. Tenant-specific map
  2. "default" map (fallback for all tenants)
  3. Silent no-op if no component is registered — post-actions are optional
"""

import logging
import inspect
from typing import Dict, Optional, Type

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.pr_workflow_context import PRWorkflowContext

logger = logging.getLogger(__name__)


class ResourcePostActionHandler:

    @classmethod
    def _build_map(cls) -> Dict[str, Dict[str, Type]]:
        from app.plugin.default.default_dynamodb_post_action_component import DefaultDynamoDbPostActionComponent
        from app.plugin.default.default_s3_post_action_component import DefaultS3PostActionComponent
        from app.plugin.default.default_sqs_post_action_component import DefaultSqsPostActionComponent
        from app.plugin.default.default_redis_post_action_component import DefaultRedisPostActionComponent
        from app.plugin.default.default_k8s_postgres_post_action_component import DefaultK8sPostgresPostActionComponent
        from app.plugin.default.default_eks_service_post_action_component import DefaultEksServicePostActionComponent
        from app.plugin.default.default_aurora_post_action_component import DefaultAuroraPostActionComponent
        from app.plugin.aspora.post_action_components import (
            AsporaDynamoDbPostActionComponent,
            AsporaS3PostActionComponent,
            AsporaSqsPostActionComponent,
            AsporaEksServicePostActionComponent,
        )

        # Aspora-specific delete post-actions, shared by the aspora and vance
        # tenants (same convention as script_gen_handler). Delete-only — these
        # run the same cascade-soft-delete as default; create/update flows fall
        # back to the "default" map below.
        # Redis and K8s Postgres are intentionally omitted — aspora/vance do not
        # support those resource types, so there is no delete path for them.
        aspora_delete_components: Dict[str, Type] = {
            "delete_dynamodb_table": AsporaDynamoDbPostActionComponent,
            "delete_bucket": AsporaS3PostActionComponent,
            "delete_queue": AsporaSqsPostActionComponent,
            "delete_service": AsporaEksServicePostActionComponent,
        }

        return {
            "default": {
                # DynamoDB
                "table_management": DefaultDynamoDbPostActionComponent,
                "delete_dynamodb_table": DefaultDynamoDbPostActionComponent,
                # S3
                "create_bucket": DefaultS3PostActionComponent,
                "delete_bucket": DefaultS3PostActionComponent,
                # SQS
                "create_queue": DefaultSqsPostActionComponent,
                "delete_queue": DefaultSqsPostActionComponent,
                # ElastiCache Redis
                "delete_redis": DefaultRedisPostActionComponent,
                # K8s PostgreSQL
                "delete_k8s_postgres_create_server": DefaultK8sPostgresPostActionComponent,
                # Aurora PostgreSQL / MySQL
                "create_server": DefaultAuroraPostActionComponent,
                # EKS service (SERVICE_CONFIG row, not INFRASTRUCTURE)
                "delete_service": DefaultEksServicePostActionComponent,
            },
            # Aspora / Vance use the Aspora delete components (create/update
            # still fall back to "default" via _resolve).
            "aspora": {
                **aspora_delete_components,
            },
            "vance": {
                **aspora_delete_components,
            },
        }

    @classmethod
    async def run(
        cls,
        tenant: str,
        queue_dict: dict,
        workflow_context: PRWorkflowContext,
        db: Optional[AsyncSession] = None,
    ) -> None:
        """
        Run the registered post-action component for a queue item.
        Dispatches by queue_dict["case_ref_code"] — one post-action per queue item.
        Silently skips if no component is registered.
        Post-action failures are non-fatal — logged but do not block the PR.
        """
        case_ref_code = queue_dict.get("case_ref_code") or ""
        component_cls = cls._resolve(tenant, case_ref_code)
        if component_cls is None:
            return

        component = component_cls()
        try:
            result = component.run(
                tenant=tenant,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                db=db,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(
                f"[PostAction] Component failed tenant={tenant} case_ref_code={case_ref_code}: {e}",
                exc_info=True,
            )

    @classmethod
    def _resolve(cls, tenant: str, case_ref_code: str) -> Optional[Type]:
        post_action_map = cls._build_map()
        tenant_map = post_action_map.get(tenant, {})
        component_cls = tenant_map.get(case_ref_code)
        if component_cls is None:
            component_cls = post_action_map.get("default", {}).get(case_ref_code)
        return component_cls
