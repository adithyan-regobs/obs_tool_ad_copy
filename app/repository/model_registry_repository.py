"""
Repository for model_registry_mst table operations.
"""
import uuid
from typing import Optional

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.model_registry_model import ModelRegistryModel


class ModelRegistryRepository:
    """Data access layer for model_registry_mst."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_shared(self, model_id: str, revision: str) -> Optional[ModelRegistryModel]:
        """Return the shared (tenant_code IS NULL) registry record for a model+revision pair."""
        result = await self.session.execute(
            select(ModelRegistryModel).where(
                and_(
                    ModelRegistryModel.model_id == model_id,
                    ModelRegistryModel.revision == revision,
                    ModelRegistryModel.tenant_code.is_(None),
                    ModelRegistryModel.is_deleted.is_(False),
                )
            )
        )
        return result.scalars().first()

    async def create_shared(
        self,
        model_id: str,
        revision: str,
        efs_path: str,
    ) -> ModelRegistryModel:
        """
        Insert a shared registry record with download_status='pending'.

        Safe to call multiple times — callers should check get_shared() first
        to avoid duplicate inserts (the migration enforces a unique index).
        """
        record = ModelRegistryModel(
            code=f"mr-{uuid.uuid4().hex[:12]}",
            name=f"{model_id}@{revision}",
            model_id=model_id,
            revision=revision,
            efs_path=efs_path,
            download_status="pending",
            tenant_code=None,
            is_deleted=False,
            is_active=True,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def mark_ready(self, model_id: str, revision: str) -> bool:
        """
        Set download_status='ready' for the shared record matching model_id+revision.
        Creates the record if it doesn't exist (upsert).
        Returns True if a row was updated or created.
        """
        result = await self.session.execute(
            update(ModelRegistryModel)
            .where(
                and_(
                    ModelRegistryModel.model_id == model_id,
                    ModelRegistryModel.revision == revision,
                    ModelRegistryModel.tenant_code.is_(None),
                    ModelRegistryModel.is_deleted.is_(False),
                )
            )
            .values(download_status="ready")
        )
        if result.rowcount > 0:
            return True
        # Record doesn't exist — create it (covers cases where create_shared was skipped)
        from app.services.service_config_service import build_efs_path
        efs_path = build_efs_path(model_id, revision)
        record = ModelRegistryModel(
            code=f"mr-{uuid.uuid4().hex[:12]}",
            name=f"{model_id}@{revision}",
            model_id=model_id,
            revision=revision,
            efs_path=efs_path,
            download_status="ready",
            tenant_code=None,
            is_deleted=False,
            is_active=True,
        )
        self.session.add(record)
        await self.session.flush()
        return True

    async def mark_failed(self, model_id: str, revision: str, error: str = "") -> bool:
        """Set download_status='failed' with an optional error message."""
        result = await self.session.execute(
            update(ModelRegistryModel)
            .where(
                and_(
                    ModelRegistryModel.model_id == model_id,
                    ModelRegistryModel.revision == revision,
                    ModelRegistryModel.tenant_code.is_(None),
                    ModelRegistryModel.is_deleted.is_(False),
                )
            )
            .values(download_status="failed", error_message=error)
        )
        return result.rowcount > 0
