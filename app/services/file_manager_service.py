"""
File Manager Service

Business logic for file management operations:
- Retrieving queue items and extracting S3 keys
- Validating S3 configuration
- Coordinating between repository, handler, and S3 integration
"""

import logging
import json
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.handlers.file_manager_handler import FileManagerHandler
from app.core.config import settings
from app.schemas.file_manager_schemas import FileTypeEnum

logger = logging.getLogger(__name__)


class FileManagerService:
    """
    Service for file management operations.

    Handles business logic for viewing HCL files stored in S3,
    including queue item retrieval, S3 key extraction, and
    presigned URL generation.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.queue_repo = TransactionQueueRepository(db)
        self.handler = FileManagerHandler()
        self.logger = logging.getLogger(__name__)

    async def get_hcl_presigned_url(
        self,
        queue_item_id: int,
        file_type: FileTypeEnum = FileTypeEnum.PREVIEW
    ) -> Dict[str, Any]:
        """
        Generate presigned URL for viewing HCL file from S3.

        Args:
            queue_item_id: ID of the queue item
            file_type: Type of file to view (preview or original)

        Returns:
            Dict with presigned URL and metadata

        Raises:
            ValueError: If queue item not found or no artifact_s3_key
            Exception: If S3 operation fails
        """
        # Get queue item
        queue_item = await self._get_queue_item(queue_item_id)

        # Extract and validate S3 key
        s3_key, s3_bucket = self._extract_s3_info(queue_item, file_type)

        # Get S3 authentication config
        s3_auth_config = self._get_s3_auth_config()

        # For now, always use S3 storage
        # TODO: In future, determine storage_type from tenant config
        storage_type = "s3"

        # Add bucket to auth config
        s3_auth_config["bucket"] = s3_bucket

        # Generate presigned URL via handler
        result = await FileManagerHandler.get_presigned_url(
            storage_type=storage_type,
            s3_key=s3_key,
            s3_auth_config=s3_auth_config,
            expiration=3600  # 1 hour default
        )

        # Add file_type to result
        result["file_type"] = file_type.value

        self.logger.info(
            f"Generated presigned URL for queue item {queue_item_id}, "
            f"file_type={file_type.value}"
        )

        return result

    async def _get_queue_item(self, queue_item_id: int) -> TransactionQueueModel:
        """
        Retrieve queue item by ID.

        Args:
            queue_item_id: Queue item ID

        Returns:
            TransactionQueueModel instance

        Raises:
            ValueError: If queue item not found
        """
        item = await self.queue_repo.get_by_id(queue_item_id)

        if not item:
            self.logger.error(f"Queue item not found: {queue_item_id}")
            raise ValueError(f"Queue item {queue_item_id} not found")

        self.logger.info(f"Retrieved queue item: {queue_item_id}")
        return item

    def _extract_s3_info(
        self,
        queue_item: TransactionQueueModel,
        file_type: FileTypeEnum
    ) -> tuple[str, str]:
        """
        Extract S3 key and bucket from queue item's artifact_s3_key.

        Args:
            queue_item: Queue item model
            file_type: Type of file (preview or original)

        Returns:
            Tuple of (s3_key, s3_bucket)

        Raises:
            ValueError: If artifact_s3_key is missing or requested key not found
        """
        # Check if artifact_s3_key exists
        if not queue_item.artifact_s3_key:
            self.logger.error(
                f"No artifact_s3_key for queue item {queue_item.id}"
            )
            raise ValueError(
                "No HCL file uploaded for this queue item. "
                "Please generate HCL first."
            )

        # Parse JSON (stored as JSON string or JSONB)
        try:
            if isinstance(queue_item.artifact_s3_key, str):
                artifact_data = json.loads(queue_item.artifact_s3_key)
            else:
                artifact_data = queue_item.artifact_s3_key
        except json.JSONDecodeError as e:
            self.logger.error(
                f"Invalid JSON in artifact_s3_key for queue item {queue_item.id}: {e}"
            )
            raise ValueError("Invalid artifact_s3_key format in database")

        # Extract S3 key based on file_type
        if file_type == FileTypeEnum.PREVIEW:
            s3_key = artifact_data.get("preview")
        else:  # ORIGINAL
            s3_key = artifact_data.get("original_s3_key")

        if not s3_key:
            self.logger.error(
                f"No {file_type.value} key in artifact_s3_key "
                f"for queue item {queue_item.id}"
            )
            raise ValueError(
                f"{file_type.value.capitalize()} file not available. "
                f"Please regenerate HCL."
            )

        # Get S3 bucket from settings
        s3_bucket = getattr(settings, 's3_upload_bucket', '')

        if not s3_bucket:
            self.logger.error("S3_UPLOAD_BUCKET not configured")
            raise ValueError(
                "S3 bucket not configured. "
                "Please contact administrator."
            )

        self.logger.info(
            f"Extracted S3 info: bucket={s3_bucket}, key={s3_key}, "
            f"type={file_type.value}"
        )

        return s3_key, s3_bucket

    def _get_s3_auth_config(self) -> Dict[str, str]:
        """
        Build S3 authentication configuration from settings.

        Returns:
            Dict with S3 authentication parameters

        Raises:
            ValueError: If required S3 settings are missing
        """
        # Check if using access key or IAM role
        access_key = getattr(settings, 's3_upload_access_key_id', None)
        secret_key = getattr(settings, 's3_upload_secret_access_key', None)
        region = getattr(settings, 's3_upload_region', 'ap-south-1')

        if access_key and secret_key:
            # Using access key authentication
            auth_config = {
                "authentication_type": "access_key",
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "region": region
            }
            self.logger.info("Using access key authentication for S3")
        else:
            # Using IAM role (default)
            auth_config = {
                "authentication_type": "iam_role",
                "region": region
            }
            self.logger.info("Using IAM role authentication for S3")

        return auth_config
