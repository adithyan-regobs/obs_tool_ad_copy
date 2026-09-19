from sqlalchemy import Column, String, ForeignKey, Text, BigInteger
from sqlalchemy.orm import relationship

from app.db.models.base_model import BaseModel


class ConversationMessageModel(BaseModel):
    """Individual message in a chat session."""

    __tablename__ = "conversation_message_model"

    session_id = Column(BigInteger, ForeignKey("chat_session_model.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    message = Column(Text, nullable=False)

    session = relationship("ChatSessionModel", back_populates="messages")
