from app.core.enum import EnvironmentEnum
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.validator_response_schemas import ValidationResult


class SQSOps:
    def __init__(self, infrastructure_repo: InfrastructureMstRepository):
        self.infrastructure_repo = infrastructure_repo

    async def duplicate_queue_validator(
        self,
        tenant_code: str,
        product_code: str,
        environment: EnvironmentEnum,
        geo_loc: str,
        queue_name: str,
    ) -> ValidationResult:
        """Check whether an SQS queue with the given name already exists
        for the (tenant_code, product_code, environment, geo_loc) combination.

        Returns a ValidationResult — see app.schemas.validator_response_schemas
        for the canonical format used by all validators.
        """
        # Validator receives the raw user-supplied identifier (not the
        # resolved AWS name). Since the factory now stores `queue_name` as
        # the resolved AWS name and `identifier` as the raw input, match on
        # `identifier`.
        # check_sqs_queue_exists uses first-match semantics, so pre-existing
        # duplicate rows don't blow up the validator with MultipleResultsFound
        # the way get_infrastructure_record's scalar_one_or_none() does.
        existing = await self.infrastructure_repo.check_sqs_queue_exists(
            queue_identifier=queue_name,
            tenant_code=tenant_code,
            environment=environment,
            application_code=product_code,
            geo_loc_mst_code=geo_loc,
        )

        if existing:
            return ValidationResult(
                type="duplicateValidation",
                description=(
                    f"Queue '{queue_name}' already exists for product "
                    f"'{product_code}' in {environment.value} ({geo_loc})."
                ),
                valid=False,
            )

        return ValidationResult(
            type="duplicateValidation",
            description=f"Queue '{queue_name}' is available.",
            valid=True,
        )