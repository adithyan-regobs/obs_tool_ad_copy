"""
Resource Settings Saver Handler

Dispatch layer for writing an APPROVED queue item's config_snapshot into the
live table that kind of change describes. Called once per queue item, AFTER the
PR exists — a change that never shipped must not move the live row.

Save writes nothing live, on purpose: the proposal sits in config_snapshot and
service_configs keeps describing what is really running, which is what makes it
an honest baseline for the next diff. This handler is what closes that gap once
the change is actually out.

Dispatch key: case_ref_code (from the queue item's case_ref_code column)

Lookup order:
  1. Tenant-specific map
  2. "default" map (fallback for all tenants)
  3. Silent no-op if nothing is registered — most case_ref_codes have no live
     settings row to update, and that is the normal case rather than an error

Adding a resource type: add a component file under app/plugin/settings_saver/
and one line to the map below.
"""

import logging
from typing import Dict, Optional, Type

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ResourceSettingsSaverHandler:

    @classmethod
    def _build_map(cls) -> Dict[str, Dict[str, Type]]:
        from app.plugin.settings_saver.service_settings_saver_component import (
            ServiceSettingsSaverComponent,
        )

        return {
            "default": {
                # EKS / ECS service settings → service_configs
                "update_service": ServiceSettingsSaverComponent,
                # Kong routes → kong_route_groups / kong_route_configs.
                # "add_route": KongSettingsSaverComponent,
                #
                # Infra resources → infra_mst.
                # "create_bucket": S3SettingsSaverComponent,
                # "create_queue": SqsSettingsSaverComponent,
                # "table_management": DynamoDbSettingsSaverComponent,
            },
        }

    @classmethod
    async def run(
        cls,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> bool:
        """Run the registered settings saver for one queue item.

        Dispatches by queue_dict["case_ref_code"]. Silently skips when nothing
        is registered.

        Failures are NON-FATAL and loud. By the time this runs the PR is raised
        and the change is out, so refusing here would not un-ship anything — it
        would only turn a stale settings row into a failed deploy. The error is
        logged at ERROR with the queue code so the row can be chased.

        Returns True when a component actually wrote something.
        """
        case_ref_code = queue_dict.get("case_ref_code") or ""
        component_cls = cls._resolve(tenant, case_ref_code)
        if component_cls is None:
            return False

        try:
            return bool(await component_cls().run(
                tenant=tenant, queue_dict=queue_dict, db=db
            ))
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[SettingsSaver] queue=%s shipped but its settings could not be "
                "saved (tenant=%s case_ref_code=%s) — the live record is now "
                "stale: %s",
                queue_dict.get("code"), tenant, case_ref_code, e,
                exc_info=True,
            )
            return False

    @classmethod
    def _resolve(cls, tenant: str, case_ref_code: str) -> Optional[Type]:
        saver_map = cls._build_map()
        tenant_map = saver_map.get(tenant, {})
        component_cls = tenant_map.get(case_ref_code)
        if component_cls is None:
            component_cls = saver_map.get("default", {}).get(case_ref_code)
        return component_cls
