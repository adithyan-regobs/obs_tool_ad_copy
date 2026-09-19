"""
File Location Handler

Orchestrator layer for file location determination.
Routes to tenant-specific locator implementations:

- Aspora → app/plugin/aspora/file_locator/aspora_file_locator.py
- Default → (TODO: default implementation)

This handler does NOT contain business logic for file location.
It only routes to the appropriate tenant-specific locator.

Tenant-specific locators implement:
- Repository determination (infrastructure vs service)
- Branch calculation (multi-branch for service configs)
- File path calculation
- Operation type (CREATE vs UPDATE)
- Business validation (duplicate checks, etc.)
"""

import logging
from typing import List, Dict, Any, Optional, Type
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.core.config import settings
from app.schemas.file_location_response_schema import FileLocationResponse
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.plugin.aspora.file_locator.aspora_file_locator import AsporaFileLocator
from app.plugin.default.default_file_locator import DefaultFileLocator

logger = logging.getLogger(__name__)

class FileLocationHandler:
    _tenant_locator_map: Dict[str, Type] = {
        "default": DefaultFileLocator,
        "aspora": AsporaFileLocator,
        "vance": AsporaFileLocator,
    }

    @classmethod
    async def locate(cls, tenant: str, queue_item: Dict, workflow_context: PRWorkflowContext) -> FileLocationResponse:
        """
        Public use-case entry point

        Args:
            tenant: Tenant code
            queue_item: Queue item dict
            workflow_context: PR workflow context (passed by reference)

        Returns:
            FileLocationResponse
        """
        return await cls.get_locator(tenant).locate(queue_item, workflow_context)

    @classmethod
    def get_locator(cls, tenant: str) -> Type:
        """
        Resolve concrete locator.
        Falls back to DefaultFileLocator if tenant is not configured.
        """
        try:
            locator_cls = cls._tenant_locator_map[tenant]
        except KeyError:
            logger.warning(f"No specific locator for tenant '{tenant}', using default")
            locator_cls = DefaultFileLocator

        return locator_cls()