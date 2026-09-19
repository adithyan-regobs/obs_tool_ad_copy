import uuid

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat_session_model import ChatSessionModel

_UNSET = object()


class ChatSessionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def find_by_ticket(self, ticket_code: str) -> ChatSessionModel | None:
        stmt = select(ChatSessionModel).where(
            and_(
                ChatSessionModel.ticket_code == ticket_code,
                ChatSessionModel.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        ticket_code: str,
        tenant_code: str,
        user_mst_code: str,
        form_id: str,
    ) -> ChatSessionModel:
        session_obj = ChatSessionModel(
            code=str(uuid.uuid4()),
            name=f"session-{ticket_code}",
            ticket_code=ticket_code,
            tenant_code=tenant_code,
            user_mst_code=user_mst_code,
            form_id=form_id,
            status="pending",
            collected_data={},
        )
        self.session.add(session_obj)
        await self.session.flush()
        return session_obj

    async def find_or_create(
        self,
        ticket_code: str,
        tenant_code: str,
        user_mst_code: str,
        form_id: str,
    ) -> tuple[ChatSessionModel, bool]:
        existing = await self.find_by_ticket(ticket_code)
        if existing:
            return existing, False
        new_session = await self.create(ticket_code, tenant_code, user_mst_code, form_id)
        return new_session, True

    async def update_state(
        self,
        session_obj: ChatSessionModel,
        collected_data: dict | None = None,
        currently_asking: str | None = _UNSET,
        status: str | None = None,
        skipped_fields: list | None = _UNSET,
        api_dropdown_cache: dict | None = _UNSET,
    ):
        if collected_data is not None:
            session_obj.collected_data = collected_data
        if currently_asking is not _UNSET:
            session_obj.currently_asking = currently_asking
        if status is not None:
            session_obj.status = status
        if skipped_fields is not _UNSET:
            session_obj.skipped_fields = skipped_fields
        if api_dropdown_cache is not _UNSET:
            session_obj.api_dropdown_cache = api_dropdown_cache
        await self.session.flush()
