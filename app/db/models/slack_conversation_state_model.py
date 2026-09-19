from sqlalchemy import Column, String, TIMESTAMP, ForeignKey, BigInteger, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.models.base_model import Base


class SlackConversationState(Base):
    """SQLAlchemy model for Slack conversation state management.

    Tracks conversation context, pending selections, and deployment approvals
    for Slack-based infrastructure creation workflows.
    """

    __tablename__ = "slack_conversation_state"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slack_channel_id = Column(String(20), nullable=False, index=True)
    slack_thread_ts = Column(String(20), nullable=False, index=True)
    chat_info_code = Column(
        String(100),
        ForeignKey("chat_info.code", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    context = Column(JSON, nullable=True)  # Stores infrastructure context (product, service, etc.)
    pending_selection_type = Column(String(50), nullable=True)  # e.g., "product", "environment", "geo_loc"
    pending_deployment = Column(JSON, nullable=True)  # Stores deployment info awaiting approval
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)  # For automatic cleanup

    # Relationships
    chat_info = relationship("ChatInfoModel", foreign_keys=[chat_info_code])

    def __repr__(self):
        return (
            f"<SlackConversationState(channel={self.slack_channel_id}, "
            f"thread={self.slack_thread_ts}, chat={self.chat_info_code})>"
        )

    # Unique constraint on channel + thread (one state per thread)
    __table_args__ = (
        {"schema": None},
    )
