import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.transaction_queue_workflow_mapping_model import TransactionQueueWorkflowMappingModel
from app.repository.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class TransactionQueueWorkflowMappingRepository(BaseRepository[TransactionQueueWorkflowMappingModel]):
    """Repository for TransactionQueueWorkflowMapping operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(TransactionQueueWorkflowMappingModel, session)
