from sqlalchemy import Column, String, Boolean, Text, Float, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class ChatHistoryModel(BaseModel):
    """
    Chat History table.
    Stores all infra chat agent conversation messages with metadata.
    Each message contains role, content, LLM tracking info, and optional parameters.
    Thread grouping is handled externally (not stored in this table).
    """

    __tablename__ = "chat_history"

    # Message content
    role = Column(
        String(20),
        nullable=False,
        comment='Message role: "user" or "agent"',
    )

    message = Column(
        Text,
        nullable=False,
        comment="The actual message text",
    )

    # LLM tracking fields
    llm_model = Column(
        String(100),
        nullable=True,
        comment="LLM model used (e.g., gpt-4, claude-3-sonnet)",
    )

    llm_purpose = Column(
        String(100),
        nullable=True,
        comment="Purpose of LLM call (e.g., intent_detection, parameter_extraction)",
    )

    summary_status = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this message has been summarized",
    )

    # Metadata (optional fields for agent responses)
    intent = Column(
        String(50),
        nullable=True,
        comment="Detected intent: CREATE, REFERENCE, QA, UNSUPPORTED",
    )

    resource = Column(
        String(50),
        nullable=True,
        comment="Resource type for CREATE intent: s3, sqs, dynamodb, etc.",
    )

    placement_parameters = Column(
        JSONB,
        nullable=True,
        comment="Placement parameters (environment, geo_loc, application, etc.)",
    )

    attribute_parameters = Column(
        JSONB,
        nullable=True,
        comment="Attribute parameters (identifier, versioning, cross_account_id, etc.)",
    )

    is_ready = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="True when all required parameters are collected",
    )

    # Thread tracking
    thread_id = Column(
        String(200),
        nullable=True,
        comment="Thread identifier in format tenant_id:conversation_id",
    )

    # Foreign Keys for multi-tenancy and user tracking
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Tenant code (FK to tenants_mst.code)"
    )

    user_mst_code = Column(
        String(100),
        ForeignKey("user_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="User code (FK to user_mst.code) - which user created this message"
    )

    # Workflow execution tracking
    workflow_phase = Column(
        String(50),
        nullable=True,
        comment="Workflow phase: init, intent_detection, parameter_extraction, parameter_collection, validation, confirmation, execution, response_generation"
    )

    confidence = Column(
        Float,
        nullable=True,
        comment="LLM confidence score (0.0 to 1.0) for intent detection"
    )

    reasoning = Column(
        Text,
        nullable=True,
        comment="LLM reasoning/explanation for decisions made"
    )

    extraction_method = Column(
        String(20),
        nullable=True,
        comment="Method used: 'llm', 'deterministic', 'manual'"
    )

    # Relationships
    tenant = relationship(
        "TenantsMstModel",
        foreign_keys=[tenants_mst_code],
        backref="chat_history"
    )

    user = relationship(
        "UserMstModel",
        foreign_keys=[user_mst_code],
        backref="chat_history"
    )

    def __repr__(self):
        return (
            f"<ChatHistory(id={self.id}, code='{self.code}', "
            f"role='{self.role}', "
            f"intent='{self.intent}', "
            f"resource='{self.resource}', "
            f"is_ready={self.is_ready}, "
            f"created_at='{self.created_at}')>"
        )
