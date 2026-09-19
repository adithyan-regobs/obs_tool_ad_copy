from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel


class ChatSessionModel(BaseModel):
    """Tracks form-filling session state, linked to a ticket in the parent project."""

    __tablename__ = "chat_session_model"

    ticket_code = Column(String(100), nullable=False, index=True)
    tenant_code = Column(String(100), nullable=False)
    user_mst_code = Column(String(255), nullable=False)
    form_id = Column(String(100), nullable=True)
    status = Column(String(50), nullable=False, default="pending")
    collected_data = Column(JSONB, nullable=False, default=dict)
    currently_asking = Column(String(100), nullable=True)
    skipped_fields = Column(JSONB, nullable=False, default=list)
    api_dropdown_cache = Column(JSONB, nullable=False, default=dict)

    messages = relationship(
        "ConversationMessageModel",
        back_populates="session",
        order_by="ConversationMessageModel.created_at",
    )
