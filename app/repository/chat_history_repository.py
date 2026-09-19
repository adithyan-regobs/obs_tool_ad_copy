"""
Chat History Repository for managing chat history records
"""
from typing import List, Optional, Dict
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.chat_history_model import ChatHistoryModel
from app.db.models.ticket_model import TicketModel
from app.repository.base_repository import BaseRepository


class ChatHistoryRepository(BaseRepository[ChatHistoryModel]):
    """Repository for chat_history table operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ChatHistoryModel, session)

    async def create(
        self,
        code: str,
        name: str,
        description: str,
        role: str,
        message: str,
        thread_id: str,
        intent: Optional[str] = None,
        resource: Optional[str] = None,
        placement_parameters: Optional[Dict] = None,
        attribute_parameters: Optional[Dict] = None,
        is_ready: bool = False,
        llm_model: Optional[str] = None,
        llm_purpose: Optional[str] = None,
        summary_status: bool = False,
        # NEW PARAMETERS for context tracking
        tenants_mst_code: Optional[str] = None,
        user_mst_code: Optional[str] = None,
        workflow_phase: Optional[str] = None,
        confidence: Optional[float] = None,
        reasoning: Optional[str] = None,
        extraction_method: Optional[str] = None,
    ) -> ChatHistoryModel:
        """
        Create new chat history record with all metadata.

        Args:
            code: Unique code for the record
            name: Display name
            description: Description
            role: Message role ("user" or "agent")
            message: The actual message text
            thread_id: Thread identifier (tenant_code:conversation_id)
            intent: Detected intent (CREATE, REFERENCE, QA, UNSUPPORTED)
            resource: Resource type (s3, sqs, etc.)
            placement_parameters: Placement params (environment, geo_loc, etc.)
            attribute_parameters: Attribute params (bucket_name, etc.)
            is_ready: Whether all required parameters are collected
            llm_model: LLM model used (gpt-4, claude-3-sonnet, etc.)
            llm_purpose: Purpose of LLM call (intent_detection, parameter_extraction, etc.)
            summary_status: Whether message has been summarized
            tenants_mst_code: Tenant code (FK to tenants_mst.code)
            user_mst_code: User code (FK to user_mst.code)
            workflow_phase: Workflow phase (intent_detection, parameter_extraction, etc.)
            confidence: LLM confidence score (0.0 to 1.0)
            reasoning: LLM reasoning/explanation
            extraction_method: Method used ('llm', 'deterministic', 'manual')

        Returns:
            Created ChatHistoryModel instance
        """
        return await super().create(
            code=code,
            name=name,
            description=description,
            role=role,
            message=message,
            thread_id=thread_id,
            intent=intent,
            resource=resource,
            placement_parameters=placement_parameters,
            attribute_parameters=attribute_parameters,
            is_ready=is_ready,
            llm_model=llm_model,
            llm_purpose=llm_purpose,
            summary_status=summary_status,
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            workflow_phase=workflow_phase,
            confidence=confidence,
            reasoning=reasoning,
            extraction_method=extraction_method,
        )

    async def get_messages_by_criteria(
        self,
        role: Optional[str] = None,
        intent: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get chat history messages filtered by criteria.

        Args:
            role: Filter by message role ("user" or "agent")
            intent: Filter by detected intent
            limit: Max number of messages to return (most recent)

        Returns:
            List of ChatHistoryModel ordered by created_at descending
        """
        stmt = select(ChatHistoryModel).where(ChatHistoryModel.is_active == True)

        if role:
            stmt = stmt.where(ChatHistoryModel.role == role)

        if intent:
            stmt = stmt.where(ChatHistoryModel.intent == intent)

        stmt = stmt.order_by(ChatHistoryModel.created_at.desc())

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_unsummarized_messages(
        self,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get messages that have not been summarized.

        Args:
            limit: Max number of messages to return

        Returns:
            List of unsummarized messages ordered by created_at ascending
        """
        stmt = (
            select(ChatHistoryModel)
            .where(
                ChatHistoryModel.is_active == True,
                ChatHistoryModel.summary_status == False,
            )
            .order_by(ChatHistoryModel.created_at.asc())
        )

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_messages_by_thread_id(
        self,
        thread_id: str,
        role: Optional[str] = None,
        intent: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get chat messages for a specific thread (conversation).

        Args:
            thread_id: Thread identifier (format: tenant_code:conversation_id)
            role: Optional filter by role ("user" or "agent")
            intent: Optional filter by intent (CREATE, REFERENCE, QA, UNSUPPORTED)
            limit: Optional limit on number of messages

        Returns:
            List of ChatHistoryModel objects ordered by id (ascending)
        """
        filters = [
            ChatHistoryModel.thread_id == thread_id,
            ChatHistoryModel.is_active == True,
        ]

        if role:
            filters.append(ChatHistoryModel.role == role)

        if intent:
            filters.append(ChatHistoryModel.intent == intent)

        stmt = (
            select(ChatHistoryModel)
            .where(and_(*filters))
            .order_by(ChatHistoryModel.id.asc())
        )

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_user_agent_conversation_display_history(
        self,
        thread_id: str,
        intent: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get conversation display history for UI (excludes internal LLM workflow trail).

        This method filters out role="llm" entries (intent detection, parameter extraction, etc.)
        and returns only user messages and final agent responses for display in the conversation UI.

        Args:
            thread_id: Thread identifier (format: tenant_code:conversation_id)
            intent: Optional filter by intent (CREATE, REFERENCE, QA, UNSUPPORTED)
            limit: Optional limit on number of messages

        Returns:
            List of ChatHistoryModel objects with role in ["user", "agent"] ordered by id (ascending)
        """
        filters = [
            ChatHistoryModel.thread_id == thread_id,
            ChatHistoryModel.is_active == True,
            ChatHistoryModel.role.in_(["user", "agent"])  # Exclude role="llm" internal responses
        ]

        if intent:
            filters.append(ChatHistoryModel.intent == intent)

        stmt = (
            select(ChatHistoryModel)
            .where(and_(*filters))
            .order_by(ChatHistoryModel.id.asc())
        )

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_user_agent_conversation_display_history_by_ticket_id(
        self,
        ticket_id: int,
        tenant_code: str,
        intent: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get conversation display history by ticket ID.

        Joins ticket -> chat_history using ticket.code == chat_history.thread_id.
        Filters to tenant and excludes role="llm" entries.
        """
        filters = [
            TicketModel.id == ticket_id,
            TicketModel.tenants_mst_code == tenant_code,
            ChatHistoryModel.is_active == True,
            ChatHistoryModel.role.in_(["user", "agent"]),
        ]

        if intent:
            filters.append(ChatHistoryModel.intent == intent)

        stmt = (
            select(ChatHistoryModel)
            .join(TicketModel, TicketModel.code == ChatHistoryModel.thread_id)
            .where(and_(*filters))
            .order_by(ChatHistoryModel.id.asc())
        )

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_qa_conversation_history(
        self,
        thread_id: str,
        limit: Optional[int] = None,
    ) -> List[ChatHistoryModel]:
        """
        Get QA conversation history (user questions + QA LLM answers only).

        Filters to only:
        - User messages (role='user')
        - QA answering records (role='agent' AND llm_purpose='qa_answering')

        Excludes all other workflow records (intent detection, parameter extraction, etc.)

        Args:
            thread_id: Thread identifier (format: tenant_code:conversation_id)
            limit: Optional limit on number of messages

        Returns:
            List of ChatHistoryModel objects ordered by id (ascending/chronological)
        """
        from sqlalchemy import or_

        stmt = (
            select(ChatHistoryModel)
            .where(
                and_(
                    ChatHistoryModel.thread_id == thread_id,
                    ChatHistoryModel.is_active == True,
                    or_(
                        ChatHistoryModel.role == "user",  # All user messages
                        and_(
                            ChatHistoryModel.role == "agent",
                            ChatHistoryModel.llm_purpose == "qa_answering"  # Only QA responses
                        )
                    )
                )
            )
            .order_by(ChatHistoryModel.id.asc())  # Chronological order
        )

        if limit:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())
