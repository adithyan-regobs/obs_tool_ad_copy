from sqlalchemy import Column, String, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ChatMessageModel(BaseModel):
    """
    Chat Message table.
    Stores individual messages within a chat conversation.
    Each message belongs to a chat_info session and has a role (user or agent).
    """

    __tablename__ = "chat_message"

    # Foreign Key to ChatInfoModel
    chat_info_code = Column(
        String(100),
        ForeignKey("chat_info.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Role: user or agent
    role = Column(
        String(20),
        nullable=False,
    )

    # The actual message content
    message = Column(
        Text,
        nullable=False,
    )

    # Summary status - indicates if this message has been summarized
    summary_status = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    # Foreign Key to CaseRefModel
    case_code = Column(
        String(100),
        ForeignKey("case_ref.code", ondelete="RESTRICT"),
        nullable=False,
        default="general_chat",
    )

    # Relationship to ChatInfoModel
    chat_info = relationship(
        "ChatInfoModel",
        back_populates="chat_messages",
        foreign_keys=[chat_info_code],
    )

    # Relationship to CaseRefModel
    case_ref = relationship(
        "CaseRefModel",
        foreign_keys=[case_code],
    )

    def __repr__(self):
        return (
            f"<ChatMessage(id={self.id}, code='{self.code}', "
            f"chat_info_code='{self.chat_info_code}', "
            f"role='{self.role}', "
            f"case_code='{self.case_code}', "
            f"summary_status={self.summary_status}, "
            f"created_at='{self.created_at}')>"
        )
