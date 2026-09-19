"""
Repository for ext_auth_code table operations
"""
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, and_, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.ext_auth_code_model import ExtAuthCodeModel


class ExtAuthCodeRepository(BaseRepository[ExtAuthCodeModel]):
    """Repository for ext_auth_code table"""

    def __init__(self, session: AsyncSession):
        super().__init__(ExtAuthCodeModel, session)

    async def create_auth_code(
        self,
        code_hash: str,
        code_prefix: str,
        client_id: str,
        user_mst_code: str,
        tenants_mst_code: str,
        clerk_jwt: str,
        token_expires_at: Optional[datetime],
        state: Optional[str],
        expires_at: datetime
    ) -> ExtAuthCodeModel:
        return await self.create(
            code_hash=code_hash,
            code_prefix=code_prefix,
            client_id=client_id,
            user_mst_code=user_mst_code,
            tenants_mst_code=tenants_mst_code,
            clerk_jwt=clerk_jwt,
            token_expires_at=token_expires_at,
            state=state,
            expires_at=expires_at,
            is_active=True,
            is_deleted=False
        )

    async def get_valid_by_hash(
        self,
        code_hash: str,
        client_id: str,
        now: datetime
    ) -> Optional[ExtAuthCodeModel]:
        stmt = select(ExtAuthCodeModel).where(
            and_(
                ExtAuthCodeModel.code_hash == code_hash,
                ExtAuthCodeModel.client_id == client_id,
                ExtAuthCodeModel.used_at.is_(None),
                ExtAuthCodeModel.expires_at > now,
                ExtAuthCodeModel.is_deleted == False,
                ExtAuthCodeModel.is_active == True
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_used(
        self,
        auth_code: ExtAuthCodeModel,
        used_at: datetime
    ) -> ExtAuthCodeModel:
        return await self.update(auth_code, {"used_at": used_at})

    async def cleanup_expired(self, now: datetime, older_than_days: int) -> int:
        if older_than_days <= 0:
            return 0

        cutoff = now - timedelta(days=older_than_days)

        stmt = delete(ExtAuthCodeModel).where(
            and_(
                ExtAuthCodeModel.created_at < cutoff,
                or_(
                    ExtAuthCodeModel.expires_at < now,
                    ExtAuthCodeModel.used_at.isnot(None)
                )
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0
