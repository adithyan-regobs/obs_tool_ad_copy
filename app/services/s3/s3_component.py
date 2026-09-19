"""
S3 Component

Centralized S3 upload wrapper using configured credentials.
"""

import logging
from typing import Dict, Optional

from app.core.config import settings
from app.integrations.s3_integration import S3Integration


class S3Component:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def upload_to_s3(
        self,
        key: str,
        content: str,
        content_type: str = "text/plain",
        metadata: Optional[Dict[str, str]] = None
    ) -> Dict:
        """Upload content to S3 and return the S3Integration response."""
        auth_config = {
            "authentication_type": "access_key",
            "aws_access_key_id": settings.s3_upload_access_key_id,
            "aws_secret_access_key": settings.s3_upload_secret_access_key,
            "region": settings.s3_upload_region,
            "bucket": settings.s3_upload_bucket
        }

        result = await S3Integration.upload_to_s3(
            auth_config=auth_config,
            key=key,
            content=content,
            content_type=content_type,
            metadata=metadata
        )
        self.logger.info(f"Uploaded content to S3: {result['location']}")
        return result

    async def get_object(
        self,
        auth_config: Dict[str, str],
        key: str,
    ) -> Optional[str]:
        """Read an object's content as a UTF-8 string. None if it doesn't exist."""
        return await S3Integration.get_object(auth_config, key)

    async def put_object(
        self,
        auth_config: Dict[str, str],
        key: str,
        content: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """Upload content using the caller's auth_config (bucket + credentials)."""
        return await S3Integration.upload_to_s3(
            auth_config=auth_config,
            key=key,
            content=content,
            content_type=content_type,
            metadata=metadata,
        )

    async def list_objects(
        self,
        auth_config: Dict[str, str],
        prefix: str,
    ) -> list:
        """List object keys under a prefix. [] if the bucket is missing."""
        return await S3Integration.list_objects(auth_config, prefix)

    async def get_objects_bulk(
        self,
        auth_config: Dict[str, str],
        keys: list,
        concurrency: int = 32,
    ) -> Dict[str, Optional[str]]:
        """Read many objects concurrently over one shared client.
        {key: content or None}; a missing/failed object maps to None."""
        return await S3Integration.get_objects_bulk(auth_config, keys, concurrency)

    async def put_objects_bulk(
        self,
        auth_config: Dict[str, str],
        items: Dict[str, str],
        content_type: str = "application/octet-stream",
        concurrency: int = 32,
    ) -> Dict[str, bool]:
        """Write many objects concurrently over one shared client.
        {key: content} in → {key: True/False}; a per-object failure maps to False."""
        return await S3Integration.put_objects_bulk(
            auth_config, items, content_type, concurrency
        )

    async def delete_object(
        self,
        auth_config: Dict[str, str],
        key: str,
    ) -> None:
        """Delete a single object. No error if it doesn't exist."""
        await S3Integration.delete_object(auth_config, key)

    async def get_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
        auth_config: Optional[Dict[str, str]] = None
    ) -> Dict:
        """Generate presigned download URL for S3 object."""
        resolved_auth_config = auth_config or {
            "authentication_type": "access_key",
            "aws_access_key_id": settings.s3_upload_access_key_id,
            "aws_secret_access_key": settings.s3_upload_secret_access_key,
            "region": settings.s3_upload_region,
            "bucket": settings.s3_upload_bucket
        }

        result = await S3Integration.get_presigned_download_url(
            auth_config=resolved_auth_config,
            key=key,
            expiration=expiration
        )
        self.logger.info(f'Generated presigned URL for s3://{resolved_auth_config.get("bucket")}/{key}')
        return result
