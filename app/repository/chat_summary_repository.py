"""
Chat Summary Repository for managing conversation summaries
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.chat_summary_model import ChatSummaryModel
from app.repository.base_repository import BaseRepository


class ChatSummaryRepository(BaseRepository[ChatSummaryModel]):
    """Repository for chat_summary table operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ChatSummaryModel, session)

    async def get_by_chat_and_case(
        self,
        chat_info_code: str,
        case_code: str
    ) -> Optional[ChatSummaryModel]:
        """Get summary for a specific chat session and case code"""
        stmt = select(ChatSummaryModel).where(
            ChatSummaryModel.chat_info_code == chat_info_code,
            ChatSummaryModel.case_code == case_code
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_or_update_summary(
        self,
        chat_info_code: str,
        case_code: str,
        summary_text: str,
        message_count: int,
    ) -> ChatSummaryModel:
        """
        Create new summary or update existing one for a specific case.

        Args:
            chat_info_code: The chat session code
            case_code: The case code from case_ref table
            summary_text: The generated summary text
            message_count: Number of messages summarized

        Returns:
            Created or updated ChatSummaryModel
        """
        existing_summary = await self.get_by_chat_and_case(chat_info_code, case_code)

        if existing_summary:
            # Update existing summary
            existing_summary.summary_text = summary_text
            existing_summary.message_count = message_count
            self.session.add(existing_summary)
            await self.session.flush()
            await self.session.refresh(existing_summary)
            return existing_summary
        else:
            # Create new summary
            import hashlib
            import time
            unique_string = f"{chat_info_code}_{case_code}_{time.time()}"
            summary_code = f"SUMMARY_{hashlib.md5(unique_string.encode()).hexdigest()[:16].upper()}"
            summary_preview = summary_text[:100] + "..." if len(summary_text) > 100 else summary_text

            return await self.create(
                code=summary_code,
                name=f"{chat_info_code[:10]}-{case_code[:20]}",
                description=summary_preview,
                chat_info_code=chat_info_code,
                case_code=case_code,
                summary_text=summary_text,
                message_count=message_count
            )
