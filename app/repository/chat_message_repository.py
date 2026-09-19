"""
Chat Message Repository for managing chat messages
"""
from typing import List, Optional
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.chat_message_model import ChatMessageModel
from app.repository.base_repository import BaseRepository


class ChatMessageRepository(BaseRepository[ChatMessageModel]):
    """Repository for chat_message table operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ChatMessageModel, session)

    async def get_messages_by_chat(
        self,
        chat_info_code: str,
        limit: Optional[int] = None,
        include_summarized: bool = True,
        case_code: Optional[str] = None,
    ) -> List[ChatMessageModel]:
        """
        Get messages for a specific chat session.

        Args:
            chat_info_code: The chat session code
            limit: Max number of messages to return (most recent)
            include_summarized: Whether to include messages that have been summarized
            case_code: Optional filter by case code

        Returns:
            List of ChatMessageModel ordered by created_at ascending
        """
        stmt = select(ChatMessageModel).where(
            ChatMessageModel.chat_info_code == chat_info_code
        )

        if case_code:
            stmt = stmt.where(ChatMessageModel.case_code == case_code)

        if not include_summarized:
            stmt = stmt.where(ChatMessageModel.summary_status == False)

        stmt = stmt.order_by(ChatMessageModel.id.asc())

        if limit:
            # Get the last N messages
            # First count total messages
            count_stmt = select(func.count()).select_from(ChatMessageModel).where(
                ChatMessageModel.chat_info_code == chat_info_code
            )
            if case_code:
                count_stmt = count_stmt.where(ChatMessageModel.case_code == case_code)
            if not include_summarized:
                count_stmt = count_stmt.where(ChatMessageModel.summary_status == False)

            count_result = await self.session.execute(count_stmt)
            total = count_result.scalar() or 0

            # Calculate offset to get last N messages
            offset = max(0, total - limit)
            stmt = stmt.offset(offset).limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_unsummarized_messages(
        self,
        chat_info_code: str,
        exclude_last_n: int = 10,
        case_code: Optional[str] = None,
    ) -> List[ChatMessageModel]:
        """
        Get unsummarized messages excluding the last N messages.
        Used for generating summaries.

        Args:
            chat_info_code: The chat session code
            exclude_last_n: Number of recent messages to exclude (default 10)
            case_code: Optional filter by case code

        Returns:
            List of unsummarized messages (excluding last N) ordered by created_at
        """
        # Get total count of unsummarized messages
        filters = [
            ChatMessageModel.chat_info_code == chat_info_code,
            ChatMessageModel.summary_status == False
        ]
        if case_code:
            filters.append(ChatMessageModel.case_code == case_code)

        count_stmt = select(func.count()).select_from(ChatMessageModel).where(and_(*filters))
        count_result = await self.session.execute(count_stmt)
        total_unsummarized = count_result.scalar() or 0

        if total_unsummarized <= exclude_last_n:
            # Not enough messages to summarize
            return []

        # Get messages to summarize (all except last N)
        messages_to_summarize = total_unsummarized - exclude_last_n

        stmt = (
            select(ChatMessageModel)
            .where(and_(*filters))
            .order_by(ChatMessageModel.created_at.asc())
            .limit(messages_to_summarize)
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_messages(
        self,
        chat_info_code: str,
        include_summarized: bool = True,
        case_code: Optional[str] = None,
    ) -> int:
        """Count total messages for a chat session"""
        stmt = select(func.count()).select_from(ChatMessageModel).where(
            ChatMessageModel.chat_info_code == chat_info_code
        )

        if case_code:
            stmt = stmt.where(ChatMessageModel.case_code == case_code)

        if not include_summarized:
            stmt = stmt.where(ChatMessageModel.summary_status == False)

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def mark_as_summarized(self, message_ids: List[int]) -> int:
        """
        Mark multiple messages as summarized.

        Args:
            message_ids: List of message IDs to mark as summarized

        Returns:
            Number of messages updated
        """
        if not message_ids:
            return 0

        stmt = select(ChatMessageModel).where(ChatMessageModel.id.in_(message_ids))
        result = await self.session.execute(stmt)
        messages = list(result.scalars().all())

        for message in messages:
            message.summary_status = True

        await self.session.flush()
        return len(messages)

    async def create_message(
        self,
        chat_info_code: str,
        role: str,
        message: str,
        case_code: str = "general_chat",
    ) -> ChatMessageModel:
        """
        Create a new chat message.

        Args:
            chat_info_code: The chat session code
            role: Message role ('user' or 'agent')
            message: The message content
            case_code: Case code from case_ref table (defaults to 'general_chat')

        Returns:
            Created ChatMessageModel
        """
        import hashlib
        import time

        # Generate unique code for message
        unique_string = f"{chat_info_code}_{role}_{time.time()}"
        msg_code = f"MSG_{hashlib.md5(unique_string.encode()).hexdigest()[:16].upper()}"

        # Generate name (truncate message if too long)
        msg_preview = message[:50] + "..." if len(message) > 50 else message
        msg_name = f"{role.capitalize()} message"

        return await self.create(
            code=msg_code,
            name=msg_name,
            description=msg_preview,
            chat_info_code=chat_info_code,
            role=role,
            message=message,
            summary_status=False,
            case_code=case_code
        )
