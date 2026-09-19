from app.core.enum import EnvironmentEnum
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.validator_response_schemas import ValidationResult


K8S_POSTGRES_INFRA_TYPE_REF_CODE = "devlift_k8s_postgres_infrastructuretype_ref"


class K8sPostgresOps:
    def __init__(self, infrastructure_repo: InfrastructureMstRepository):
        self.infrastructure_repo = infrastructure_repo

    async def duplicate_server_validator(
        self,
        tenant_code: str,
        product_code: str,
        environment: EnvironmentEnum,
        geo_loc: str,
        server_name: str,
    ) -> ValidationResult:
        """Check whether a Kubernetes Postgres server with the given name already
        exists for the (tenant_code, product_code, environment, geo_loc) combination.

        Returns a ValidationResult — see app.schemas.validator_response_schemas
        for the canonical format used by all validators.
        """
        filters = [
            InfrastructureMstModel.tenants_mst_code == tenant_code,
            InfrastructureMstModel.applications_mst_code == product_code,
            InfrastructureMstModel.environments_enum == environment,
            InfrastructureMstModel.geo_loc_mst_code == geo_loc,
            InfrastructureMstModel.infrastructuretype_ref_code == K8S_POSTGRES_INFRA_TYPE_REF_CODE,
            InfrastructureMstModel.locator["server_name"].astext == server_name,
            InfrastructureMstModel.is_deleted == False,
        ]
        existing = await self.infrastructure_repo.get_infrastructure_record(filters)

        if existing:
            return ValidationResult(
                type="duplicateValidation",
                description=(
                    f"Postgres server '{server_name}' already exists for product "
                    f"'{product_code}' in {environment.value} ({geo_loc})."
                ),
                valid=False,
            )

        return ValidationResult(
            type="duplicateValidation",
            description=f"Postgres server '{server_name}' is available.",
            valid=True,
        )
