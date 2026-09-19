from app.core.enum import EnvironmentEnum
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.validator_response_schemas import ValidationResult


REDIS_INFRA_TYPE_REF_CODE = "elasticache_redis_infrastructuretype_ref"


class ElastiCacheOps:
    def __init__(self, infrastructure_repo: InfrastructureMstRepository):
        self.infrastructure_repo = infrastructure_repo

    async def duplicate_cluster_validator(
        self,
        tenant_code: str,
        product_code: str,
        environment: EnvironmentEnum,
        geo_loc: str,
        identifier: str,
    ) -> ValidationResult:
        """Check whether an ElastiCache Redis cluster with the given identifier
        already exists for the (tenant_code, product_code, environment, geo_loc)
        combination.

        Returns a ValidationResult — see app.schemas.validator_response_schemas
        for the canonical format used by all validators.
        """
        filters = [
            InfrastructureMstModel.tenants_mst_code == tenant_code,
            InfrastructureMstModel.applications_mst_code == product_code,
            InfrastructureMstModel.environments_enum == environment,
            InfrastructureMstModel.geo_loc_mst_code == geo_loc,
            InfrastructureMstModel.infrastructuretype_ref_code == REDIS_INFRA_TYPE_REF_CODE,
            InfrastructureMstModel.locator["identifier"].astext == identifier,
            InfrastructureMstModel.is_deleted == False,
        ]
        existing = await self.infrastructure_repo.get_infrastructure_record(filters)

        if existing:
            return ValidationResult(
                type="duplicateValidation",
                description=(
                    f"Redis cluster '{identifier}' already exists for product "
                    f"'{product_code}' in {environment.value} ({geo_loc})."
                ),
                valid=False,
            )

        return ValidationResult(
            type="duplicateValidation",
            description=f"Redis cluster '{identifier}' is available.",
            valid=True,
        )
