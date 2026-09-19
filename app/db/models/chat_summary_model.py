from sqlalchemy import Column, String, ForeignKey, Text, Integer
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ChatSummaryModel(BaseModel):
    """
    Chat Summary table.
    Stores summarized version of chat conversations.
    Used to provide context without loading entire message history.
    """

    __tablename__ = "chat_summary"

    # Foreign Key to ChatInfoModel
    chat_info_code = Column(
        String(100),
        ForeignKey("chat_info.code", ondelete="CASCADE"),
        nullable=False,
    )

    # Foreign Key to CaseRefModel
    case_code = Column(
        String(100),
        ForeignKey("case_ref.code", ondelete="RESTRICT"),
        nullable=False,
        default="general_chat",
    )

    # The summary text of previous messages
    summary_text = Column(
        Text,
        nullable=False,
    )

    # Number of messages that were summarized
    message_count = Column(
        Integer,
        nullable=False,
        default=0,
    )

    # Relationship to ChatInfoModel
    chat_info = relationship(
        "ChatInfoModel",
        back_populates="chat_summaries",
        foreign_keys=[chat_info_code],
    )

    # Relationship to CaseRefModel
    case_ref = relationship(
        "CaseRefModel",
        foreign_keys=[case_code],
    )

    def __repr__(self):
        return (
            f"<ChatSummary(id={self.id}, code='{self.code}', "
            f"chat_info_code='{self.chat_info_code}', "
            f"case_code='{self.case_code}', "
            f"message_count={self.message_count}, "
            f"created_at='{self.created_at}')>"
        )
