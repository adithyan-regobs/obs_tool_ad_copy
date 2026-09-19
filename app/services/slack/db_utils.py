"""Database utilities for Slack layer.

Centralizes session management for all Slack handlers including background tasks.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


@asynccontextmanager
async def managed_session() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database session with auto commit/rollback.

    Usage:
        async with managed_session() as db:
            service = SomeService(db)
            await service.do_something()
            # Auto-commits on success, rolls back on exception
    """
    async with AsyncSessionLocal() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
