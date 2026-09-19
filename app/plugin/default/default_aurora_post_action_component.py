"""
Default Aurora Post-Action Component

Runs after the Aurora script gen component has staged the terragrunt.hcl.
Saves Phase 1 connection variables (known at generation time) to variable_mst.

Phase 1 variables saved here:
  - DB_PORT      — 5432 (PostgreSQL) or 3306 (MySQL)
  - DB_NAME      — user-provided database name
  - DB_USERNAME  — master username (pg_admin / mysql_admin)

Phase 2 variables (only available after Terraform apply outputs):
  - DB_HOST, DB_READER_HOST, DB_PASSWORD_SECRET_ARN
  These are written by the pipeline webhook when Terraform completes.

Note: AWS manages the actual secret via manage_master_user_password = true.
      The cluster_master_user_secret_arn Terraform output is captured in Phase 2.
      No Secrets Manager interaction needed here.
"""

import logging
import uuid

from app.core.config import settings

logger = logging.getLogger(__name__)


class DefaultAuroraPostActionComponent:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        workflow_context,
        db=None,
    ) -> None:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        identifier = config_snapshot.get("db_server_name") or config_snapshot.get("identifier", "")
        infrastructuretype_ref_code = config_snapshot.get("infrastructuretype_ref_code", "")
        engine = (
            "aurora-mysql"
            if infrastructuretype_ref_code == "aurora_mysql_infrastructuretype_ref"
            else "aurora-postgresql"
        )
        database_name = config_snapshot.get("database_name") or "app_db"
        master_username = config_snapshot.get("master_username") or (
            "mysql_admin" if engine == "aurora-mysql" else "pg_admin"
        )
        infrastructure_mst_code = config_snapshot.get("infrastructure_mst_code", "")
        environment = (
            queue_dict.get("environment")
            or config_snapshot.get("environment")
            or config_snapshot.get("environment_enum", "")
        )

        if not all([db, infrastructure_mst_code, environment, identifier]):
            self.logger.warning(
                "[AuroraPostAction] Missing required fields — skipping variable save. "
                f"infra_code={infrastructure_mst_code!r} env={environment!r} id={identifier!r}"
            )
            return

        db_port = 3306 if engine == "aurora-mysql" else 5432
        phase1_vars = [
            {"key": "DB_PORT",     "value": str(db_port)},
            {"key": "DB_NAME",     "value": database_name},
            {"key": "DB_USERNAME", "value": "iam_user"},
            {"key": "AWS_REGION",  "value": settings.onboarding_default_region},
        ]

        await self._save_variables(
            db=db,
            tenant=tenant,
            infrastructure_mst_code=infrastructure_mst_code,
            environment=environment,
            variables=phase1_vars,
        )

    async def _save_variables(
        self,
        db,
        tenant: str,
        infrastructure_mst_code: str,
        environment: str,
        variables: list,
    ) -> None:
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum
        from app.integrations.secret_config_client import SecretConfigClient
        from sqlalchemy.orm.attributes import flag_modified

        try:
            EnvironmentEnum(environment)
        except ValueError:
            self.logger.warning(f"[AuroraPostAction] Unknown environment '{environment}' — skipping")
            return

        infra_repo = InfrastructureMstRepository(db)

        infra = await infra_repo.get_by_code(infrastructure_mst_code)
        application_code = infra.applications_mst_code if infra else None

        # Upsert via devlift-secret-config-manager's internal API — obs_tool no
        # longer writes variable_mst directly (variable-mst-isolation-spec).
        # match_environment=False + skip_if_unchanged=False keep the old
        # get_by_key + always-rewrite semantics; returned ids feed the
        # infra.variable_ids merge below (infrastructure_mst stays local).
        results = await SecretConfigClient().upsert_variables(
            table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
            transaction_code=infrastructure_mst_code,
            environment=environment,
            tenant_code=tenant,
            match_environment=False,
            items=[
                {
                    "key": var_def["key"],
                    "value": var_def["value"],
                    "variable_type": "VARIABLE",
                    "secret_provider": "local",
                    "description": f"{var_def['key']} for Aurora cluster {infrastructure_mst_code}",
                    "metadata_json": {"application_code": application_code},
                    "skip_if_unchanged": False,
                }
                for var_def in variables
            ],
        )
        recorded_ids: list = [r["id"] for r in results]

        if recorded_ids:
            if not infra:
                infra = await infra_repo.get_by_code(infrastructure_mst_code)
            if infra:
                existing_ids = infra.variable_ids or []
                merged = list(set(existing_ids + recorded_ids))
                infra.variable_ids = merged
                db.add(infra)
                flag_modified(infra, "variable_ids")
                await db.flush()

        self.logger.info(
            "[AuroraPostAction] Saved Phase 1 variables %s for infra_code=%s",
            [v["key"] for v in variables], infrastructure_mst_code,
        )
