"""
Transaction Queue Workflow Mapping Model

Stores the many-to-many relationship between transaction_queue items and gitops_workflow_detail.
This allows multiple queue items to be associated with a single workflow (e.g., when deploying
multiple items in a single PR), and tracks which workflow processed which queue items.
"""

from sqlalchemy import Column, String, BigInteger, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class TransactionQueueWorkflowMappingModel(BaseModel):
    """
    Model for mapping transaction_queue items to gitops_workflow_detail.

    This is a junction table that links queue items to the workflows that processed them.
    Each record represents a single queue item being processed by a workflow.

    Inherits from BaseModel:
        - id, code, name, description
        - created_at, updated_at
        - is_deleted, is_active

    Usage:
        1. When a workflow is created for one or more queue items, create mapping records
        2. Track which items were included in which workflow/PR
        3. Enable reverse lookups from workflows to queue items and vice versa
    """

    __tablename__ = "transaction_queue_workflow_mapping"

    # Foreign Key to transaction_queue (using code instead of id)
    transaction_queue_code = Column(
        String(100),
        ForeignKey("transaction_queue.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Foreign key reference to transaction_queue.code"
    )

    # Foreign Key to gitops_workflow_detail (using code instead of id)
    gitops_workflow_code = Column(
        String(100),
        ForeignKey("gitops_workflow_detail.code", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Foreign key reference to gitops_workflow_detail.code"
    )

    # Relationships
    transaction_queue = relationship(
        "TransactionQueueModel",
        foreign_keys=[transaction_queue_code],
        back_populates="workflow_mappings"
    )

    gitops_workflow = relationship(
        "GitopsWorkflowDetailModel",
        foreign_keys=[gitops_workflow_code],
        back_populates="transaction_queue_mappings"
    )

    # Composite indexes for efficient lookups
    __table_args__ = (
        Index('idx_transaction_queue_workflow_mapping_queue_workflow', 'transaction_queue_code', 'gitops_workflow_code'),
        Index('idx_transaction_queue_workflow_mapping_workflow_queue', 'gitops_workflow_code', 'transaction_queue_code'),
    )

    def __repr__(self):
        return f"<TransactionQueueWorkflowMappingModel(id={self.id}, code={self.code}, queue_code={self.transaction_queue_code}, workflow_code={self.gitops_workflow_code})>"
