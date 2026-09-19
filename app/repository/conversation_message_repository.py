import uuid

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation_message_model import ConversationMessageModel


class ConversationMessageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, session_id: int, role: str, message: str) -> ConversationMessageModel:
        msg = ConversationMessageModel(
            code=str(uuid.uuid4()),
            name=f"msg-{role}",
            session_id=session_id,
            role=role,
            message=message,
        )
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def get_by_session(self, session_id: int, limit: int = 50) -> list[ConversationMessageModel]:
        stmt = (
            select(ConversationMessageModel)
            .where(
                and_(
                    ConversationMessageModel.session_id == session_id,
                    ConversationMessageModel.is_deleted == False,
                )
            )
            .order_by(ConversationMessageModel.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
