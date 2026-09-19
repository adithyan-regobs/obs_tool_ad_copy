"""
Default Resource Pre-Action Component

Per-queue early-status flip on the resource referenced by a queue item, so
the canvas updates within ~100ms of the POST landing, before file-location,
script-gen, git-commit, and GitHub PR work.

Status target by case_ref_code:
  delete_*  →  SOFT_DELETING   (e.g. delete_bucket / delete_queue / delete_dynamodb_table)
  anything else  →  INITIALISING
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import ResourceStatusEnum
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

logger = logging.getLogger(__name__)


class DefaultResourcePreActionComponent:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> None:
        """
        Flip the resource's status for a single queue item.
        Called once per queue item from the workflow loop.
        """
        if db is None:
            return

        transaction_code = queue_dict.get("transaction_code")
        table_name = queue_dict.get("table_name")
        case_ref_code = queue_dict.get("case_ref_code") or ""

        if not transaction_code or not table_name:
            return

        # Resolve enum/string table names to a comparable string.
        table = getattr(table_name, "value", table_name) if not isinstance(table_name, str) else table_name

        target_status = (
            ResourceStatusEnum.SOFT_DELETING
            if case_ref_code.startswith("delete_")
            else ResourceStatusEnum.INITIALISING
        )

        try:
            if table == "SERVICE_CONFIG":
                await ServiceConfigRepository(db).update_status(transaction_code, target_status)
            elif table == "INFRASTRUCTURE":
                await InfrastructureMstRepository(db).update_status(transaction_code, target_status)
            else:
                return
            await db.commit()
            self.logger.info(
                "[PreAction] %s/%s → %s",
                table, transaction_code, target_status.value,
            )
        except Exception as exc:
            self.logger.error(
                "Failed to set pre-action status for %s/%s: %s",
                table, transaction_code, exc, exc_info=True,
            )
