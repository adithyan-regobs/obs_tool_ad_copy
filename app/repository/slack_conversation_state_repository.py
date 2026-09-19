from typing import Optional, Dict, Any
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from app.db.models.slack_conversation_state_model import SlackConversationState


class SlackConversationStateRepository:
    """Repository for managing Slack conversation state persistence."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_thread(
        self,
        channel_id: str,
        thread_ts: str
    ) -> Optional[SlackConversationState]:
        """Get conversation state by Slack channel and thread timestamp."""
        stmt = select(SlackConversationState).where(
            SlackConversationState.slack_channel_id == channel_id,
            SlackConversationState.slack_thread_ts == thread_ts
        ).execution_options(populate_existing=True)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def create(
        self,
        channel_id: str,
        thread_ts: str,
        chat_info_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        ttl_hours: int = 24
    ) -> SlackConversationState:
        """Create new conversation state."""
        expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)

        state = SlackConversationState(
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            chat_info_code=chat_info_code,
            context=context or {},
            expires_at=expires_at
        )

        self.db.add(state)
        await self.db.commit()
        await self.db.refresh(state)
        return state

    async def update_context(
        self,
        channel_id: str,
        thread_ts: str,
        context: Dict[str, Any]
    ) -> Optional[SlackConversationState]:
        """Update conversation context.

        Uses atomic UPDATE to avoid session cache issues.
        """
        from sqlalchemy import update

        stmt = (
            update(SlackConversationState)
            .where(
                SlackConversationState.slack_channel_id == channel_id,
                SlackConversationState.slack_thread_ts == thread_ts
            )
            .values(
                context=context,
                updated_at=datetime.utcnow()
            )
            .execution_options(synchronize_session=False)
        )

        await self.db.execute(stmt)
        await self.db.commit()

        # Fetch fresh state from database (bypass session cache with populate_existing)
        stmt = select(SlackConversationState).where(
            SlackConversationState.slack_channel_id == channel_id,
            SlackConversationState.slack_thread_ts == thread_ts
        ).execution_options(populate_existing=True)

        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def set_pending_selection(
        self,
        channel_id: str,
        thread_ts: str,
        selection_type: str
    ) -> Optional[SlackConversationState]:
        """Set pending selection type (e.g., 'product', 'environment')."""
        state = await self.get_by_thread(channel_id, thread_ts)
        if state:
            state.pending_selection_type = selection_type
            state.updated_at = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(state)
        return state

    async def clear_pending_selection(
        self,
        channel_id: str,
        thread_ts: str
    ) -> Optional[SlackConversationState]:
        """Clear pending selection after user responds.

        Uses atomic UPDATE to avoid overwriting concurrent context changes.
        """
        from sqlalchemy import update

        stmt = (
            update(SlackConversationState)
            .where(
                SlackConversationState.slack_channel_id == channel_id,
                SlackConversationState.slack_thread_ts == thread_ts
            )
            .values(
                pending_selection_type=None,
                updated_at=datetime.utcnow()
            )
            .execution_options(synchronize_session=False)
        )

        await self.db.execute(stmt)
        await self.db.commit()

        # Fetch and return the updated state
        return await self.get_by_thread(channel_id, thread_ts)

    async def set_pending_deployment(
        self,
        channel_id: str,
        thread_ts: str,
        deployment_info: Dict[str, Any]
    ) -> Optional[SlackConversationState]:
        """Store deployment info awaiting approval."""
        state = await self.get_by_thread(channel_id, thread_ts)
        if state:
            state.pending_deployment = deployment_info
            state.updated_at = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(state)
        return state

    async def clear_pending_deployment(
        self,
        channel_id: str,
        thread_ts: str
    ) -> Optional[SlackConversationState]:
        """Clear pending deployment after approval/rejection."""
        state = await self.get_by_thread(channel_id, thread_ts)
        if state:
            state.pending_deployment = None
            state.updated_at = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(state)
        return state

    async def link_chat_info(
        self,
        channel_id: str,
        thread_ts: str,
        chat_info_code: str
    ) -> Optional[SlackConversationState]:
        """Link conversation state to chat_info record."""
        state = await self.get_by_thread(channel_id, thread_ts)
        if state:
            state.chat_info_code = chat_info_code
            state.updated_at = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(state)
        return state

    async def delete_by_thread(
        self,
        channel_id: str,
        thread_ts: str
    ) -> bool:
        """Delete conversation state."""
        stmt = delete(SlackConversationState).where(
            SlackConversationState.slack_channel_id == channel_id,
            SlackConversationState.slack_thread_ts == thread_ts
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def cleanup_expired(self) -> int:
        """Delete expired conversation states (for scheduled cleanup job)."""
        stmt = delete(SlackConversationState).where(
            SlackConversationState.expires_at < datetime.utcnow()
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount
