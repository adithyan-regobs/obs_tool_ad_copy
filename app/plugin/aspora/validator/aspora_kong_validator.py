"""
Aspora Kong Validator

Validates Kong gateway configuration presence in GitHub before creation.
"""

#TODO add pre validator handler
import logging

from app.handlers.file_location_handler import FileLocationHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.schemas.validator_response_schemas import ValidatorResponse


class AsporaKongValidator:
    """Validator for Aspora Kong gateway configuration presence."""

    COMPONENT_NAME = "aspora_kong_validator"
    ERROR_CODE_OK = "OK"
    ERROR_CODE_FILE_MISSING = "KONG_GATEWAY_FILE_NOT_FOUND"
    ERROR_CODE_FILE_LOCATION = "KONG_GATEWAY_FILE_LOCATION_ERROR"
    ERROR_CODE_GITHUB_ERROR = "KONG_GATEWAY_GITHUB_ERROR"
    ERROR_CODE_API_MISSING = "KONG_GATEWAY_API_NOT_FOUND"

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def validate(
        self,
        tenant_code: str,
        environment: str,
        geo_loc_mst_code: str,
        product_name: str,
        api_name: str
    ) -> ValidatorResponse:
        """
        Run all Kong validations.

        This is the main entry point to keep space for future validations.
        """
        self.logger.info(
            "Kong validation start: tenant=%s, env=%s, geo_loc=%s",
            tenant_code,
            environment,
            geo_loc_mst_code
        )
        file_result, file_content = await self._get_gateway_file_content(
            tenant_code=tenant_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            product_name=product_name
        )
        if not file_result.validation_status:
            return file_result

        return await self.validate_api_exists(
            api_name=api_name,
            gateway_content=file_content
        )

    async def validate_api_exists(
        self,
        api_name: str,
        gateway_content: str
    ) -> ValidatorResponse:
        """
        Validate that the API exists in the gateway configuration file.
        """
        normalized_api_name = api_name
        if normalized_api_name and not normalized_api_name.endswith("-service"):
            normalized_api_name = f"{normalized_api_name}-service"

        self.logger.info(
            "Validating API exists in gateway file: api_name=%s",
            normalized_api_name
        )
        api_pattern = f"\"{normalized_api_name}\""
        if api_pattern not in (gateway_content or ""):
            error_message = (
                f"API service '{normalized_api_name}' does not exist in current configuration."
            )
            self.logger.error(error_message)
            return ValidatorResponse(
                error_code=self.ERROR_CODE_API_MISSING,
                error_message=error_message,
                validation_status=False,
                component_name=self.COMPONENT_NAME
            )

        self.logger.info("API validation passed for %s", normalized_api_name)
        return ValidatorResponse(
            error_code=self.ERROR_CODE_OK,
            error_message="",
            validation_status=True,
            component_name=self.COMPONENT_NAME
        )


    async def _get_gateway_file_content(
        self,
        tenant_code: str,
        environment: str,
        geo_loc_mst_code: str,
        product_name: str
    ):
        try:
            file_location = await self._resolve_kong_file_location(
                tenant_code=tenant_code,
                environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
                product_name=product_name
            )
        except Exception as exc:
            error_message = f"Failed to resolve Kong gateway file location: {exc}"
            self.logger.error(error_message, exc_info=True)
            return ValidatorResponse(
                error_code=self.ERROR_CODE_FILE_LOCATION,
                error_message=error_message,
                validation_status=False,
                component_name=self.COMPONENT_NAME
            ), ""

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        branch = file_location.feature_branch or file_location.base_branch

        self.logger.info(
            "Kong file location resolved: repo=%s, branch=%s, path=%s",
            file_location.repo,
            branch,
            file_location.file_path
        )

        self.logger.info("Calling GitOps get_content for Kong gateway file")
        existing_file = await GitOpsHandler.get_content(
            tenant=tenant_code,
            owner=owner,
            repo=repo,
            file_path=file_location.file_path,
            branch=branch
        )
        self.logger.info(
            "GitOps get_content completed: status=%s, exists=%s",
            existing_file.get("status"),
            existing_file.get("exists")
        )

        if existing_file.get("status") == "error":
            error_message = f"GitOps get_content failed: {existing_file.get('error')}"
            self.logger.error(error_message)
            return ValidatorResponse(
                error_code=self.ERROR_CODE_GITHUB_ERROR,
                error_message=error_message,
                validation_status=False,
                component_name=self.COMPONENT_NAME
            ), ""

        if not existing_file.get("exists"):
            error_message = (
                f"Kong gateway configuration file does not exist: {file_location.file_path}. "
                "Routes can only be added to existing gateway configurations."
            )
            self.logger.warning(error_message)
            return ValidatorResponse(
                error_code=self.ERROR_CODE_FILE_MISSING,
                error_message=error_message,
                validation_status=False,
                component_name=self.COMPONENT_NAME
            ), ""

        self.logger.info("Kong gateway file validation passed")
        return ValidatorResponse(
            error_code=self.ERROR_CODE_OK,
            error_message="",
            validation_status=True,
            component_name=self.COMPONENT_NAME
        ), existing_file.get("content") or ""

    async def _resolve_kong_file_location(
        self,
        tenant_code: str,
        environment: str,
        geo_loc_mst_code: str,
        product_name: str
    ):
        workflow_context = PRWorkflowContext(skip_commit=True)
        queue_dict = {
            "case_ref_code": "add_route",
            "tenant": tenant_code,
            "tenant_code": tenant_code,
            "environment": environment,
            "config_snapshot": {
                "product_name": product_name,
                "environment": environment,
                "geo_loc_mst_code": geo_loc_mst_code,
            },
        }

        file_location_response = await FileLocationHandler.locate(
            tenant_code,
            queue_dict,
            workflow_context
        )
        if not file_location_response or not file_location_response.files:
            raise ValueError("No file locations determined for Kong gateway validation")

        return file_location_response.files[0]


    @staticmethod
    async def validate_from_chat(
        new_params: dict,
        current_valid: dict,
        *,
        tenant_id: str | None = None,
    ) -> dict | None:
        """Validate Kong route — checks service exists in GitHub Kong HCL config.

        Returns None if valid, or {param_name: {"value": ..., "reason": ...}} for invalid params.
        """
        from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code

        _log = logging.getLogger(__name__)
        _log.debug(
            "[validate_from_chat] INPUT: new_params=%s, current_valid=%s, tenant_id=%s",
            new_params, current_valid, tenant_id
        )

        service = new_params.get("service")
        _log.debug("[validate_from_chat] extracted service=%s", service)
        if not service:
            _log.debug("[validate_from_chat] no service — skipping validation, return None")
            return None

        merged = {**current_valid, **new_params}
        region = merged.get("region")
        product = merged.get("product")
        environment = merged.get("environment")
        _log.debug(
            "[validate_from_chat] merged params: region=%s, product=%s, environment=%s",
            region, product, environment
        )

        if not all([region, product, environment]):
            _log.debug(
                "[validate_from_chat] missing required params — skipping Phase 2, return None"
            )
            return None

        try:
            geo_loc_mst_code = normalize_geo_loc_code(region)
            tenant_code = tenant_id or "aspora"
            _log.debug(
                "[validate_from_chat] calling validator.validate: tenant_code=%s, environment=%s, "
                "geo_loc_mst_code=%s, product=%s, api_name=%s",
                tenant_code, environment, geo_loc_mst_code, product, service
            )

            validator = AsporaKongValidator()
            result = await validator.validate(
                tenant_code=tenant_code,
                environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
                product_name=product,
                api_name=service,
            )

            _log.debug(
                "[validate_from_chat] validator.validate result: validation_status=%s, "
                "error_code=%s, error_message=%s",
                result.validation_status, result.error_code, result.error_message
            )

            if not result.validation_status:
                error_response = {
                    "service": {
                        "value": service,
                        "reason": result.error_message,
                        "error_code": result.error_code,
                    },
                }
                _log.debug("[validate_from_chat] OUTPUT (invalid): %s", error_response)
                return error_response

            _log.debug("[validate_from_chat] OUTPUT: None (validation passed)")
            return None

        except Exception as exc:
            _log.error("[validate_from_chat] validation error: %s", exc, exc_info=True)
            return {
                "service": {
                    "value": service,
                    "reason": f"Failed to validate service against Kong gateway config: {exc}",
                },
            }