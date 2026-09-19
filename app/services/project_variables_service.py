"""
Project Variables Service

Manages canvas variables. Metadata is stored in variable_mst.
Actual secret values live in AWS Secrets Manager (cross-account via AssumeRole).
"""

import asyncio
import logging
from typing import Dict, List
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import (
    EnvironmentEnum,
    InfraVendorEnum,
    SecretProviderEnum,
    VariableDataTypeEnum,
    VariableScopeTypeEnum,
    VariableTypeEnum,
    WorkflowSourceTableEnum,
)
from app.handlers.environment_variable_handler import EnvironmentVariableHandler
from app.integrations.aws_integration import AWSIntegration
from app.services.locator_variable_mapper import map_locator_to_values
from app.repository.variable_mst_repository import VariableMstRepository
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.repository.applications_mst_repository import ApplicationsMstRepository
from app.schemas.secrets_parameters_schemas import (
    CreateCanvasVariableRequest,
    UpdateCanvasVariableRequest,
    CreateCanvasVariableRefRequest,
    CanvasVariableResponse,
    CanvasVariableWithValueResponse,
    CanvasVariableRefResponse,
    BulkCanvasVariablesResponse,
    BulkUpsertVariablesRequest,
    BulkUpsertVariablesResponse,
)

logger = logging.getLogger(__name__)

# Map incoming resource_type_str to WorkflowSourceTableEnum.
# Accepts both the short canvas aliases (e.g. "service") and the canonical
# enum values (e.g. "SERVICE_CONFIG", "INFRASTRUCTURE") directly.
_RESOURCE_TYPE_TO_TABLE = {
    "service": WorkflowSourceTableEnum.SERVICE_CONFIG,
    "SERVICE_CONFIG": WorkflowSourceTableEnum.SERVICE_CONFIG,
    "INFRASTRUCTURE": WorkflowSourceTableEnum.INFRASTRUCTURE,
}
_DEFAULT_TABLE = WorkflowSourceTableEnum.INFRASTRUCTURE


def _resolve_table_name(resource_type_str: str) -> WorkflowSourceTableEnum:
    """Map a canvas resource type string to the polymorphic table_name enum."""
    return _RESOURCE_TYPE_TO_TABLE.get(resource_type_str, _DEFAULT_TABLE)


