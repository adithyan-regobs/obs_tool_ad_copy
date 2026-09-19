"""
S3 Creation Service

Provides S3 bucket operations for MCP server consumption.
Parameter validation is handled at the schema level (MCP tool inputSchema).
"""
import logging
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra_chat_agent.config.tools_enum.reference_enums import Environment
from app.core.enum import EnvironmentEnum

logger = logging.getLogger(__name__)


class S3CreationService:
    """
    Service for S3 bucket operations.

    Formats parameters and builds responses.
    Designed for MCP server consumption.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _check_duplicate_bucket(
        self,
        identifier: str,
        tenant_code: str,
        environment: EnvironmentEnum,
    ) -> None:
        """
        Check if a bucket with the same identifier already exists.

        Raises:
            ValueError: If a duplicate bucket is found.
        """
        from sqlalchemy.exc import MultipleResultsFound
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

        repo = InfrastructureMstRepository(self.db)
        try:
            existing = await repo.check_s3_bucket_exists(
                bucket_identifier=identifier,
                tenant_code=tenant_code,
                environment=environment,
            )
        except MultipleResultsFound:
            raise ValueError(
                f"S3 bucket with identifier '{identifier}' already exists "
                f"(multiple records found) for tenant '{tenant_code}' in "
                f"environment '{environment.value}'. "
                f"Please choose a different bucket name."
            )

        if existing:
            raise ValueError(
                f"S3 bucket with identifier '{identifier}' already exists for "
                f"tenant '{tenant_code}' in environment '{environment.value}'. "
                f"Existing bucket code: {existing.code}. "
                f"Please choose a different bucket name."
            )

    # ── Public methods ──────────────────────────────────────────────

    async def list_s3_buckets(
        self,
        tenant_code: str,
        environment: Environment,
        geo_loc_code: str,
        product_name: str,
    ) -> Dict[str, Any]:
        """
        List existing S3 buckets for a tenant and environment.

        Args:
            tenant_code: Tenant identifier
            environment: Environment enum
            geo_loc_code: Geographic location code
            product_name: Product name (e.g., 'Core', 'Falcon')

        Returns:
            Dict with buckets list and count
        """
        logger.info(
            "S3 CREATION - list_s3_buckets",
            extra={
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
            }
        )

        try:
            from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

            repo = InfrastructureMstRepository(self.db)
            results = await repo.list_by_filters(
                tenant_code=tenant_code,
                infrastructuretype_ref_code="s3_infrastructuretype_ref",
                environment=EnvironmentEnum(environment.value),
                geo_loc_mst_code=geo_loc_code,
            )

            buckets = []
            for infra in results:
                locator = infra.locator or {}
                buckets.append({
                    "bucket_name": locator.get("bucket_name", ""),
                    "status": infra.infra_status.value if infra.infra_status else None,
                    "code": infra.code,
                })

            logger.info(f"Found {len(buckets)} S3 buckets")

            return {
                "buckets": buckets,
                "count": len(buckets),
            }

        except Exception as e:
            logger.error(f"list_s3_buckets failed: {e}", exc_info=True)
            return {
                "buckets": [],
                "count": 0,
                "error": str(e),
            }

    async def create_s3_bucket(
        self,
        identifier: str,
        tenant_code: str,
        product_name: str,
        environment: Environment,
        geo_loc: str,
        geo_loc_code: str,
        applications_mst_code: Optional[str] = None,
        infra_vendor: str = "aws",
        versioning: bool = False,
        enable_s3_replication: bool = False,
        cross_account_account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Format S3 bucket creation parameters into response.

        Note: This does NOT save to database. Validation is handled by the tool schema.
        """
        logger.info(
            "S3 CREATION - create_s3_bucket",
            extra={
                "identifier": identifier,
                "tenant_code": tenant_code,
                "product_name": product_name,
                "environment": environment.value,
                "geo_loc_code": geo_loc_code,
                "versioning": versioning,
                "enable_s3_replication": enable_s3_replication,
            }
        )

        # # Check for duplicate bucket
        # await self._check_duplicate_bucket(
        #     identifier=identifier,
        #     tenant_code=tenant_code,
        #     environment=EnvironmentEnum(environment.value),
        # )

        # Build attribute_parameters (S3-specific)
        attribute_parameters = {
            "identifier": identifier,
            "versioning": versioning,
            "enable_s3_replication": enable_s3_replication,
        }
        if cross_account_account_id:
            attribute_parameters["cross_account_account_id"] = cross_account_account_id

        # Build placement_parameters (environment/location context)
        placement_parameters = {
            "tenant_code": tenant_code,
            "product_name": product_name,
            "applications_mst_code": applications_mst_code or product_name,
            "environment_enum": environment.value,
            "geo_loc": geo_loc,
            "geo_loc_mst_code": geo_loc_code,
            "infra_vendor_enum": infra_vendor,
            "case_type_ref_code": "s3",
            "case_ref_code": "create_bucket",
        }

        return {
            "status": "success",
            "message": f"S3 bucket '{identifier}' creation request validated",
            "attribute_parameters": attribute_parameters,
            "placement_parameters": placement_parameters,
            "is_ready": True,
        }
