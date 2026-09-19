from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.enum import EnvironmentEnum
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.repository.db_object_mst_repository import DbObjectMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.validator_response_schemas import ValidationResult


class DatabaseEngineEnum(str, Enum):
    """AWS database engines supported by DatabaseOps."""
    RDS_MYSQL = "rds_mysql"
    RDS_POSTGRES = "rds_postgres"
    AURORA_MYSQL = "aurora_mysql"
    AURORA_POSTGRES = "aurora_postgres"


# Maps engine -> exact infrastructuretype_ref_code value
_ENGINE_TYPE_REF_CODE = {
    DatabaseEngineEnum.RDS_MYSQL: "rds_mysql_infrastructuretype_ref",
    DatabaseEngineEnum.RDS_POSTGRES: "rds_postgres_infrastructuretype_ref",
    DatabaseEngineEnum.AURORA_MYSQL: "aurora_mysql_infrastructuretype_ref",
    DatabaseEngineEnum.AURORA_POSTGRES: "aurora_postgres_infrastructuretype_ref",
}

# All AWS database infrastructuretype_ref_code values
DB_INFRA_TYPE_REF_CODES = list(_ENGINE_TYPE_REF_CODE.values())


class DatabaseOps:
    def __init__(
        self,
        infrastructure_repo: InfrastructureMstRepository,
        db_object_mst_repo: Optional[DbObjectMstRepository] = None,
    ):
        self.infrastructure_repo = infrastructure_repo
        self.db_object_mst_repo = db_object_mst_repo

    async def fetch_servers(
        self,
        tenant_code: str,
        product_code: str,
        environment: EnvironmentEnum,
        geo_loc: str,
        page: Optional[int] = None,
        page_size: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch all AWS database servers (RDS / Aurora — MySQL & Postgres) for
        the (tenant_code, product_code, environment, geo_loc) combination as a
        list of `{label, value, extra_data}` dropdown options where `value` is
        the infrastructure code, `label` is the server name from the locator,
        and `extra_data` carries the `infrastructuretype_ref_code` so the
        frontend can tell RDS/Aurora MySQL/Postgres apart.

        If `page` is provided (1-indexed), pagination is applied with the given
        `page_size`. If not, all matching servers are returned.
        """
        filters = [
            InfrastructureMstModel.tenants_mst_code == tenant_code,
            InfrastructureMstModel.applications_mst_code == product_code,
            InfrastructureMstModel.environments_enum == environment,
            InfrastructureMstModel.geo_loc_mst_code == geo_loc,
            InfrastructureMstModel.infrastructuretype_ref_code.in_(DB_INFRA_TYPE_REF_CODES),
            InfrastructureMstModel.is_deleted == False,
        ]
        if page is not None:
            skip = max(0, (page - 1) * page_size)
            servers = await self.infrastructure_repo.get_multi(
                filters=filters, skip=skip, limit=page_size
            )
        else:
            servers = await self.infrastructure_repo.get_multi(filters=filters, limit=1000)

        return [
            {
                "label": (s.locator or {}).get("server_name") or s.code,
                "value": s.code,
                "extra_data": {
                    "infrastructuretype_ref_code": s.infrastructuretype_ref_code,
                },
            }
            for s in servers
        ]

    async def duplicate_database_validator(
        self,
        tenant_code: str,
        server_code: str,
        database_name: str,
    ) -> ValidationResult:
        """Check whether a database with the given name already exists on
        the given server (infrastructure_mst_code) for the tenant.

        Looks up `db_object_mst` for a top-level entry where:
            - tenant_code == tenant_code
            - infrastructure_mst_code == server_code
            - type == 'database'
            - name == database_name

        Returns a ValidationResult — see app.schemas.validator_response_schemas
        for the canonical format used by all validators.
        """
        if self.db_object_mst_repo is None:
            raise ValueError(
                "db_object_mst_repo is required to call duplicate_database_validator()"
            )

        existing = await self.db_object_mst_repo.get_by(
            tenant_code=tenant_code,
            infrastructure_mst_code=server_code,
            name=database_name,
            type="database",
            parent_id=None,
            is_deleted=False,
        )

        if existing:
            return ValidationResult(
                type="duplicateValidation",
                description=(
                    f"Database '{database_name}' already exists on server "
                    f"'{server_code}'."
                ),
                valid=False,
            )

        return ValidationResult(
            type="duplicateValidation",
            description=(
                f"Database '{database_name}' is available on server "
                f"'{server_code}'."
            ),
            valid=True,
        )