class ProjectVariablesService:
    """
    Service layer for canvas project variables.
    Metadata stored in variable_mst, values in AWS Secrets Manager.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.var_repo = VariableMstRepository(db)
        self.vendor_accounts_repo = InfraVendorAccountsMstRepository(db)
        self.app_repo = ApplicationsMstRepository(db)

    # ==================== PRIVATE HELPERS ====================

    async def _resolve_application_code(
        self, resource_code: str, table_name: WorkflowSourceTableEnum
    ) -> str | None:
        """Resolve the owning application_code for a resource, for authz.

        The value-bearing readers only receive a ``resource_code`` (a
        service_configs.code or infrastructure_mst.code), but workspace access is
        checked per application. Map the resource back to its application:
          - INFRASTRUCTURE -> infrastructure_mst.applications_mst_code
          - SERVICE_CONFIG  -> service_configs -> services_mst.applications_mst_code
        Returns None when the resource can't be resolved (unknown code / other
        table), which the caller treats as "deny".
        """
        from sqlalchemy import select

        if table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
            from app.db.models.infrastructure_mst_model import InfrastructureMstModel as I
            stmt = select(I.applications_mst_code).where(I.code == resource_code)
        elif table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
            from app.db.models.service_config_model import ServiceConfigModel as SC
            from app.db.models.services_mst_model import ServicesMstModel as S
            stmt = (
                select(S.applications_mst_code)
                .select_from(SC)
                .join(S, SC.services_mst_code == S.code)
                .where(SC.code == resource_code)
            )
        else:
            return None

        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _verify_resource_access(
        self,
        resource_code: str,
        table_name: WorkflowSourceTableEnum,
        user_code: str | None,
        tenant_code: str,
    ) -> bool:
        """Workspace access gate for a resource's variables (read side).

        Resolves the resource's application and defers to
        ``WorkspaceService.verify_app_workspace_access``. Denies (returns False)
        when the caller is unauthenticated or the resource has no resolvable
        application — never fails open. Mirrors the check the service-config /
        infrastructure loaders already enforce for the Settings tab.
        """
        if not user_code:
            return False
        application_code = await self._resolve_application_code(resource_code, table_name)
        if not application_code:
            logger.warning(
                "variable access: no application for resource '%s' (%s) — deny",
                resource_code, table_name.value,
            )
            return False
        from app.services.workspace_service import WorkspaceService
        return await WorkspaceService(self.db).verify_app_workspace_access(
            user_code, tenant_code, application_code
        )

    async def _get_auth_config(
        self,
        application_code: str,
        environment: EnvironmentEnum,
        tenant_code: str,
    ) -> Dict:
        """
        Get AWS auth_config via hierarchical vendor account lookup.
        Returns the auth_config dict (contains assume_role_arn for Account B).
        """
        vendor_account = await self.vendor_accounts_repo.get_by_hierarchy(
            infra_vendor_enum=InfraVendorEnum.aws,
            environments_enum=environment,
            application_code=application_code,
            tenant_code=tenant_code,
        )

        if not vendor_account:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No AWS vendor account configured for application '{application_code}' "
                    f"in '{environment.value}' environment. "
                    f"Please configure an AWS vendor account in Infrastructure Settings."
                ),
            )

        return vendor_account.auth_config

    @staticmethod
    def _strip_service_suffix(name: str) -> str:
        """Strip trailing '-service' suffix to match K8s/deploy naming conventions."""
        if name.lower().endswith("-service"):
            return name[:-8]
        return name

    def _build_secret_path(
        self,
        tenant_code: str,
        application_name: str,
        environment: str,
        resource_name: str,
        transaction_code: str,
    ) -> str:
        """Build the AWS Secrets Manager path for a resource's consolidated secret."""
        resource_name = self._strip_service_suffix(resource_name)
        transaction_code = self._strip_service_suffix(transaction_code)
        return f"{tenant_code}/{application_name}/{environment}/{resource_name}/{transaction_code}"

    async def _get_or_create_resource_secret(
        self,
        auth_config: dict,
        secret_path: str,
        table_name: WorkflowSourceTableEnum,
        transaction_code: str,
        environment: EnvironmentEnum,
        resource_name: str,
        tenant_code: str,
        application_code: str,
    ) -> tuple:
        """
        Get existing consolidated secret for a resource, or create a new empty one.
        Returns (arn, current_dict) where current_dict is the parsed JSON object.
        """
        existing_records = await self.var_repo.get_all_by_transaction(
            table_name=table_name,
            transaction_code=transaction_code,
            environment=environment,
        )

        # Filter to only secret-type records that have an ARN
        secret_records = [
            r for r in existing_records
            if r.variable_type == VariableTypeEnum.SECRET and r.variable_cloud_identifier
        ]

        if secret_records:
            arn = secret_records[0].variable_cloud_identifier
            try:
                aws_secret = await AWSIntegration.get_secret(
                    auth_config=auth_config,
                    secret_name=arn,
                )
                value = aws_secret["value"]
                current_dict = value if isinstance(value, dict) else {}
                # Idempotent re-grant: covers secrets created before this hook
                # existed, and is a no-op once the ARN is already in the policy.
                await self._grant_default_role_secret_access(
                    tenant_code=tenant_code, environment=environment, secret_arn=arn,
                )
                return arn, current_dict
            except Exception as e:
                error_msg = str(e).lower()
                if "not found" in error_msg or "scheduled for deletion" in error_msg:
                    pass  # Secret deleted in AWS — create a fresh one
                else:
                    raise

        # No existing secret — create a new empty one
        clean_name = self._strip_service_suffix(resource_name)
        tags = [
            {"Key": "tenant", "Value": tenant_code},
            {"Key": "application", "Value": application_code},
            {"Key": "environment", "Value": environment.value},
            {"Key": "resource", "Value": clean_name},
        ]

        try:
            aws_response = await AWSIntegration.create_secret(
                auth_config=auth_config,
                secret_name=secret_path,
                secret_value={},
                description=f"Canvas variables for {clean_name}",
                tags=tags,
            )
            await self._grant_default_role_secret_access(
                tenant_code=tenant_code, environment=environment, secret_arn=aws_response["arn"],
            )
            return aws_response["arn"], {}
        except Exception as e:
            if "already exists" in str(e).lower():
                # Secret exists in AWS but DB records were deleted — adopt it.
                logger.info(f"Secret already exists at {secret_path}, adopting existing")
                aws_secret = await AWSIntegration.get_secret(
                    auth_config=auth_config,
                    secret_name=secret_path,
                )
                value = aws_secret["value"]
                current_dict = value if isinstance(value, dict) else {}
                await self._grant_default_role_secret_access(
                    tenant_code=tenant_code, environment=environment, secret_arn=aws_secret["arn"],
                )
                return aws_secret["arn"], current_dict
            raise

    async def _grant_default_role_secret_access(
        self, *, tenant_code: str, environment: EnvironmentEnum, secret_arn: str,
    ) -> None:
        """Append ``secret_arn`` to the tenant's default-role ``custom_policy_json``.

        Non-blocking: a failure here is logged but does not fail the secret
        create. The apply is idempotent, so it's safe to retry via any later
        secret create for the same tenant.
        """
        from app.plugin.default.default_aws_role_gen_component import apply_policy_direct
        try:
            result = await apply_policy_direct(
                tenant_code=tenant_code,
                environment=environment.value,
                kind_name="secrets-read",
                resource_arn=secret_arn,
                operation="add",
                db=self.db,
            )
            if result.get("changed"):
                logger.info(
                    "Granted default role access to %s (commit=%s)",
                    secret_arn, result.get("commit_sha"),
                )
        except Exception as exc:
            logger.warning(
                "Failed to grant default role access to secret %s (non-blocking): %s",
                secret_arn, exc,
            )

    async def sync_default_role_for_transaction(
        self,
        *,
        tenant_code: str,
        table_name: WorkflowSourceTableEnum,
        transaction_code: str,
        environment: EnvironmentEnum,
    ) -> None:
        """Re-grant the tenant default-role for every SECRET variable owned by
        ``(table_name, transaction_code, environment)``.

        Heals drifted ``TenantDefaultSecretsRead`` on redeploys: the original
        grant runs at secret-create time; a redeploy that doesn't touch
        variables won't re-run it, so if the tenant's default-role
        ``terragrunt.hcl`` lost the statement the pod fails CSI mount with
        AccessDenied. ``_grant_default_role_secret_access`` is idempotent, so
        calling this on every PR workflow is a no-op when the policy is
        already correct.
        """
        records = await self.var_repo.get_all_by_transaction(
            table_name=table_name,
            transaction_code=transaction_code,
            environment=environment,
        )
        seen: set = set()
        for record in records:
            if record.variable_type != VariableTypeEnum.SECRET:
                continue
            arn = record.variable_cloud_identifier
            if not arn or arn in seen:
                continue
            seen.add(arn)
            await self._grant_default_role_secret_access(
                tenant_code=tenant_code, environment=environment, secret_arn=arn,
            )

    async def _get_record_or_404(self, variable_id: int, tenant_code: str):
        """Fetch a record by ID and verify tenant ownership."""
        record = await self.var_repo.get_by_id(variable_id)
        if not record or record.is_deleted:
            raise HTTPException(status_code=404, detail="Variable not found")
        if record.tenants_mst_code != tenant_code:
            raise HTTPException(status_code=404, detail="Variable not found")
        return record

    def _to_response(self, record) -> CanvasVariableResponse:
        """Map a variable_mst record to CanvasVariableResponse."""
        return CanvasVariableResponse(
            id=record.id,
            code=record.code,
            transaction_code=record.transaction_code,
            table_name=record.table_name.value if record.table_name else "",
            name=record.key,
            type=record.variable_type.value.lower() if record.variable_type else "secret",
            secret_arn=record.variable_cloud_identifier,
            referenced_variable_id=record.referenced_variable_id,
            environment=record.environments_enum.value if record.environments_enum else "",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    # ==================== PUBLIC METHODS ====================

    async def _resolve_variable_value(
        self,
        source_record,
        auth_config: dict,
    ) -> str:
        """Fetch the value of a source variable from its secret provider (AWS etc.)."""
        if not source_record.variable_cloud_identifier:
            raise HTTPException(
                status_code=400,
                detail="Source variable has no secret stored — cannot reference it",
            )

        aws_secret = await AWSIntegration.get_secret(
            auth_config=auth_config,
            secret_name=source_record.variable_cloud_identifier,
        )
        secret_value = aws_secret["value"]
        if isinstance(secret_value, dict):
            return secret_value.get(source_record.key, "")
        return str(secret_value)

    async def create_variable(
        self,
        request: CreateCanvasVariableRequest,
        tenant_code: str,
    ) -> CanvasVariableResponse:
        """
        Create a new canvas variable or variable reference.
        - If referenced_variable_id is set: fetch value from the source variable's
          secret provider, then create a new secret entry for the target resource.
        - If variable_value is set: use the provided value directly.
        Both paths store the value in AWS and metadata in variable_mst.
        """
        environment = EnvironmentEnum(request.environment)
        table_name = _resolve_table_name(request.table_name)

        # Duplicate check
        existing = await self.var_repo.get_by_transaction_and_key(
            table_name=table_name,
            transaction_code=request.transaction_code,
            key=request.variable_name,
            environment=environment,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Variable '{request.variable_name}' already exists on this resource",
            )

        # Get cross-account auth config
        auth_config = await self._get_auth_config(
            application_code=request.application_code,
            environment=environment,
            tenant_code=tenant_code,
        )

        # Resolve the value — either from source variable or from request
        referenced_variable_id = request.referenced_variable_id
        if referenced_variable_id is not None:
            # Fetch source variable and its value from AWS
            source_record = await self.var_repo.get_by_id(referenced_variable_id)
            if not source_record or source_record.is_deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"Source variable {referenced_variable_id} not found",
                )
            try:
                variable_value = await self._resolve_variable_value(source_record, auth_config)
            except PermissionError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except Exception as e:
                logger.error(f"Failed to resolve source variable value: {e}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch source variable value: {str(e)}",
                )
        elif request.variable_value is not None:
            variable_value = request.variable_value
        else:
            raise HTTPException(
                status_code=400,
                detail="Either variable_value or referenced_variable_id must be provided",
            )

        # Resolve application name for AWS secret path
        app_record = await self.app_repo.get_by_code(request.application_code)
        application_name = app_record.name if app_record else request.application_code

        # Build resource-level AWS path — use resource_name for human-readable
        # secret paths (e.g. "rgb1/my-app/stage/my-service/modelhosting").
        resource_name = request.resource_name or request.transaction_code
        secret_path = self._build_secret_path(
            tenant_code=tenant_code,
            application_name=application_name,
            environment=request.environment,
            resource_name=resource_name,
            transaction_code=resource_name,
        )

        try:
            # Get or create the consolidated secret for this resource
            arn, current_dict = await self._get_or_create_resource_secret(
                auth_config=auth_config,
                secret_path=secret_path,
                table_name=table_name,
                transaction_code=request.transaction_code,
                environment=environment,
                resource_name=request.variable_name,
                tenant_code=tenant_code,
                application_code=request.application_code,
            )

            # Add the key-value pair to the consolidated secret
            current_dict[request.variable_name] = variable_value

            # Update the secret in AWS with the new consolidated value
            await AWSIntegration.update_secret(
                auth_config=auth_config,
                secret_name=arn,
                secret_value=current_dict,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            logger.error(f"Failed to create variable in AWS: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to create variable in AWS: {str(e)}",
            )

        # Create metadata record in variable_mst
        db_record = await self.var_repo.create(
            code=str(uuid4()),
            name=request.variable_name,
            description=f"Canvas variable for {request.transaction_code}",
            key=request.variable_name,
            value=None,
            variable_type=VariableTypeEnum.SECRET,
            secret_provider=SecretProviderEnum.AWS_SECRETS_MANAGER,
            variable_cloud_identifier=arn,
            referenced_variable_id=referenced_variable_id,
            scope_type=VariableScopeTypeEnum.INFRA,
            table_name=table_name,
            transaction_code=request.transaction_code,
            environments_enum=environment,
            tenants_mst_code=tenant_code,
            data_type=VariableDataTypeEnum.string,
            metadata_json={
                "full_resource_path": secret_path,
                "application_code": request.application_code,
            },
        )

        return self._to_response(db_record)

    async def bulk_create_variable_refs(
        self,
        request: "BulkCreateRefsRequest",
        tenant_code: str,
    ) -> "BulkCreateRefsResponse":
        """Frontend-facing bulk wrapper. Resolves each source variable's value
        from AWS, then calls bulk_create_referenced_variables for one read + one
        write into the target's consolidated secret.

        Same path format + variable_mst shape as create_variable. Merges into
        the existing secret (preserves keys from previous calls).
        """
        from app.schemas.secrets_parameters_schemas import (
            BulkCreateRefsResponse,
        )

        from app.core.enum import SecretProviderEnum

        environment = EnvironmentEnum(request.environment)

        # Split refs into LOCAL (DB-only) vs AWS-backed.
        ref_records: list[tuple] = []        # (ref, source_record) — AWS only
        local_ref_records: list[tuple] = []  # (ref, source_record) — LOCAL only
        for ref in request.items:
            source_record = await self.var_repo.get_by_id(ref.referenced_variable_id)
            if not source_record or source_record.is_deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"Source variable {ref.referenced_variable_id} not found",
                )
            if source_record.is_write_only:
                # A reference copies the source's plaintext into the target's
                # store, where it IS readable — that would launder a write-only
                # secret into a readable one.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{source_record.key}' is write-only and cannot be referenced "
                        f"from another resource"
                    ),
                )
            if source_record.secret_provider == SecretProviderEnum.LOCAL:
                local_ref_records.append((ref, source_record))
                continue
            if not source_record.variable_cloud_identifier:
                raise HTTPException(
                    status_code=400,
                    detail=f"Source variable '{ref.target_name}' has no secret stored",
                )
            ref_records.append((ref, source_record))

        # Auth only needed when there are AWS-backed refs.
        target_auth_config = None
        if ref_records:
            target_auth_config = await self._get_auth_config(
                application_code=request.application_code,
                environment=environment,
                tenant_code=tenant_code,
            )

        # Fetch each unique AWS secret ARN once.
        arn_to_secret: dict[str, dict] = {}
        unique_arns = {r.variable_cloud_identifier for _, r in ref_records}
        for arn in unique_arns:
            try:
                aws_secret = await AWSIntegration.get_secret(
                    auth_config=target_auth_config,
                    secret_name=arn,
                )
                value = aws_secret["value"]
                arn_to_secret[arn] = value if isinstance(value, dict) else {}
            except PermissionError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except Exception as e:
                logger.error(f"Failed to fetch secret {arn}: {e}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch source secret: {str(e)}",
                )

        # Resolve each AWS ref's value from the cached secret dict.
        items: list[dict] = []
        for ref, source_record in ref_records:
            secret_dict = arn_to_secret[source_record.variable_cloud_identifier]
            value = secret_dict.get(source_record.key, "")
            items.append({
                "target_name": ref.target_name,
                "value": value,
                "referenced_variable_id": ref.referenced_variable_id,
            })

        all_variables: list = []

        # LOCAL refs — DB only, no AWS interaction.
        if local_ref_records:
            local_variables = await self._create_local_variable_refs(
                environment_str=request.environment,
                transaction_code=request.transaction_code,
                table_name_str=request.table_name,
                tenant_code=tenant_code,
                application_code=request.application_code,
                ref_records=local_ref_records,
            )
            all_variables.extend(local_variables)

        # AWS refs — 1 read + 1 write.
        if items:
            aws_variables = await self.bulk_create_referenced_variables(
                application_code=request.application_code,
                environment_str=request.environment,
                transaction_code=request.transaction_code,
                table_name_str=request.table_name,
                resource_name=request.resource_name,
                tenant_code=tenant_code,
                items=items,
            )
            all_variables.extend(aws_variables)

        return BulkCreateRefsResponse(created=len(all_variables), variables=all_variables)

    async def _create_local_variable_refs(
        self,
        *,
        environment_str: str,
        transaction_code: str,
        table_name_str: str,
        tenant_code: str,
        application_code: str,
        ref_records: list[tuple],
    ) -> list["CanvasVariableResponse"]:
        """DB-only path for LOCAL-provider refs.

        Creates a variable_mst row for each ref pointing back to the source via
        referenced_variable_id. No value stored — callers read it from the source row.
        No AWS Secrets Manager interaction.
        """
        environment = EnvironmentEnum(environment_str)
        table_name = _resolve_table_name(table_name_str)
        results = []

        for ref, source_record in ref_records:
            target_name = ref.target_name

            existing = await self.var_repo.get_by_transaction_and_key(
                table_name=table_name,
                transaction_code=transaction_code,
                key=target_name,
                environment=environment,
            )
            if existing:
                results.append(self._to_response(existing))
                continue

            db_record = await self.var_repo.create(
                code=str(uuid4()),
                name=target_name,
                description=f"Canvas variable for {transaction_code}",
                key=target_name,
                value=None,
                variable_type=VariableTypeEnum.VARIABLE,
                secret_provider=SecretProviderEnum.LOCAL,
                referenced_variable_id=ref.referenced_variable_id,
                scope_type=VariableScopeTypeEnum.INFRA,
                table_name=table_name,
                transaction_code=transaction_code,
                environments_enum=environment,
                tenants_mst_code=tenant_code,
                data_type=VariableDataTypeEnum.string,
                metadata_json={"application_code": application_code},
            )
            results.append(self._to_response(db_record))

        await self.db.flush()
        return results

    async def bulk_create_referenced_variables(
        self,
        *,
        application_code: str,
        environment_str: str,
        transaction_code: str,
        table_name_str: str,
        resource_name: str,
        tenant_code: str,
        items: list[dict],
    ) -> list[CanvasVariableResponse]:
        """Bulk-create service variables with resolved values and reference IDs.

        Same storage pattern as create_variable (path format, variable_type=SECRET,
        referenced_variable_id preserved) but batched into 1 AWS read + 1 AWS write
        regardless of item count.

        Each item in `items` is:
            {"target_name": str, "value": str, "referenced_variable_id": int | None}

        Idempotent: pre-existing target rows (by key + environment) are soft-deleted
        before creating new ones.
        """
        environment = EnvironmentEnum(environment_str)
        table_name = _resolve_table_name(table_name_str)

        auth_config = await self._get_auth_config(
            application_code=application_code,
            environment=environment,
            tenant_code=tenant_code,
        )

        app_record = await self.app_repo.get_by_code(application_code)
        application_name = app_record.name if app_record else application_code

        # Path matches create_variable: resource_name used for both slots.
        secret_path = self._build_secret_path(
            tenant_code=tenant_code,
            application_name=application_name,
            environment=environment_str,
            resource_name=resource_name,
            transaction_code=resource_name,
        )

        # 1 AWS read — get or create the service's consolidated secret.
        arn, current_dict = await self._get_or_create_resource_secret(
            auth_config=auth_config,
            secret_path=secret_path,
            table_name=table_name,
            transaction_code=transaction_code,
            environment=environment,
            resource_name=resource_name,
            tenant_code=tenant_code,
            application_code=application_code,
        )

        # Add new keys to the existing consolidated dict — preserves keys added
        # by previous calls (e.g. postgres vars stay when S3 vars are added later).
        for item in items:
            current_dict[item["target_name"]] = item["value"]

        # 1 AWS write.
        await AWSIntegration.update_secret(
            auth_config=auth_config,
            secret_name=arn,
            secret_value=current_dict,
        )

        # Create variable_mst rows — same shape as create_variable.
        results = []
        for item in items:
            target_name = item["target_name"]
            ref_id = item.get("referenced_variable_id")

            # Idempotency: soft-delete any existing row with the same key.
            existing = await self.var_repo.get_by_transaction_and_key(
                table_name=table_name,
                transaction_code=transaction_code,
                key=target_name,
                environment=environment,
            )
            if existing:
                await self.var_repo.soft_delete(existing.id)

            db_record = await self.var_repo.create(
                code=str(uuid4()),
                name=target_name,
                description=f"Canvas variable for {transaction_code}",
                key=target_name,
                value=None,
                variable_type=VariableTypeEnum.SECRET,
                secret_provider=SecretProviderEnum.AWS_SECRETS_MANAGER,
                variable_cloud_identifier=arn,
                referenced_variable_id=ref_id,
                scope_type=VariableScopeTypeEnum.INFRA,
                table_name=table_name,
                transaction_code=transaction_code,
                environments_enum=environment,
                tenants_mst_code=tenant_code,
                data_type=VariableDataTypeEnum.string,
                metadata_json={
                    "full_resource_path": secret_path,
                    "application_code": application_code,
                },
            )
            results.append(self._to_response(db_record))

        await self.db.flush()
        return results

    async def get_variable_value(
        self,
        variable_id: int,
        tenant_code: str,
    ) -> CanvasVariableWithValueResponse:
        """
        Fetch a variable's value on-demand from the consolidated AWS secret.
        Parses the JSON object and returns only the requested key's value.
        """
        record = await self._get_record_or_404(variable_id, tenant_code)

        # The on-demand reveal is the one endpoint whose whole purpose is to hand
        # back a plaintext value, so it is also the one that must refuse.
        if record.is_write_only:
            raise HTTPException(
                status_code=403,
                detail=f"'{record.key}' is write-only — its value cannot be read back",
            )

        # LOCAL-provider variables: value lives in DB (optionally via referenced_variable)
        if record.secret_provider == SecretProviderEnum.LOCAL:
            value = record.value
            if value is None and record.referenced_variable:
                value = record.referenced_variable.value or ""
            return CanvasVariableWithValueResponse(
                id=record.id,
                code=record.code,
                transaction_code=record.transaction_code,
                table_name=record.table_name.value if record.table_name else "",
                name=record.key,
                type=record.variable_type.value.lower() if record.variable_type else "secret",
                secret_arn=record.variable_cloud_identifier,
                referenced_variable_id=record.referenced_variable_id,
                environment=record.environments_enum.value if record.environments_enum else "",
                created_at=record.created_at,
                updated_at=record.updated_at,
                value=value or "",
            )

        application_code = (record.metadata_json or {}).get("application_code")
        if not application_code:
            raise HTTPException(
                status_code=500,
                detail="Variable metadata missing application_code",
            )

        auth_config = await self._get_auth_config(
            application_code=application_code,
            environment=record.environments_enum,
            tenant_code=tenant_code,
        )

        try:
            aws_secret = await AWSIntegration.get_secret(
                auth_config=auth_config,
                secret_name=record.variable_cloud_identifier,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "scheduled for deletion" in error_msg:
                await self.var_repo.soft_delete(record.id)
                raise HTTPException(
                    status_code=404,
                    detail="Variable not found in AWS (marked as deleted)",
                )
            logger.error(f"Failed to get secret from AWS: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to retrieve variable value from AWS: {str(e)}",
            )

        # Extract the specific key from the consolidated JSON secret
        secret_value = aws_secret["value"]
        if isinstance(secret_value, dict):
            variable_value = secret_value.get(record.key, "")
        else:
            variable_value = str(secret_value)

        return CanvasVariableWithValueResponse(
            id=record.id,
            code=record.code,
            transaction_code=record.transaction_code,
            table_name=record.table_name.value if record.table_name else "",
            name=record.key,
            type=record.variable_type.value.lower() if record.variable_type else "secret",
            secret_arn=record.variable_cloud_identifier,
            referenced_variable_id=record.referenced_variable_id,
            environment=record.environments_enum.value if record.environments_enum else "",
            created_at=record.created_at,
            updated_at=record.updated_at,
            value=variable_value,
        )

    async def get_variables_with_values(
        self,
        resource_code: str,
        resource_type_str: str,
        environment: str,
        tenant_code: str,
        user_email: str | None = None,
        user_code: str | None = None,
        clone_fetch: bool = False,
    ) -> dict:
        """Eagerly return each variable with all three value sources + sync state.

        ``clone_fetch`` (clone picker): ignore the caller's un-deployed staged
        file so pure drafts are dropped and ``values.pending`` stays null — the
        clone source list must show deployed values only.

        For every variable this resolves, in one call:
          1. the DB metadata (keys, type, provider, cloud_identifier, reference)
          2. ``values.cloud``   — the live provider value
          3. ``values.devlift`` — the last DEPLOYED value (audit bucket)
          4. ``values.pending`` — this user's un-deployed staged value (temp bucket)
        and a ``sync`` verdict (status / deployed / last_synced_at) so the UI can
        flag drift and the "deleted in cloud" (cloud_missing) case. ``user_email``
        scopes the per-user staged (pending) file.

        Shape per item: {"id","key","type","secret_provider","cloud_identifier",
        "value","referenced_variable_id","reference","values","sync"}.
        """
        # Authz: this endpoint returns decrypted secret values, so gate on
        # workspace access to the resource's application — the same check the
        # service-config / infrastructure loaders enforce. Denied callers get an
        # empty listing (consistent with the sibling /canvas + services readers),
        # which also avoids leaking whether the resource exists.
        table_name = _resolve_table_name(resource_type_str)
        if not await self._verify_resource_access(
            resource_code, table_name, user_code, tenant_code
        ):
            return {"items": [], "total": 0, "_timings": []}

        items = await self._resolve_variables(
            resource_code,
            resource_type_str,
            environment,
            tenant_code,
            include_sources=True,
            # Clone must not see the caller's drafts — skip the pending file read.
            user_email=None if clone_fetch else user_email,
            clone_fetch=clone_fetch,
        )
        # `_timings`: per-step wall times (temporary perf aid) so the browser
        # console can render the same breakdown the server logs.
        return {"items": items, "total": len(items), "_timings": getattr(self, "_perf_steps", [])}

    async def _get_resource_environment(
        self, resource_code: str, table_name: WorkflowSourceTableEnum
    ) -> EnvironmentEnum | None:
        """The resource's OWN environment, from its master table.

        A service config / infrastructure record belongs to exactly one
        environment, so its code already implies the env. Derive it here so
        callers never have to trust the request's ``environment`` param —
        a mismatched param once created rows stamped with the wrong env
        (hidden from every correct-env listing, but still occupying the
        env-less unique index ⇒ duplicate-key 500s on every later load).
        Returns None when the resource can't be found (caller falls back to
        the request param).
        """
        from sqlalchemy import select
        if table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
            from app.db.models.service_config_model import ServiceConfigModel
            stmt = select(ServiceConfigModel.environment).where(
                ServiceConfigModel.code == resource_code
            )
        elif table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
            from app.db.models.infrastructure_mst_model import InfrastructureMstModel
            stmt = select(InfrastructureMstModel.environments_enum).where(
                InfrastructureMstModel.code == resource_code
            )
        else:
            return None
        return (await self.db.execute(stmt)).scalars().first()

    async def _resolve_variables(
        self,
        resource_code: str,
        resource_type_str: str,
        environment: str,
        tenant_code: str,
        *,
        resolve_values: bool = True,
        include_sources: bool = False,
        user_email: str | None = None,
        clone_fetch: bool = False,
    ) -> list[dict]:
        """Load a resource's variables (+ reference/resource info for grouping).

        When ``resolve_values`` is True, each value is fetched from its provider
        (SSM per-key; Secrets Manager deduped per ARN). When False, ``value`` is
        "" (no cloud calls).

        When ``include_sources`` is True (the eager /with-values load), each item
        also carries the three value sources and a sync verdict:
          - ``values.cloud``   — live provider value (same as ``value``)
          - ``values.devlift`` — last DEPLOYED value from the audit bucket
          - ``values.pending`` — this user's un-deployed staged value (temp bucket)
          - ``sync`` — {status, deployed, last_synced_at}
        Reading devlift/pending forces ``resolve_values`` on (cloud is needed to
        compute drift). ``user_email`` scopes the per-user staged (pending) file.
        """
        if include_sources:
            resolve_values = True
        from app.core.enum import EnvironmentEnum
        table_name = _resolve_table_name(resource_type_str)
        env_enum = EnvironmentEnum(environment)

        # The resource's master record — not the request — is the authority on
        # the environment. Override a mismatched param instead of honouring it,
        # so a stale client can neither list under the wrong env nor make the
        # provider sync create rows labeled with it.
        resource_env = await self._get_resource_environment(resource_code, table_name)
        if resource_env is not None and resource_env != env_enum:
            logger.warning(
                "[with-values] environment mismatch for %s: request said '%s' "
                "but the resource belongs to '%s' — using the resource's env",
                resource_code, env_enum.value, resource_env.value,
            )
            env_enum = resource_env

        # ── Phase timing (temporary perf instrumentation) ──────────────────
        # Logs each step + its wall time so we can see where first-load time
        # goes when a service has many variables. Search logs for "[with-values]".
        import time as _time
        _perf = _time.perf_counter
        _t_total = _perf()
        _last = [_perf()]
        # Surfaced to the client as `_timings` so the browser console can show
        # the same per-step breakdown as the server logs.
        self._perf_steps = []

        def _lap(label: str, **extra) -> None:
            now = _perf()
            ms = (now - _last[0]) * 1000.0
            _last[0] = now
            self._perf_steps.append({"step": label, "ms": round(ms, 1), **{k: v for k, v in extra.items()}})
            suffix = (" " + " ".join(f"{k}={v}" for k, v in extra.items())) if extra else ""
            logger.info("[with-values] %s — %.1f ms (%.2f s)%s", label, ms, ms / 1000.0, suffix)

        logger.info(
            "[with-values] START resource=%s env=%s include_sources=%s",
            resource_code, environment, include_sources,
        )

        records = await self.var_repo.get_all_by_transaction(
            table_name=table_name,
            transaction_code=resource_code,
            environment=env_enum,
        )
        active = [r for r in records if not r.is_deleted]
        if clone_fetch:
            # Clone copies values, and a write-only value can't be read — so the
            # key must not even be offered. Dropping the row (rather than nulling
            # its value) is what stops the picker listing a key that would clone
            # as empty; the clone execute API re-checks server-side.
            active = [r for r in active if not getattr(r, "is_write_only", False)]
        _lap("step 1: load variable rows from DB", rows=len(active))
        # For the eager /with-values load (include_sources) AWS is the source of
        # truth, so keep going even with zero DB rows — the provider sync below
        # discovers the service's keys from AWS and creates them. For other
        # callers, no rows means nothing to return.
        if not active and not include_sources:
            return []

        # Resolve auth_config once — all records share the same app/env. Only
        # needed when actually reading values from the cloud.
        auth_config = None
        application_code = None
        if resolve_values:
            for r in active:
                application_code = (r.metadata_json or {}).get("application_code")
                if application_code:
                    break
            # Resolve auth_config even when application_code is None — the vendor
            # account lookup falls back to the tenant-level account.
            try:
                auth_config = await self._get_auth_config(
                    application_code=application_code,
                    environment=env_enum,
                    tenant_code=tenant_code,
                )
            except Exception as e:
                logger.warning("variable values: could not resolve auth_config: %s", e)
            _lap("step 2: resolve cloud auth (cross-account assume-role)")

        # Provider-as-source-of-truth: for the eager /with-values listing,
        # enumerate the provider stores and create DB rows for any keys that
        # exist in the cloud but not yet in the DB, so the listing reflects the
        # provider (AWS today, any provider via the plugin) rather than the DB.
        # ``store_keys_by_id`` (identifier -> {key: id} or None) lets the main
        # loop tell a cloud-absent key from one that's genuinely present.
        store_keys_by_id: dict = {}
        # scope -> {key: value}: values captured during enumeration (step 3) and
        # reused by the resolve loop (step 7) so it makes no per-key cloud calls.
        value_by_scope: dict = {}
        # Build the service path context ONCE up front — the provider key sync
        # (step 3) and the audit/pending reads (steps 5–6) all need it. Threading
        # the single instance through avoids the duplicate lookup step 3 and step
        # 5 previously each performed. None for infra resources / unresolvable
        # services (audit/temp layout is service-scoped).
        var_svc = None
        path_ctx = None
        if include_sources and auth_config is not None:
            from app.services.variable_service import VariableService
            var_svc = VariableService(self.db)
            path_ctx = await var_svc.try_build_path_context(resource_code)
        if include_sources and auth_config is not None:
            try:
                active, store_keys_by_id, value_by_scope = await self._sync_keys_from_providers(
                    active,
                    tenant_code=tenant_code,
                    table_name=table_name,
                    resource_code=resource_code,
                    env_enum=env_enum,
                    application_code=application_code,
                    auth_config=auth_config,
                    path_ctx=path_ctx,
                )
            except Exception as e:
                logger.warning("variable values: provider key sync failed: %s", e)
            _lap(
                "step 3: sync keys from AWS (enumerate stores + create missing DB rows)",
                total_rows=len(active), stores=len(store_keys_by_id), cached_scopes=len(value_by_scope),
            )

        # Pre-resolve referenced-resource details (for grouping in the UI). A
        # reference variable points to a source variable that lives on a resource
        # (e.g. INFRASTRUCTURE:INFRA_S3_xxx); join infrastructure_mst for its
        # name/type/status so the frontend can render subgroups per resource.
        infra_codes = {
            src.transaction_code
            for r in active
            if (src := getattr(r, "referenced_variable", None)) is not None
            and getattr(src, "table_name", None) is not None
            and src.table_name.value == "INFRASTRUCTURE"
            and src.transaction_code
        }
        # Resource references saved via /resource-variable/save live on the row
        # itself (referenced_transaction_code / referenced_table_name), not on a
        # source variable — include them so they group too.
        infra_codes |= {
            r.referenced_transaction_code
            for r in active
            if r.referenced_transaction_code
            and r.referenced_table_name is not None
            and r.referenced_table_name.value == "INFRASTRUCTURE"
        }
        infra_details = await self._load_infra_details(infra_codes)

        # Per-edge permission + network_policy for each referenced resource, keyed
        # by (target_table_name, target_transaction_code). The edge is
        # source=(this resource) -> target=(referenced resource); one query loads
        # every outgoing edge for this resource in the environment.
        connections = await self._load_connections(
            source_transaction_code=resource_code,
            source_table_name=table_name,
            environment=env_enum,
            tenant_code=tenant_code,
        )
        _lap("step 4: load reference/grouping metadata (infra + connections)")

        # Eager source resolution for /with-values. devlift = last DEPLOYED value
        # (state bucket state.json); pending = this user's un-deployed staged
        # edits (temp bucket). Both read via VariableService, which owns the
        # bucket paths/KMS.
        # Path context is service_config-scoped: when it can't be built (infra-
        # owned vars, or never saved) devlift/pending stay null → not_deployed.
        # ``var_svc``/``path_ctx`` were built once before step 3 and are reused
        # here — no second try_build_path_context lookup.
        pending_map: dict[str, dict] = {}
        # Caller's staged renames: deployed DB key -> staged new key. The DB
        # keeps a deployed row's key until the rename is deployed, so the new
        # name is overlaid for the renaming user only.
        pending_renames: dict[str, str] = {}
        if include_sources and auth_config is not None and var_svc is not None:
            # S3 buckets and KMS key are in stage — always use machine role for audit/temp reads.
            storage_auth = var_svc._get_storage_auth_config()
            if path_ctx is not None and user_email:
                try:
                    pending_map, pending_renames = await var_svc.read_pending_map(
                        path_ctx, user_email, storage_auth
                    )
                except Exception as e:
                    logger.warning("variable values: pending map read failed: %s", e)
            _lap("step 5: build path context + read staged/pending file (S3)")

        # Pre-read the deployed (devlift) value + timestamp for every row once —
        # reused when computing each in-cloud key's sync status (synced / drift /
        # cloud_only) below.
        def _is_secret(row) -> bool:
            return (row.variable_type.value or "").lower() == "secret" if row.variable_type else False

        def _is_write_only(row) -> bool:
            return bool(getattr(row, "is_write_only", False))

        devlift_cache: dict = {}  # row.id -> (devlift_val, last_synced_at)
        # Whether the audit baseline was actually READABLE this request. Stays
        # True when there's simply no audit layout (path_ctx None → infra
        # resources: devlift genuinely doesn't apply). Flips to False only when we
        # tried to read the audit and the batch failed — so a transient S3/KMS
        # blip reports status=unknown instead of a fabricated cloud_only/drift.
        devlift_available = True
        if include_sources and path_ctx is not None and auth_config is not None:
            # State bucket + KMS live in S3 — read with the machine role
            # (storage_auth). Bulk read: ONE get of the per-service state.json
            # yields every key's deployed value, secrets KMS-decrypted over a
            # shared client — instead of a list+get per key.
            # A batch-level failure degrades to "no drift info" (every key ->
            # (None, None)) rather than failing the whole listing. Per-item
            # decrypt failures are already isolated inside the bulk helper.
            # Write-only keys are left out: their deployed value would have to be
            # KMS-decrypted to be compared, and we never compare it — their
            # deploy state comes from variable_cloud_identifier instead. Keeping
            # them out means the plaintext is never materialised on this path.
            try:
                _by_key = await var_svc.read_deployed_sources_bulk(
                    path_ctx,
                    [(r.key, _is_secret(r)) for r in active if not _is_write_only(r)],
                    auth_config=storage_auth,
                )
            except Exception as e:
                logger.warning("variable values: bulk devlift read failed: %s", e)
                _by_key = {}
                devlift_available = False
            for r in active:
                devlift_cache[r.id] = _by_key.get(r.key, (None, None))
            _lap(
                "step 6: devlift state pre-pass (1 state.json get + parallel KMS decrypt)",
                keys=len(devlift_cache),
            )

        # Resolve each variable by its OWN provider + identifier. `sm_cache`
        # dedupes Secrets Manager reads: each unique secret ARN is fetched once,
        # then every variable extracts its own key from it.
        sm_cache: dict[str, object] = {}
        locator_cache: dict[str, dict] = {}
        items: list[dict] = []
        _val_fetch_ms = [0.0]  # accumulated time spent fetching values from AWS
        for r in active:
            # Which infrastructure resource does this variable reference?
            # (referenced_variable join, or the ref columns stored on the row.)
            src = getattr(r, "referenced_variable", None)
            if src is not None and getattr(src, "table_name", None) is not None:
                ref_code, ref_table = src.transaction_code, src.table_name.value
            elif r.referenced_transaction_code and r.referenced_table_name is not None:
                ref_code, ref_table = r.referenced_transaction_code, r.referenced_table_name.value
            else:
                ref_code, ref_table = None, None
            det = infra_details.get(ref_code) if ref_code else None

            # Write-only: the value is never read back through DevLift, so it is
            # never resolved here either — not fetched, not decrypted, not held
            # in a local. Cloud membership still comes from the store
            # enumeration below, which needs no value.
            row_write_only = _is_write_only(r)

            # Value ALWAYS comes from the actual provider (SSM Parameter Store /
            # Secrets Manager) — the real value in AWS. The DB-derived resource
            # locator never overrides it.
            if row_write_only:
                value = ""
            elif resolve_values:
                _vs = _perf()
                try:
                    value = await self._resolve_single_value(r, auth_config, sm_cache, value_by_scope)
                except Exception as e:
                    logger.warning("variable values: failed to resolve '%s': %s", r.key, e)
                    value = ""
                _val_fetch_ms[0] += (_perf() - _vs) * 1000.0
            else:
                value = ""

            # The referenced-resource locator is computed ONLY for the
            # classification below (LOCAL-draft detection / reference grouping).
            # It is NEVER used as the variable's value: if AWS has no value, the
            # value stays empty — that's final.
            locator_value = None
            if det is not None and det.get("locator"):
                loc_vals = locator_cache.get(ref_code)
                if loc_vals is None:
                    loc_vals = map_locator_to_values(det.get("type_ref"), det["locator"])
                    locator_cache[ref_code] = loc_vals
                locator_value = loc_vals.get(r.key)

            # Authoritative cloud membership from the provider enumeration (the
            # source of truth). A plain cloud-backed key whose store WAS readable
            # but no longer holds it is genuinely gone from the cloud — e.g.
            # renamed away in the console (AWS reports that as delete+add). Trust
            # that over _resolve_single_value, which for a consolidated secret
            # can hand back the whole JSON blob for a missing key.
            store_keys = store_keys_by_id.get(r.variable_cloud_identifier)
            plain_cloud_backed = (
                r.secret_provider is not None
                and r.secret_provider != SecretProviderEnum.LOCAL
                and bool(r.variable_cloud_identifier)
                and not r.referenced_variable_id
                and not r.referenced_transaction_code
            )
            cloud_gone = plain_cloud_backed and store_keys is not None and r.key not in store_keys
            if cloud_gone:
                value = ""  # not in the cloud store → no live cloud value

            # The caller's staged rename shows under the new name for them
            # only; everyone else keeps seeing the deployed key (r.key).
            display_key = pending_renames.get(r.key, r.key)

            # Three value sources + sync verdict (eager /with-values only).
            values_block = None
            sync_block = None
            if include_sources:
                # A value only counts as "cloud" when it came from a real
                # provider store (Secrets Manager / SSM) or a live resource
                # attribute (locator). A LOCAL row's value is a draft staged in
                # the temp bucket, NOT in AWS — it must not masquerade under
                # values.cloud; it surfaces via values.pending instead.
                is_local_draft = (
                    r.secret_provider == SecretProviderEnum.LOCAL
                    and locator_value is None
                )
                cloud_val = None if is_local_draft else (value if value != "" else None)
                # Reuse the audit value read once in the pre-pass above.
                devlift_val, last_synced_at = devlift_cache.get(r.id, (None, None))
                pending_entry = pending_map.get(display_key) or {}
                pending_val = pending_entry.get("value")
                pending_op = pending_entry.get("operation")
                if row_write_only:
                    # No value in any source, and no drift verdict: comparing
                    # values is exactly what write-only forbids. What is left is
                    # existence — deployed (the row carries a cloud identifier)
                    # and in_cloud (the store enumeration) — which is enough to
                    # warn that a live secret was deleted out-of-band.
                    values_block = {"cloud": None, "devlift": None, "pending": None}
                    deployed = bool(r.variable_cloud_identifier)
                    in_cloud = deployed and not cloud_gone
                    if not deployed:
                        status = "not_deployed"
                    elif not in_cloud and store_keys is not None:
                        status = "cloud_missing"
                    else:
                        status = "synced"
                    sync_block = {
                        "status": status,
                        "deployed": deployed,
                        "in_cloud": in_cloud,
                        "last_synced_at": None,
                    }
                else:
                    values_block = {"cloud": cloud_val, "devlift": devlift_val, "pending": pending_val}
                    sync_block = self._compute_sync(
                        cloud_val, devlift_val, pending_val, last_synced_at,
                        devlift_available=devlift_available,
                    )
                # Staged presence — a rename-only entry has no pending VALUE but
                # is still deployable (drives the UI's Redeploy gating).
                sync_block["staged"] = display_key in pending_map
                # A staged rename is a pending change even when the value is
                # unchanged — "synced" would hide it from the redeploy diff.
                if display_key != r.key and sync_block["status"] == "synced":
                    sync_block["status"] = "drift"
                # A staged deletion overrides the computed verdict — the UI shows
                # it as a "remove" in the redeploy modal.
                if pending_op == "delete":
                    sync_block["status"] = "pending_delete"

            # Reference block — the referenced infrastructure resource (null when
            # the variable isn't a resource reference).
            reference = None
            if ref_code and ref_table:
                # Type prefers infrastructure_mst; falls back to the code prefix
                # (INFRA_S3_xxx -> "s3") so grouping works even for orphaned refs.
                resource_type = (det["type"] if det else None) or self._type_from_infra_code(
                    ref_code
                )
                # Permission + network policy for the edge to this resource
                # (null when the connection carries neither / hasn't been drawn).
                conn = connections.get((ref_table, ref_code))
                reference = {
                    "referenced_transaction_code": ref_code,
                    "referenced_table_name": ref_table,
                    # Joined from infrastructure_mst (null when the infra was deleted).
                    "resource_name": det["name"] if det else None,
                    "infra_type_ref": det["type_ref"] if det else None,
                    "resource_type": resource_type,
                    "resource_status": det["status"] if det else None,
                    "permission": conn["permission"] if conn else None,
                    "network_policy": conn["network_policy"] if conn else None,
                }

            # Does this row point at another resource / source variable? A
            # reference DERIVES its displayed value from that target (via
            # locator_value or the resolved source value) — but that derived
            # value says nothing about whether THIS reference row was ever
            # deployed/shared. Deploy is what makes a row public: it sets
            # variable_cloud_identifier and writes an audit (devlift/state.json)
            # entry (see deploy_variables). So a reference row with neither is an
            # un-deployed per-user draft, exactly like a normal variable, and its
            # target-derived value must NOT keep it visible to other users.
            is_reference = reference is not None or bool(r.referenced_variable_id)

            # Drafts are per-user: an un-deployed variable saved by ANOTHER user
            # lives only in that user's private staged file. Hide such rows. A row
            # stays visible when it has a deployment footprint everyone can reach
            # (own cloud identifier or audit/devlift value) or is the caller's own
            # pending draft. Non-reference rows additionally stay visible on any
            # resolved value/locator; reference rows do NOT — their value is
            # derived from the target, not proof of their own deployment. The
            # creator still sees their own draft via `display_key in pending_map`.
            undeployed_for_caller = (
                not r.variable_cloud_identifier
                and devlift_val is None
                and display_key not in pending_map
            )
            if (
                include_sources
                and undeployed_for_caller
                and (is_reference or (locator_value is None and not value))
            ):
                continue

            # A plain cloud-backed key that's gone from its (readable) cloud store
            # — deleted OR renamed away in the console — is handled by intent:
            #   - If DevLift once deployed it (an audit value exists), keep the row
            #     so it surfaces as `cloud_missing`. The UI shows a "deleted from
            #     AWS" marker with a one-click re-enable (restore the audit value).
            #   - Otherwise it was never ours to restore — hide it silently.
            # The DB row is left intact either way. The caller's own staged draft
            # always shows so in-progress work never vanishes.
            # A write-only key has no devlift value to test, so its deploy
            # footprint is the cloud identifier — set only by a successful
            # deploy. That keeps the "deleted from AWS" marker working.
            devlift_footprint = (
                bool(r.variable_cloud_identifier) if row_write_only else devlift_val is not None
            )
            if (
                include_sources
                and cloud_gone
                and not devlift_footprint
                and display_key not in pending_map
            ):
                continue

            item = {
                "id": r.id,
                # variable_mst.code — the client sends this back on save/delete
                # so the backend identifies the row without a lookup.
                "code": r.code,
                "key": display_key,
                # Set when the caller has an un-deployed staged rename of this
                # row — the deployed key it will replace.
                "renamed_from": r.key if display_key != r.key else None,
                "type": r.variable_type.value if r.variable_type else None,
                # Value is never present in any source for these — the UI renders
                # a mask and hides the reveal/copy affordances.
                "is_write_only": row_write_only,
                "secret_provider": r.secret_provider.value if r.secret_provider else None,
                # Cloud resource identifier (Secrets Manager ARN / SSM path) so the
                # client can fetch the value from the provider on demand.
                "cloud_identifier": r.variable_cloud_identifier,
                "value": value,
                "referenced_variable_id": r.referenced_variable_id,
                "reference": reference,
            }
            if include_sources:
                item["values"] = values_block
                item["sync"] = sync_block
            items.append(item)
        _lap(
            "step 7: resolve values from AWS (SSM per-key / SM deduped) + build items",
            items=len(items),
            aws_fetch_ms=round(_val_fetch_ms[0], 1),
        )
        _total_ms = (_perf() - _t_total) * 1000.0
        self._perf_steps.append({"step": "TOTAL", "ms": round(_total_ms, 1), "items": len(items)})
        logger.info(
            "[with-values] TOTAL — %.1f ms (%.2f s) (resource=%s, items=%d)",
            _total_ms, _total_ms / 1000.0, resource_code, len(items),
        )
        return items

    async def _sync_keys_from_providers(
        self,
        active: list,
        *,
        tenant_code: str,
        table_name: WorkflowSourceTableEnum,
        resource_code: str,
        env_enum: EnvironmentEnum,
        application_code: str | None,
        auth_config: dict,
        path_ctx=None,
    ) -> tuple[list, dict, dict]:
        """Make the cloud providers the SOURCE OF TRUTH for the resource's KEY
        SET by filling DB gaps and reporting cloud membership:

          * cloud key with no DB row -> create a ``variable_mst`` row

        Returns ``(active, store_keys_by_id)`` where ``active`` is the row list
        extended with the newly-created rows, and ``store_keys_by_id`` maps each
        enumerated store's ``identifier`` -> ``{key: key_cloud_identifier}`` (or
        ``None`` when that store couldn't be read). The caller uses the map to
        tell a cloud-absent key from a present one, and to null the cloud value
        of a key that's no longer in its store.

        This method does NOT hide or classify anything — it only creates and
        reports. The caller pairs out-of-band renames from persistent state (a
        key IN the audit trail but GONE from cloud, matched 1:1 with a key IN
        cloud but with NO audit), hides the old name, labels the new one
        ``renamed``, and surfaces an unpaired deployed-but-gone key as
        ``cloud_missing``.

        For every distinct provider store the resource's rows anchor
        (``secret_provider`` + ``variable_cloud_identifier``), it enumerates the
        live keys via the plugin. Provider-agnostic by construction: routes only
        through ``EnvironmentVariableHandler`` / ``BaseEnvironmentVariableComponent``
        (``list_store_keys`` / ``stores_secrets``) — no provider-specific path or
        layout logic lives here, so a new provider plugs in untouched.

        Membership is tracked PER STORE against a row's own
        ``variable_cloud_identifier`` — not a per-provider union — so one
        unreadable or stale identifier can't distort another store. A store that
        can't be read (``list_store_keys`` -> ``None``) is recorded as ``None``
        so the caller keeps its rows rather than treating them as cloud-absent.

        Stores come from two places: the identifiers existing rows anchor, AND —
        for a service_config — the resource's canonical Secrets Manager / SSM
        locations derived from its ``_PathContext``. The latter lets an existing
        service whose env lives only in AWS (no anchor row) load on first open.
        Infrastructure resources have no path context and stay anchor-only.
        """
        # Distinct provider stores, anchored by existing rows. The identifier is
        # the provider-neutral locator, so no layout knowledge is needed here.
        stores: dict[tuple, None] = {}
        for r in active:
            if (
                r.variable_cloud_identifier
                and r.secret_provider is not None
                and r.secret_provider != SecretProviderEnum.LOCAL
            ):
                stores[(r.secret_provider, r.variable_cloud_identifier)] = None

        # Also seed the resource's CANONICAL provider locations derived from the
        # service itself, so an already-existing service whose env lives in AWS
        # but was never tracked in DevLift (no anchor row) still loads on first
        # open. Service-scoped only: ``path_ctx`` is None for infrastructure
        # resources, which keep the anchor-only behaviour. Built once by the
        # caller and threaded in, so we don't repeat the lookup step 5 also does.
        if path_ctx is not None:
            # Consolidated Secrets Manager secret for the service's secrets.
            canonical_secret_path = path_ctx.secret_path(tenant_code)
            # SSM configs: a placeholder leaf resolves to the config PREFIX once
            # the SSM component strips the leaf inside list_store_keys.
            canonical_parameter_path = path_ctx.parameter_path(tenant_code, "_")
            logger.info(
                "[with-values] canonical paths for resource=%s: secret='%s' parameter='%s'",
                resource_code, canonical_secret_path, canonical_parameter_path,
            )
            stores.setdefault(
                (SecretProviderEnum.AWS_SECRETS_MANAGER, canonical_secret_path), None
            )
            stores.setdefault(
                (SecretProviderEnum.AWS_SSM, canonical_parameter_path), None
            )

        if not stores:
            return active, {}, {}

        existing_keys = {r.key for r in active}
        # Per-STORE live key set: identifier -> {key: key_cloud_identifier}, or
        # None when THAT store couldn't be read (missing / transient error).
        # Keyed per store (not per provider) so one unreadable or stale
        # identifier never disables filtering for the resource's other stores.
        store_keys_by_id: dict = {}
        # scope -> {key: value}: the values enumeration already fetched, handed
        # back so the resolve loop reuses them instead of re-reading per key.
        value_by_scope: dict = {}
        # (provider, scope) -> entries | None: enumerate each scope ONCE. Per-key
        # SSM paths all collapse to their shared prefix, so a service's configs
        # are listed a single time instead of once per key.
        entries_by_scope: dict = {}
        created_any = False

        # Enumerate every DISTINCT scope CONCURRENTLY (each read once). Previously
        # each store's list_store_entries was awaited one-at-a-time, so a service
        # anchoring many stores paid the SUM of those provider round-trips. Fan
        # them out under a bounded semaphore instead — identical results, with
        # wall-clock ≈ the slowest single store rather than their sum.
        scope_rep: dict = {}  # skey -> (component, identifier, provider)
        for provider, identifier in stores:
            component = EnvironmentVariableHandler.get_component(provider, tenant_code)
            skey = (provider, component.enumeration_scope(identifier))
            scope_rep.setdefault(skey, (component, identifier, provider))

        _sem = asyncio.Semaphore(16)

        async def _enumerate(skey, component, identifier, provider):
            async with _sem:
                try:
                    return skey, await component.list_store_entries(auth_config, identifier)
                except Exception as exc:
                    logger.warning(
                        "sync: could not enumerate provider '%s' store '%s': %s",
                        provider, identifier, exc,
                    )
                    return skey, None

        for skey, entries in await asyncio.gather(
            *[_enumerate(sk, c, i, p) for sk, (c, i, p) in scope_rep.items()]
        ):
            entries_by_scope[skey] = entries

        # Walk the stores in their original order — the enumeration above is the
        # only thing that changed; the aliasing, per-scope de-dupe and row
        # creation below are byte-for-byte the same as before.
        for provider, identifier in stores:
            component = EnvironmentVariableHandler.get_component(provider, tenant_code)
            scope = component.enumeration_scope(identifier)
            entries = entries_by_scope.get((provider, scope))

            if entries is None:
                # Store unreadable / missing — never create or hide from it.
                store_keys_by_id[identifier] = None
                continue

            # Alias every anchoring identifier to this scope's key set so the
            # caller's per-row membership lookup (by the row's own identifier)
            # keeps working after the de-dupe.
            store_keys_by_id[identifier] = {k: cid for k, (cid, _v) in entries.items()}
            if scope in value_by_scope:
                continue  # rows + values already captured for this scope

            value_by_scope[scope] = {k: v for k, (_cid, v) in entries.items()}
            variable_type = (
                VariableTypeEnum.SECRET
                if getattr(component, "stores_secrets", True)
                else VariableTypeEnum.VARIABLE
            )
            for key, (key_identifier, _v) in entries.items():
                if key in existing_keys:
                    continue
                try:
                    # SAVEPOINT per insert — concurrency backstop. The unique
                    # index uq_variable_mst_owner_key (partial: live rows only,
                    # env-less by design since transaction_code implies the env)
                    # can still be hit when two first-opens of the same service
                    # race: both find no rows, both enumerate AWS, both INSERT
                    # the same key — the loser gets a UniqueViolation on flush.
                    # Without a savepoint that error poisons the whole request
                    # session (every later query, incl. the referenced_variable
                    # lazy-load, fails with PendingRollbackError and 500s this
                    # read-only GET). Nesting the flush in a savepoint rolls
                    # back only this insert and keeps the transaction usable.
                    async with self.db.begin_nested():
                        new_row = await self.var_repo.create(
                            code=str(uuid4()),
                            name=key,
                            description=f"Synced from provider for {resource_code}",
                            key=key,
                            value=None,
                            variable_type=variable_type,
                            secret_provider=provider,
                            variable_cloud_identifier=key_identifier,
                            scope_type=VariableScopeTypeEnum.INFRA,
                            table_name=table_name,
                            transaction_code=resource_code,
                            environments_enum=env_enum,
                            tenants_mst_code=tenant_code,
                            data_type=VariableDataTypeEnum.string,
                            metadata_json={
                                "application_code": application_code,
                                "synced_from_provider": True,
                            },
                        )
                except IntegrityError as exc:
                    # A concurrent viewer created the same key first — their
                    # row is equivalent to ours, so skip rather than fail.
                    logger.warning("sync: key '%s' already exists, skipping: %s", key, exc)
                    continue
                except Exception as exc:
                    logger.warning("sync: failed to create key '%s': %s", key, exc)
                    continue
                existing_keys.add(key)
                active.append(new_row)
                created_any = True

        if created_any:
            await self.db.flush()
        # Return the per-store membership map (cloud-absent vs present) and the
        # values captured during enumeration (reused by the resolve loop).
        return active, store_keys_by_id, value_by_scope

    @staticmethod
    def _compute_sync(
        cloud: str | None,
        devlift: str | None,
        pending: str | None,
        last_synced_at: str | None,
        devlift_available: bool = True,
    ) -> dict:
        """Derive the deploy/sync verdict from the three value sources.

        ``deployed`` reflects whether DevLift ever deployed the variable (the
        audit bucket holds a value). ``in_cloud`` reflects whether AWS currently
        holds a value — independent of whether DevLift deployed it. Statuses:
          - unknown       — the audit baseline couldn't be read this request
                            (``devlift_available`` False: transient S3/KMS
                            failure). Without it we can't tell synced from drift
                            from cloud_only, so we report "unknown" rather than
                            fabricate a verdict from a devlift value that's absent
                            only because the read failed.
          - not_deployed  — nothing anywhere: no cloud value and no audit record
          - cloud_only    — AWS holds a value DevLift never deployed (created or
                            edited directly in the console). No audit/temp record
                            to diff against, so it can't be classic "drift", but
                            under AWS-as-source-of-truth it is an untracked value
                            the user should import/reconcile.
          - cloud_missing — deployed once, but the value is now gone in the cloud
                            (deleted out-of-band); the audit value is recoverable
          - drift         — un-deployed staged edit (pending != devlift), or the
                            cloud value was changed out-of-band (cloud != devlift)
          - synced        — cloud == devlift and nothing pending diverges
        """
        deployed = devlift is not None
        in_cloud = cloud is not None
        if not devlift_available:
            # Audit read failed — we know what's in the cloud, but not the
            # deployed baseline, so every deployed/drift/cloud_only distinction is
            # unknowable right now. Report that honestly. A staged delete/rename is
            # still known and is overlaid by the caller after this returns.
            return {
                "status": "unknown",
                "deployed": deployed,
                "in_cloud": in_cloud,
                "last_synced_at": last_synced_at,
            }
        if not deployed:
            # No DevLift baseline. Distinguish "AWS has an untracked value"
            # (edited directly in the console) from "genuinely empty".
            status = "cloud_only" if in_cloud else "not_deployed"
        elif cloud is None:
            status = "cloud_missing"
        elif pending is not None and pending != devlift:
            status = "drift"
        elif cloud != devlift:
            status = "drift"
        else:
            status = "synced"
        return {
            "status": status,
            "deployed": deployed,
            "in_cloud": in_cloud,
            "last_synced_at": last_synced_at,
        }

    @staticmethod
    def _type_from_infra_code(code: str | None) -> str | None:
        """Derive a resource type from an infra transaction code.

        e.g. "INFRA_S3_D35E9973" -> "s3", "INFRA_K8S_PG_EAC32CF5" -> "k8s_pg".
        """
        if not code or not code.startswith("INFRA_"):
            return None
        body = code[len("INFRA_"):]
        parts = body.rsplit("_", 1)  # strip the trailing hash segment
        return parts[0].lower() if parts and parts[0] else None

    async def _load_infra_details(self, codes: set[str]) -> dict[str, dict]:
        """Resolve infrastructure_mst codes -> {name, type, type_ref, status}.

        Join key is infrastructure_mst.code == the referenced variable's
        transaction_code (both use the INFRA_<TYPE>_<HASH> format). ``type`` is
        the short form ("s3"); ``type_ref`` is the raw infrastructuretype_ref_code
        ("s3_infrastructuretype_ref"). Missing codes (deleted infra) simply won't
        appear in the map.
        """
        if not codes:
            return {}
        from sqlalchemy import select
        from app.db.models.infrastructure_mst_model import InfrastructureMstModel as M

        stmt = select(
            M.code, M.name, M.infrastructuretype_ref_code, M.status, M.locator
        ).where(M.code.in_(list(codes)))
        result = await self.db.execute(stmt)
        out: dict[str, dict] = {}
        for code, name, type_ref, status, locator in result.all():
            rtype = type_ref.replace("_infrastructuretype_ref", "") if type_ref else None
            out[code] = {
                "name": name,
                "type": rtype,
                "type_ref": type_ref,
                "status": status.value if status is not None else None,
                # Raw provisioning attributes (bucket_name, queue_url, …); the
                # source for derived variable values via _map_locator_to_values.
                "locator": locator if isinstance(locator, dict) else None,
            }
        return out

    async def _load_connections(
        self,
        source_transaction_code: str,
        source_table_name,
        environment,
        tenant_code: str,
    ) -> dict[tuple[str, str], dict]:
        """Load this resource's outgoing edges (permission + network_policy).

        Returns a map keyed by ``(target_table_name, target_transaction_code)`` so
        each referenced resource can attach its edge's operational payload. One
        query per resource; empty map when the table has no matching edges.
        """
        from sqlalchemy import select, and_
        from app.db.models.resource_connection_model import ResourceConnectionMstModel as C

        stmt = select(
            C.target_table_name,
            C.target_transaction_code,
            C.permission,
            C.network_policy,
        ).where(
            and_(
                C.source_transaction_code == source_transaction_code,
                C.source_table_name == source_table_name,
                C.environments_enum == environment,
                C.tenants_mst_code == tenant_code,
                C.is_deleted == False,  # noqa: E712
            )
        )
        try:
            result = await self.db.execute(stmt)
        except Exception as e:
            # Table may not be migrated yet in every environment — degrade to
            # "no permissions" rather than failing the whole variables load.
            logger.warning("resource connections load failed: %s", e)
            return {}

        out: dict[tuple[str, str], dict] = {}
        for target_table, target_code, permission, network_policy in result.all():
            key = (
                target_table.value if hasattr(target_table, "value") else str(target_table),
                target_code,
            )
            out[key] = {"permission": permission, "network_policy": network_policy}
        return out

    async def _resolve_single_value(
        self, r, auth_config: dict | None, sm_cache: dict | None = None,
        value_by_scope: dict | None = None,
    ) -> str:
        """Resolve one variable_mst row's value by its stored provider + identifier.

        Handles: inline/LOCAL value, variable reference, SSM parameter (individual
        per key), and Secrets Manager (fetched once per unique ARN via `sm_cache`,
        then the variable's key is extracted — supports both per-key secrets and a
        consolidated JSON of many keys). Returns "" when unresolvable / no auth.

        ``value_by_scope`` (scope -> {key: value}) holds the values step 3's
        enumeration already fetched — SSM keyed by config prefix, Secrets Manager
        by ARN. A hit here means zero extra cloud calls; a miss falls back to the
        per-key read below.
        """
        if sm_cache is None:
            sm_cache = {}
        provider = r.secret_provider.value if r.secret_provider else None

        # Variable reference -> resolve the source variable.
        if r.referenced_variable_id and not r.variable_cloud_identifier:
            if r.referenced_variable is not None:
                return await self._resolve_single_value(
                    r.referenced_variable, auth_config, sm_cache, value_by_scope
                )
            return ""

        # Inline / LOCAL / dangling (no external reference) -> DB value.
        if provider == SecretProviderEnum.LOCAL.value or not r.variable_cloud_identifier:
            return r.value or ""

        if auth_config is None:
            return ""  # can't reach AWS without creds

        # SSM Parameter Store — reuse the value enumeration already fetched for
        # this key's config prefix; fall back to a single GetParameter on a miss.
        if provider == SecretProviderEnum.AWS_SSM.value:
            identifier = r.variable_cloud_identifier
            scope = identifier.rsplit("/", 1)[0] if "/" in identifier else identifier
            if value_by_scope and r.key in value_by_scope.get(scope, {}):
                val = value_by_scope[scope][r.key]
                return str(val) if val is not None else ""
            param = await AWSIntegration.get_parameter(
                auth_config=auth_config, parameter_name=identifier
            )
            val = param.get("value")
            return str(val) if val is not None else ""

        # Secrets Manager — reuse the value enumeration already fetched for this
        # secret's ARN (one read served every key); fall back to a cached
        # per-ARN GetSecretValue on a miss.
        arn = r.variable_cloud_identifier
        if value_by_scope and r.key in value_by_scope.get(arn, {}):
            val = value_by_scope[arn][r.key]
            return str(val) if val is not None else ""
        if arn not in sm_cache:
            aws_secret = await AWSIntegration.get_secret(
                auth_config=auth_config, secret_name=arn
            )
            sm_cache[arn] = aws_secret.get("value")
        val = sm_cache[arn]
        if isinstance(val, dict):
            if r.key in val:
                return str(val[r.key])
            if len(val) == 1:
                return str(next(iter(val.values())))
            import json as _json
            return _json.dumps(val)
        return str(val) if val is not None else ""

    async def update_variable(
        self,
        variable_id: int,
        request: UpdateCanvasVariableRequest,
        tenant_code: str,
    ) -> CanvasVariableResponse:
        """
        Update a specific key's value in the consolidated AWS secret.
        Fetches the full JSON, updates the key, writes back.
        """
        record = await self._get_record_or_404(variable_id, tenant_code)

        application_code = (record.metadata_json or {}).get("application_code")
        if not application_code:
            raise HTTPException(
                status_code=500,
                detail="Variable metadata missing application_code",
            )

        auth_config = await self._get_auth_config(
            application_code=application_code,
            environment=record.environments_enum,
            tenant_code=tenant_code,
        )

        try:
            # Fetch current consolidated secret
            aws_secret = await AWSIntegration.get_secret(
                auth_config=auth_config,
                secret_name=record.variable_cloud_identifier,
            )

            secret_value = aws_secret["value"]
            current_dict = secret_value if isinstance(secret_value, dict) else {}

            # Update only this variable's key
            current_dict[record.key] = request.variable_value

            # Write back the full consolidated secret
            await AWSIntegration.update_secret(
                auth_config=auth_config,
                secret_name=record.variable_cloud_identifier,
                secret_value=current_dict,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "scheduled for deletion" in error_msg:
                await self.var_repo.soft_delete(record.id)
                raise HTTPException(
                    status_code=404,
                    detail="Variable not found in AWS (marked as deleted)",
                )
            logger.error(f"Failed to update secret in AWS: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update variable in AWS: {str(e)}",
            )

        return self._to_response(record)

    async def delete_variable(
        self,
        variable_id: int,
        tenant_code: str,
    ) -> None:
        """
        Delete a variable: remove its key from the consolidated AWS secret and soft-delete in DB.
        If no keys remain, schedule the entire AWS secret for deletion.
        """
        record = await self._get_record_or_404(variable_id, tenant_code)

        application_code = (record.metadata_json or {}).get("application_code")
        if not application_code:
            raise HTTPException(
                status_code=500,
                detail="Variable metadata missing application_code",
            )

        auth_config = await self._get_auth_config(
            application_code=application_code,
            environment=record.environments_enum,
            tenant_code=tenant_code,
        )

        try:
            # Fetch current consolidated secret
            aws_secret = await AWSIntegration.get_secret(
                auth_config=auth_config,
                secret_name=record.variable_cloud_identifier,
            )

            secret_value = aws_secret["value"]
            current_dict = secret_value if isinstance(secret_value, dict) else {}

            # Remove this variable's key
            current_dict.pop(record.key, None)

            if current_dict:
                # Other keys remain — update the secret with remaining keys
                await AWSIntegration.update_secret(
                    auth_config=auth_config,
                    secret_name=record.variable_cloud_identifier,
                    secret_value=current_dict,
                )
            else:
                # No keys remain — schedule AWS secret for deletion
                session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
                import aioboto3
                from botocore.config import Config

                config = Config(
                    signature_version="v4",
                    retries={"max_attempts": 10, "mode": "adaptive"},
                )
                session = aioboto3.Session(**session_kwargs)
                async with session.client("secretsmanager", config=config) as client:
                    await client.delete_secret(
                        SecretId=record.variable_cloud_identifier,
                        ForceDeleteWithoutRecovery=True,
                    )
                await self._revoke_default_role_secret_access(
                    tenant_code=tenant_code,
                    environment=record.environments_enum,
                    secret_arn=record.variable_cloud_identifier,
                )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" not in error_msg and "scheduled for deletion" not in error_msg:
                logger.error(f"Failed to delete variable from AWS: {e}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to delete variable in AWS: {str(e)}",
                )

        # Soft-delete in DB
        await self.var_repo.soft_delete(record.id)

    async def _revoke_default_role_secret_access(
        self, *, tenant_code: str, environment: EnvironmentEnum, secret_arn: str,
    ) -> None:
        """Mirror of ``_grant_default_role_secret_access`` for delete."""
        from app.plugin.default.default_aws_role_gen_component import apply_policy_direct
        try:
            result = await apply_policy_direct(
                tenant_code=tenant_code,
                environment=environment.value,
                kind_name="secrets-read",
                resource_arn=secret_arn,
                operation="remove",
                db=self.db,
            )
            if result.get("changed"):
                logger.info(
                    "Revoked default role access to %s (commit=%s)",
                    secret_arn, result.get("commit_sha"),
                )
        except Exception as exc:
            logger.warning(
                "Failed to revoke default role access to secret %s (non-blocking): %s",
                secret_arn, exc,
            )

    async def bulk_delete_variables(
        self,
        variable_ids: list[int],
        tenant_code: str,
    ) -> int:
        """
        Bulk delete variables: 1 AWS read + 1 AWS write for all keys sharing
        the same consolidated secret. Soft-deletes all DB records.
        Returns the count of deleted variables.
        """
        if not variable_ids:
            return 0

        # Load all records
        records = []
        for vid in variable_ids:
            try:
                record = await self._get_record_or_404(vid, tenant_code)
                records.append(record)
            except HTTPException:
                continue  # Skip already-deleted or missing records

        if not records:
            return 0

        # Group by secret ARN (all should share the same one, but be safe)
        arn_groups: dict[str, list] = {}
        for r in records:
            arn = r.variable_cloud_identifier
            if arn:
                arn_groups.setdefault(arn, []).append(r)

        deleted_count = 0
        for arn, group_records in arn_groups.items():
            first = group_records[0]
            application_code = (first.metadata_json or {}).get("application_code")
            if not application_code:
                continue

            auth_config = await self._get_auth_config(
                application_code=application_code,
                environment=first.environments_enum,
                tenant_code=tenant_code,
            )

            try:
                # 1 AWS read
                aws_secret = await AWSIntegration.get_secret(
                    auth_config=auth_config,
                    secret_name=arn,
                )
                secret_value = aws_secret["value"]
                current_dict = secret_value if isinstance(secret_value, dict) else {}

                # Remove all keys
                for r in group_records:
                    current_dict.pop(r.key, None)

                # 1 AWS write (or delete if empty)
                if current_dict:
                    await AWSIntegration.update_secret(
                        auth_config=auth_config,
                        secret_name=arn,
                        secret_value=current_dict,
                    )
                else:
                    session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
                    import aioboto3
                    from botocore.config import Config
                    config = Config(
                        signature_version="v4",
                        retries={"max_attempts": 10, "mode": "adaptive"},
                    )
                    session = aioboto3.Session(**session_kwargs)
                    async with session.client("secretsmanager", config=config) as client:
                        await client.delete_secret(
                            SecretId=arn,
                            ForceDeleteWithoutRecovery=True,
                        )
            except Exception as e:
                error_msg = str(e).lower()
                if "not found" not in error_msg and "scheduled for deletion" not in error_msg:
                    logger.error(f"Failed to bulk delete from AWS: {e}")
                    raise HTTPException(
                        status_code=502,
                        detail=f"Failed to delete variables in AWS: {str(e)}",
                    )

            # Soft-delete all DB records
            for r in group_records:
                await self.var_repo.soft_delete(r.id)
                deleted_count += 1

        return deleted_count

    async def create_variable_ref(
        self,
        request: CreateCanvasVariableRefRequest,
        tenant_code: str,
    ) -> CanvasVariableRefResponse:
        """
        Create a variable reference (interpolation link between canvas nodes).
        DB only — no AWS call.
        """
        environment = EnvironmentEnum(request.environment)
        table_name = _resolve_table_name("service")  # refs target service nodes

        # Build the ref path encoding: ref:{target}:{source}:{variable_name}
        ref_path = f"ref:{request.target_resource_code}:{request.source_resource_code}:{request.variable_name}"

        # Duplicate check
        existing = await self.var_repo.get_by_cloud_identifier(ref_path)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Variable reference '{request.alias_name}' already exists for this resource",
            )

        db_record = await self.var_repo.create(
            code=str(uuid4()),
            name=request.alias_name,
            description=request.source_label,
            key=request.alias_name,
            value=None,
            variable_type=VariableTypeEnum.VARIABLE,
            variable_cloud_identifier=ref_path,
            scope_type=VariableScopeTypeEnum.INFRA,
            table_name=table_name,
            transaction_code=request.target_resource_code,
            environments_enum=environment,
            tenants_mst_code=tenant_code,
            data_type=VariableDataTypeEnum.string,
            metadata_json={
                "application_code": request.application_code,
                "source_resource_code": request.source_resource_code,
                "value_template": request.value_template,
            },
        )

        return CanvasVariableRefResponse(
            id=db_record.code,
            source_resource_code=request.source_resource_code,
            source_label=request.source_label,
            variable_name=request.variable_name,
            alias_name=request.alias_name,
            value_template=request.value_template,
        )

    async def get_all_canvas_variables(
        self,
        application_code: str,
        environment: str,
        tenant_code: str,
    ) -> BulkCanvasVariablesResponse:
        """
        Bulk load all canvas variables for an application + environment.
        All records (regular variables and references) go through _to_response.
        References are distinguished by having referenced_variable_id set.
        """
        env_enum = EnvironmentEnum(environment)
        records = await self.var_repo.get_all_by_application_and_environment(
            application_code=application_code,
            environment=env_enum,
            tenant_code=tenant_code,
        )

        variables = [self._to_response(r) for r in records]

        return BulkCanvasVariablesResponse(
            application_code=application_code,
            environment=environment,
            variables=variables,
        )

    async def bulk_upsert_variables(
        self,
        request: BulkUpsertVariablesRequest,
        tenant_code: str,
    ) -> BulkUpsertVariablesResponse:
        """
        Bulk create/update/delete variables for a single resource.
        Reads the AWS secret once, applies all changes, writes once.
        """
        environment = EnvironmentEnum(request.environment)
        table_name = _resolve_table_name(request.resource_type_str)

        # Split: LOCAL (type=variable) vs AWS (type=secret)
        ref_names = {item.target_name for item in (request.refs_to_add or [])}
        local_variables = [v for v in request.variables if v.type == "variable" and v.name not in ref_names]
        aws_variables   = [v for v in request.variables if v.type != "variable" and v.name not in ref_names]

        # Fetch all existing records once — used for both local and secret paths
        all_records_for_resource = await self.var_repo.get_all_by_transaction(
            table_name=table_name,
            transaction_code=request.resource_code,
            environment=environment,
        )

        # Handle LOCAL variables — create, update, and delete
        local_created = 0
        local_updated = 0
        local_deleted = 0
        local_var_responses = []
        existing_local_by_key = {
            r.key: r for r in all_records_for_resource
            if r.variable_type == VariableTypeEnum.VARIABLE and not r.is_deleted
        }
        incoming_local_keys = {v.name for v in local_variables}

        for v in local_variables:
            existing = existing_local_by_key.get(v.name)
            if existing:
                if existing.value != v.value:
                    existing.value = v.value
                    local_updated += 1
                local_var_responses.append(self._to_response(existing))
            else:
                db_record = await self.var_repo.create(
                    code=str(uuid4()),
                    name=v.name,
                    description=f"Canvas variable for {request.resource_name}",
                    key=v.name,
                    value=v.value,
                    variable_type=VariableTypeEnum.VARIABLE,
                    secret_provider=SecretProviderEnum.LOCAL,
                    scope_type=VariableScopeTypeEnum.INFRA,
                    table_name=table_name,
                    transaction_code=request.resource_code,
                    environments_enum=environment,
                    tenants_mst_code=tenant_code,
                    data_type=VariableDataTypeEnum.string,
                    metadata_json={"application_code": request.application_code},
                )
                local_created += 1
                local_var_responses.append(self._to_response(db_record))

        # Delete local variables that were removed from the payload
        for key, record in existing_local_by_key.items():
            if key not in incoming_local_keys:
                await self.var_repo.soft_delete(record.id)
                local_deleted += 1

        if local_created or local_updated or local_deleted:
            await self.db.flush()

        # Check if there are any existing secret-type records that may need deletion
        has_existing_secrets = any(
            r.variable_type == VariableTypeEnum.SECRET and not r.is_deleted
            and not r.referenced_variable_id
            for r in all_records_for_resource
        )

        # Only skip the secrets path if there's nothing to create AND nothing to delete
        if not aws_variables and not has_existing_secrets:
            return BulkUpsertVariablesResponse(
                created=local_created,
                updated=local_updated,
                deleted=local_deleted,
                variables=local_var_responses,
            )

        auth_config = await self._get_auth_config(
            application_code=request.application_code,
            environment=environment,
            tenant_code=tenant_code,
        )

        app_record = await self.app_repo.get_by_code(request.application_code)
        application_name = app_record.name if app_record else request.application_code

        clean_resource_name = self._strip_service_suffix(request.resource_name)

        secret_path = self._build_secret_path(
            tenant_code=tenant_code,
            application_name=application_name,
            environment=request.environment,
            resource_name=clean_resource_name,
            transaction_code=clean_resource_name,
        )

        # Get or create the consolidated secret (1 AWS read)
        arn, current_dict = await self._get_or_create_resource_secret(
            auth_config=auth_config,
            secret_path=secret_path,
            table_name=table_name,
            transaction_code=request.resource_code,
            environment=environment,
            resource_name=clean_resource_name,
            tenant_code=tenant_code,
            application_code=request.application_code,
        )

        # Reuse the records we already fetched
        existing_records = all_records_for_resource
        existing_by_key = {
            r.key: r for r in existing_records
            if r.variable_type == VariableTypeEnum.SECRET and not r.is_deleted
            and not r.referenced_variable_id  # Skip refs — managed by bulk-refs API
        }

        incoming_keys = {v.name for v in aws_variables}
        created_count = 0
        updated_count = 0
        deleted_count = 0

        # Build the new AWS secret dict — start with ref keys to preserve them
        ref_keys = {
            r.key: current_dict.get(r.key, "")
            for r in existing_records
            if r.variable_type == VariableTypeEnum.SECRET and not r.is_deleted
            and r.referenced_variable_id
        }
        new_dict = {**ref_keys}
        for v in aws_variables:
            new_dict[v.name] = v.value
            existing = existing_by_key.get(v.name)
            if existing:
                # Key exists — check if value changed
                old_value = current_dict.get(v.name, "")
                if v.value != old_value:
                    updated_count += 1
            else:
                # New key — create DB record
                await self.var_repo.create(
                    code=str(uuid4()),
                    name=v.name,
                    description=f"Canvas variable for {clean_resource_name}",
                    key=v.name,
                    value=None,
                    variable_type=VariableTypeEnum.SECRET,
                    secret_provider=SecretProviderEnum.AWS_SECRETS_MANAGER,
                    variable_cloud_identifier=arn,
                    scope_type=VariableScopeTypeEnum.INFRA,
                    table_name=table_name,
                    transaction_code=request.resource_code,
                    environments_enum=environment,
                    tenants_mst_code=tenant_code,
                    data_type=VariableDataTypeEnum.string,
                    metadata_json={
                        "full_resource_path": secret_path,
                        "resource_name": clean_resource_name,
                        "application_code": request.application_code,
                    },
                )
                created_count += 1

        # Delete keys that were removed
        for key, record in existing_by_key.items():
            if key not in incoming_keys:
                await self.var_repo.soft_delete(record.id)
                deleted_count += 1

        # Single AWS write with the complete new dict
        await AWSIntegration.update_secret(
            auth_config=auth_config,
            secret_name=arn,
            secret_value=new_dict,
        )

        await self.db.flush()

        # Return all variables (SECRET + LOCAL), excluding refs
        all_records = await self.var_repo.get_all_by_transaction(
            table_name=table_name,
            transaction_code=request.resource_code,
            environment=environment,
        )
        variables = [
            self._to_response(r) for r in all_records
            if not r.is_deleted and not r.referenced_variable_id
            and r.variable_type in (VariableTypeEnum.SECRET, VariableTypeEnum.VARIABLE)
        ]

        return BulkUpsertVariablesResponse(
            created=created_count + local_created,
            updated=updated_count + local_updated,
            deleted=deleted_count + local_deleted,
            variables=variables,
        )

    async def delete_variable_ref(
        self,
        ref_code: str,
        tenant_code: str,
    ) -> None:
        """
        Delete a variable reference. DB soft-delete only — no AWS call.
        Refs are identified by their code (uuid), not integer ID.
        """
        record = await self.var_repo.get_by(code=ref_code, is_deleted=False)
        if not record:
            raise HTTPException(status_code=404, detail="Variable reference not found")
        if record.tenants_mst_code != tenant_code:
            raise HTTPException(status_code=404, detail="Variable reference not found")

        record.is_deleted = True
        record.is_active = False
        await self.db.flush()