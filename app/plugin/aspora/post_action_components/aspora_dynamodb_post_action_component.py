"""
Aspora DynamoDB Post-Action Component

Runs after the DynamoDB script gen component has staged the terragrunt.hcl.
Saves the table name to variable_mst as a VARIABLE so EKS deployments can
reference it via DYNAMODB_TABLE_NAME.

The full AWS table name is already pre-computed and stored in infra.locator['table_name']
by make_infrastructure_mst_dynamodb() at record creation time:
  {tenant}-{env}-{region_code}-{index}-{identifier}

variable_type = VARIABLE, secret_provider = LOCAL (stored directly, no external backend).
"""

import logging
import uuid

logger = logging.getLogger(__name__)


class AsporaDynamoDbPostActionComponent:

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
        if case == "table_management":
            await self._handle_create(tenant=tenant, queue_dict=queue_dict, db=db)
        elif case == "delete_dynamodb_table":
            await self._handle_delete(
                tenant=tenant,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                db=db,
            )
        else:
            self.logger.warning(
                "[DynamoDBPostAction] Unknown case_ref_code=%r — skipping", case
            )

    async def _handle_create(
        self,
        tenant: str,
        queue_dict: dict,
        db,
    ) -> None:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        environment = (
            queue_dict.get("environment")
            or config_snapshot.get("environment")
            or config_snapshot.get("environment_enum", "")
        )

        if not all([db, infrastructure_mst_code, environment]):
            self.logger.warning(
                "[DynamoDBPostAction] Missing required fields — skipping. "
                f"infra_code={infrastructure_mst_code!r} env={environment!r}"
            )
            return

        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        infra_repo = InfrastructureMstRepository(db)
        infra = await infra_repo.get_by_code(infrastructure_mst_code)

        if not infra:
            self.logger.warning(
                "[DynamoDBPostAction] Infrastructure record not found: %s", infrastructure_mst_code
            )
            return

        table_name = (infra.locator or {}).get("table_name", "")
        if not table_name:
            self.logger.warning(
                "[DynamoDBPostAction] locator.table_name missing for infra_code=%s", infrastructure_mst_code
            )
            return

        await self._save_variable(
            db=db,
            tenant=tenant,
            infra=infra,
            infrastructure_mst_code=infrastructure_mst_code,
            environment=environment,
            key="DYNAMODB_TABLE_NAME",
            value=table_name,
        )

        from app.core.config import settings
        await self._save_variable(
            db=db,
            tenant=tenant,
            infra=infra,
            infrastructure_mst_code=infrastructure_mst_code,
            environment=environment,
            key="AWS_REGION",
            value=settings.onboarding_default_region,
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
            log_prefix="[DynamoDBPostAction]",
            logger=self.logger,
        )

    async def _save_variable(
        self,
        db,
        tenant: str,
        infra,
        infrastructure_mst_code: str,
        environment: str,
        key: str,
        value: str,
    ) -> None:
        from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum
        from app.integrations.secret_config_client import SecretConfigClient

        try:
            EnvironmentEnum(environment)
        except ValueError:
            self.logger.warning(f"[DynamoDBPostAction] Unknown environment '{environment}' — skipping")
            return

        application_code = infra.applications_mst_code if infra else None

        # Upsert via devlift-secret-config-manager's internal API — obs_tool no
        # longer writes variable_mst directly (variable-mst-isolation-spec).
        # skip_if_unchanged reproduces the old existing.value == value early-out.
        results = await SecretConfigClient().upsert_variables(
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            transaction_code=infrastructure_mst_code,
            environment=environment,
            tenant_code=tenant,
            items=[{
                "key": key,
                "value": value,
                "variable_type": "VARIABLE",
                "secret_provider": "local",
                "description": f"{key} for DynamoDB table {infrastructure_mst_code}",
                "metadata_json": {"application_code": application_code},
            }],
        )
        if results and results[0]["status"] == "unchanged":
            self.logger.info(
                "[DynamoDBPostAction] %s unchanged for infra_code=%s env=%s — skipping",
                key, infrastructure_mst_code, environment,
            )
            return

        self.logger.info(
            "[DynamoDBPostAction] Saved %s=%s for infra_code=%s env=%s",
            key, value, infrastructure_mst_code, environment,
        )
