"""
Resource Pre-Action Handler

Dispatch layer for per-queue pre-workflow actions that run before
file-location, script-gen, git-commit, or GitHub-PR work for that queue item.

Called once per queue item (mirrors ResourcePostActionHandler).

Pre-action components handle early side-effects — e.g. flipping resource
status to INITIALISING (or SOFT_DELETING for delete_* case_refs) so the canvas
updates within ~100ms of the POST landing.

Dispatch key: tenant (single-level, like FileLocationHandler)

Lookup order:
  1. Tenant-specific map
  2. "default" map (fallback for all tenants)
  3. Silent no-op if no component is registered — pre-actions are optional
"""

import logging
import inspect
from typing import Dict, Optional, Type

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ResourcePreActionHandler:

    @classmethod
    def _build_map(cls) -> Dict[str, Type]:
        from app.plugin.default.default_resource_pre_action_component import DefaultResourcePreActionComponent
        from app.plugin.aspora.aspora_resource_pre_action_component import AsporaResourcePreActionComponent
        return {
            "default": DefaultResourcePreActionComponent,
            # Aspora and Vance opt out of the default INITIALISING flip — the
            # canvas drives their resource lifecycle UX itself.
            "aspora": AsporaResourcePreActionComponent,
            "vance":  AsporaResourcePreActionComponent,
        }

    @classmethod
    async def run(
        cls,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> None:
        """
        Run the registered pre-action component for a single queue item.
        Silently skips if no component is registered.
        Pre-action failures are non-fatal — logged but do not block the workflow.
        """
        component_cls = cls._resolve(tenant)
        if component_cls is None:
            return

        component = component_cls()
        try:
            result = component.run(
                tenant=tenant,
                queue_dict=queue_dict,
                db=db,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(
                "[PreAction] Component failed tenant=%s: %s",
                tenant,
                e,
                exc_info=True,
            )

    @classmethod
    def _resolve(cls, tenant: str) -> Optional[Type]:
        pre_action_map = cls._build_map()
        component_cls = pre_action_map.get(tenant)
        if component_cls is None:
            component_cls = pre_action_map.get("default")
        return component_cls
