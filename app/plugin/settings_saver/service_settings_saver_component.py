"""
Service Settings Saver Component

case_ref_code: update_service

Writes an approved SERVICE_CONFIG queue row's `config_snapshot` into its live
`service_configs` row — the moment a proposal becomes the configuration.

Nothing else writes those values. Save parks them in `config_snapshot` and the
GitOps pipeline only ever writes status and alb_url back, so without this the
settings screen would keep showing the old values after every successful deploy.
"""

import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import WorkflowSourceTableEnum
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.plugin.settings_saver.base_settings_saver_component import (
    BaseSettingsSaverComponent,
)

logger = logging.getLogger(__name__)

# Snapshot-only routing keys, never persisted to the live config. They ride in
# config_snapshot so the deploy dialog can route, and the reviewer's diff
# already excludes them (ApprovalService.compute_changes) — writing them would
# put fields into the live row nobody was shown, which the next diff would then
# report as changes.
SNAPSHOT_ONLY_KEYS = (
    "ci_provider", "pendingChanges", "pendingchanges", "pending_changes",
)


class ServiceSettingsSaverComponent(BaseSettingsSaverComponent):
    """update_service → service_configs."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> bool:
        if db is None:
            return False

        # A variables row is a SERVICE_CONFIG row too, told apart only by its
        # case. Its snapshot is deliberately just an S3 pointer — the values
        # live in devlift-secret-config-manager, and config_snapshot is copied
        # verbatim into the audit table, which is exactly where secret names
        # must not go.
        if queue_dict.get("case_ref_code") == "update_variables":
            return False
        if queue_dict.get("table_name") != WorkflowSourceTableEnum.SERVICE_CONFIG:
            return False

        # Imported here rather than at module scope: this plugin is loaded from
        # a handler that the PR workflow already imports, and ServiceConfigService
        # pulls in a large slice of the app.
        from app.repository.service_config_repository import ServiceConfigRepository
        from app.repository.services_mst_repository import ServicesMstRepository
        from app.schemas.service_config_schemas import SidecarOverrideSchema
        from app.services.service_config_service import ServiceConfigService

        transaction_code = queue_dict.get("transaction_code")
        tenant_code = queue_dict.get("tenant_code") or tenant

        config_repo = ServiceConfigRepository(db)
        live = await config_repo.get_by_code_and_tenant(transaction_code, tenant_code)
        if live is None:
            self.logger.warning(
                "[SettingsSaver] no service_config %s in tenant %s to apply "
                "queue %s to", transaction_code, tenant_code, queue_dict.get("code"),
            )
            return False

        # The snapshot on the row, not the one in queue_dict: the dict was built
        # before the pipeline ran and this is the record the approval sealed.
        snapshot = queue_dict.get("config_snapshot")
        if snapshot is None:
            row = (await db.execute(
                select(TransactionQueueModel).where(
                    TransactionQueueModel.id == queue_dict.get("id"),
                    TransactionQueueModel.tenant_code == tenant_code,
                )
            )).scalars().first()
            snapshot = (row.config_snapshot if row else None) or {}
        snapshot = snapshot or {}

        # Two snapshot shapes. New ones nest the form's payload under `config`
        # and keep queue identity at the root; older flat ones share the root,
        # with nothing structural to tell them apart — so a flat snapshot is
        # written whole, as it always was.
        nested = snapshot.get("config")
        config_dict = dict(nested) if isinstance(nested, dict) else dict(snapshot)
        for key in SNAPSHOT_ONLY_KEYS:
            config_dict.pop(key, None)
        if not config_dict:
            return False

        # A column, not a config key. New snapshots carry it inside the nested
        # payload; the root read covers rows saved flat.
        language_ref_code = (
            config_dict.get("language_ref_code") or snapshot.get("language_ref_code")
        )

        # Sidecars only when the snapshot SPEAKS for them — the merge-patch rule.
        # A proposal silent about sidecars changes nothing about them, however
        # many the live column holds; "remove them all" still arrives
        # explicitly, because disabling one keeps its entry with enabled:false.
        sidecar_config: Optional[List[dict]] = None
        if "sidecar_config" in snapshot:
            overrides: List[SidecarOverrideSchema] = []
            for entry in (snapshot.get("sidecar_config") or []):
                if not isinstance(entry, dict) or not entry.get("sidecar_config_code"):
                    continue
                # The snapshot holds the form's full display objects; the schema
                # keeps only what the live column stores. Skipping an unreadable
                # entry beats failing a deploy the reviewer approved over a
                # cosmetic field.
                try:
                    overrides.append(SidecarOverrideSchema(**entry))
                except Exception:  # noqa: BLE001
                    self.logger.warning(
                        "[SettingsSaver] queue %s has an unreadable sidecar "
                        "entry %s — skipped",
                        queue_dict.get("code"), entry.get("sidecar_config_code"),
                    )
            service = await ServicesMstRepository(db).get_by_code(live.services_mst_code)
            if service is not None:
                # Mapped through the SAME routine the settings save uses, so both
                # paths leave the column in one shape. It resolves each sidecar
                # to this environment's own code and is idempotent when the
                # snapshot already carries them.
                sidecar_config = await ServiceConfigService(db)._map_sidecars_to_environment(
                    overrides,
                    service.applications_mst_code,
                    service.resource_group_mst_code,
                    getattr(live.environment, "value", live.environment),
                )

        # The repository the settings screen already uses, which MERGES config
        # rather than replacing it — so keys and columns this change does not
        # carry are left alone.
        await config_repo.update_service_config_fields(
            config=live,
            config_dict=config_dict,
            language_ref_code=language_ref_code,
            sidecar_config=sidecar_config,
        )
        self.logger.info(
            "[SettingsSaver] applied queue %s to service_config %s",
            queue_dict.get("code"), transaction_code,
        )
        return True
