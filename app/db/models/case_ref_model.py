from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class CaseRefModel(BaseModel):
    __tablename__ = "case_ref"

    case_type_ref_code = Column(
        String(100),
        ForeignKey("case_type_ref.code", ondelete="SET NULL"),
        nullable=True,
    )
    prompt_file_path = Column(String(500), nullable=True)

    # Relationships
    transaction_queues = relationship("TransactionQueueModel", back_populates="case_ref")
