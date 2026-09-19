"""
File Manager Handler

Orchestrates file retrieval operations by routing to appropriate storage integrations:
- If storage == "s3" → call S3Integration
- If storage == "gcp" → call GCPIntegration (TODO: not implemented yet)
- Otherwise → call default integration (TODO: not implemented yet)

This handler follows the lightweight orchestrator pattern:
- No business logic
- Routes to appropriate integration based on storage type
- Passes parameters from service layer to integration layer
"""

import logging
from typing import Dict, Any, Optional
from app.services.s3 import S3Component

logger = logging.getLogger(__name__)


class FileManagerHandler:
    """
    Handler for file management operations.

    Routes file retrieval requests to appropriate storage integrations
    based on storage type (s3, gcp, azure, etc.).

    Storage integrations are stored in: app/integrations/
    """

    STORAGE_COMPONENT_MAP = {
        's3': S3Component,
        # Future storage backends register here, e.g.:
        # 'gcs': GcsComponent,
        # 'azure_blob': AzureBlobComponent,
    }

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @classmethod
    def _get_component(cls, storage_type: str):
        """Resolve the storage component for a storage type."""
        storage_normalized = (storage_type or "s3").lower()
        component_class = cls.STORAGE_COMPONENT_MAP.get(storage_normalized)
        if not component_class:
            raise ValueError(
                f"Storage type '{storage_normalized}' not supported. "
                f"Supported types: {list(cls.STORAGE_COMPONENT_MAP.keys())}"
            )
        return component_class()

    # ── Object operations (caller-provided auth_config: bucket + credentials) ──

    @classmethod
    async def get_object(
        cls,
        auth_config: Dict[str, str],
        key: str,
        storage_type: str = "s3",
    ) -> Optional[str]:
        """Read an object's content as a UTF-8 string. None if it doesn't exist."""
        return await cls._get_component(storage_type).get_object(auth_config, key)

    @classmethod
    async def put_object(
        cls,
        auth_config: Dict[str, str],
        key: str,
        content: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
        storage_type: str = "s3",
    ) -> Dict[str, Any]:
        """Upload content to the caller's bucket/container."""
        return await cls._get_component(storage_type).put_object(
            auth_config=auth_config,
            key=key,
            content=content,
            content_type=content_type,
            metadata=metadata,
        )

    @classmethod
    async def list_objects(
        cls,
        auth_config: Dict[str, str],
        prefix: str,
        storage_type: str = "s3",
    ) -> list:
        """List object keys under a prefix. [] if the bucket is missing."""
        return await cls._get_component(storage_type).list_objects(auth_config, prefix)

    @classmethod
    async def get_objects_bulk(
        cls,
        auth_config: Dict[str, str],
        keys: list,
        storage_type: str = "s3",
        concurrency: int = 32,
    ) -> Dict[str, Optional[str]]:
        """Read many objects concurrently over one shared client.
        {key: content or None}; a missing/failed object maps to None."""
        return await cls._get_component(storage_type).get_objects_bulk(
            auth_config, keys, concurrency
        )

    @classmethod
    async def put_objects_bulk(
        cls,
        auth_config: Dict[str, str],
        items: Dict[str, str],
        content_type: str = "application/octet-stream",
        storage_type: str = "s3",
        concurrency: int = 32,
    ) -> Dict[str, bool]:
        """Write many objects concurrently over one shared client.
        {key: content} in → {key: True/False}; a per-object failure maps to False."""
        return await cls._get_component(storage_type).put_objects_bulk(
            auth_config, items, content_type, concurrency
        )

    @classmethod
    async def delete_object(
        cls,
        auth_config: Dict[str, str],
        key: str,
        storage_type: str = "s3",
    ) -> None:
        """Delete a single object. No error if it doesn't exist."""
        await cls._get_component(storage_type).delete_object(auth_config, key)

    @classmethod
    async def upload_file(
        cls,
        key: str,
        content: str,
        storage_type: str = "s3",
        content_type: str = "text/plain",
        metadata: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Upload content by routing to appropriate storage component.

        Args:
            storage_type: Type of storage ('s3', 'gcp', 'azure', etc.)
            bucket: Storage bucket/container name
            key: Storage object key
            content: File content
            content_type: MIME type (default: text/plain)
            metadata: Optional metadata dict

        Returns:
            Dict with upload response from the storage component
        """
        logger = logging.getLogger(__name__)
        storage_normalized = storage_type.lower()

        component_class = cls.STORAGE_COMPONENT_MAP.get(storage_normalized)
        if not component_class:
            logger.error(
                f"Unsupported storage type: {storage_normalized}. "
                f"Supported types: {list(cls.STORAGE_COMPONENT_MAP.keys())}"
            )
            raise ValueError(
                f"Storage type '{storage_normalized}' not supported. "
                f"Supported types: {list(cls.STORAGE_COMPONENT_MAP.keys())}"
            )

        try:
            logger.info(
                f"Uploading content using {component_class.__name__} "
            )

            component = component_class()
            result = await component.upload_to_s3(
                key=key,
                content=content,
                content_type=content_type,
                metadata=metadata
            )
            logger.info(f"Upload completed via {component_class.__name__}")
            return result
        except Exception as e:
            logger.error(
                f"Failed to upload via {component_class.__name__}: {str(e)}",
                exc_info=True
            )
            raise

    @classmethod
    async def get_presigned_url(
        cls,
        storage_type: str,
        s3_auth_config: Dict[str, str],
        s3_key: str,
        expiration: int = 3600
    ) -> Dict[str, Any]:
        """
        Generate presigned URL by routing to appropriate storage integration.

        Args:
            storage_type: Type of storage ('s3', 'gcp', 'azure', etc.)
            s3_key: Storage object key (e.g., "preview/sqs/payment-queue.hcl")
            s3_bucket: Storage bucket/container name
            s3_auth_config: Storage authentication configuration
            expiration: URL expiration time in seconds (default: 3600)

        Returns:
            Dict with presigned URL and metadata from integration layer

        Raises:
            ValueError: If storage type not supported
            Exception: If storage operation fails
        """
        logger = logging.getLogger(__name__)

        # Normalize storage type
        storage_normalized = storage_type.lower()

        try:
            logger.info(
                f"Generating presigned URL using storage component "
                f'for {storage_normalized}://{s3_auth_config.get("bucket")}/{s3_key}'
            )

            component_class = cls.STORAGE_COMPONENT_MAP.get(storage_normalized)
            if not component_class:
                logger.error(
                    f"Unsupported storage type: {storage_normalized}. "
                    f"Supported types: {list(cls.STORAGE_COMPONENT_MAP.keys())}"
                )
                raise ValueError(
                    f"Storage type '{storage_normalized}' not supported. "
                    f"Supported types: {list(cls.STORAGE_COMPONENT_MAP.keys())}"
                )

            component = component_class()
            result = await component.get_presigned_url(
                key=s3_key,
                expiration=expiration,
                auth_config=s3_auth_config
            )

            logger.info(f"Presigned URL generated successfully via {component_class.__name__}")

            return result

        except Exception as e:
            logger.error(
                f"Failed to generate presigned URL via {component_class.__name__}: {str(e)}",
                exc_info=True
            )
            raise
