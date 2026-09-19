"""
Approval Rule Repository

Data access for approval_rule_mst — the per-resource-group policy.

Only the read path is here for now. Rules are seeded directly in the database
until the settings screen that writes them is built.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.approval_rule_mst_model import ApprovalRuleMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel


class ApprovalRuleRepository:
    """Repository for approval_rule_mst table operations."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        self.model = ApprovalRuleMstModel

    async def get_for_resource_group(
        self, tenant_code: str, resource_group_mst_code: str
    ) -> Optional[ApprovalRuleMstModel]:
        """The live rule for one group, or None.

        Soft-deleted rows are excluded on purpose: a deleted rule must read as
        ABSENT — i.e. fall back to the defaults — not as a present row with
        require_approval false. The two happen to agree today and stop agreeing
        the moment a default changes.
        """
        stmt = select(self.model).where(
            self.model.tenant_code == tenant_code,
            self.model.resource_group_mst_code == resource_group_mst_code,
            self.model.is_deleted.isnot(True),
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def resource_group_of_service_config(
        self, service_config_code: str
    ) -> Optional[str]:
        """Which resource group owns a service config.

        A plain FK join — service_configs -> services_mst -> resource_group —
        so there is no hierarchy to walk.
        """
        stmt = (
            select(ServicesMstModel.resource_group_mst_code)
            .join(
                ServiceConfigModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code,
            )
            .where(ServiceConfigModel.code == service_config_code)
        )
        return (await self.session.execute(stmt)).scalars().first()
