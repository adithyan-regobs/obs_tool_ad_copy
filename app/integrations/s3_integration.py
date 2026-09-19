"""
S3 Integration for file storage using presigned URLs.
"""
import aioboto3
import asyncio
import logging
from typing import Dict, Any, List, Optional, Union
from botocore.exceptions import ClientError
from botocore.config import Config

logger = logging.getLogger(__name__)


class S3Integration:
    """S3 operations: upload content and generate presigned URLs."""

    @staticmethod
    def _get_config() -> Config:
        """Get boto3 config for S3 operations."""
        return Config(
            signature_version='s3v4',
            s3={'addressing_style': 'virtual'}
        )

    @staticmethod
    async def get_presigned_upload_url(
        auth_config: Dict[str, str],
        key: str,
        expiration: int = 3600,
        content_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate presigned URL for client-side file upload to S3."""
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            # Build client kwargs based on auth type
            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                access_key = auth_config.get("aws_access_key_id")
                secret_key = auth_config.get("aws_secret_access_key")

                if not access_key or not secret_key:
                    raise ValueError("aws_access_key_id and aws_secret_access_key are required for access_key authentication")

                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key

            async with session.client("s3", **client_kwargs) as s3_client:
                params = {
                    "Bucket": bucket_name,
                    "Key": key
                }

                headers = {}
                if content_type:
                    params["ContentType"] = content_type
                    headers["Content-Type"] = content_type

                # generate_presigned_url is NOT async - it's a local computation
                upload_url = await s3_client.generate_presigned_url(
                    "put_object",
                    Params=params,
                    ExpiresIn=expiration
                )

                logger.info(
                    f"Generated presigned upload URL for bucket '{bucket_name}', "
                    f"key '{key}', expires in {expiration}s"
                )

                return {
                    "upload_url": upload_url,
                    "key": key,
                    "bucket_name": bucket_name,
                    "expires_in": expiration,
                    "method": "PUT",
                    "headers": headers
                }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = str(e)

            logger.error(f"S3 error generating presigned URL: {error_message}")

            if error_code == "NoSuchBucket":
                raise Exception(
                    f"Bucket '{bucket_name}' does not exist. "
                    f"Please create the bucket manually in AWS S3."
                )
            else:
                raise Exception(f"Failed to generate presigned URL: {error_message}")
        except Exception as e:
            logger.error(f"Unexpected error generating presigned URL: {str(e)}")
            raise Exception(f"Failed to generate presigned URL: {str(e)}")

    @staticmethod
    async def get_presigned_download_url(
        auth_config: Dict[str, str],
        key: str,
        expiration: int = 3600
    ) -> Dict[str, Any]:
        """Generate presigned URL for client-side file download from S3."""
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            # Build client kwargs based on auth type
            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                access_key = auth_config.get("aws_access_key_id")
                secret_key = auth_config.get("aws_secret_access_key")

                if not access_key or not secret_key:
                    raise ValueError("aws_access_key_id and aws_secret_access_key are required for access_key authentication")

                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key

            async with session.client("s3", **client_kwargs) as s3_client:
                # First verify the object exists
                try:
                    await s3_client.head_object(
                        Bucket=bucket_name,
                        Key=key
                    )
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")
                    if error_code == "404" or error_code == "NoSuchKey":
                        logger.error(f"Object not found: s3://{bucket_name}/{key}")
                        raise Exception(
                            f"File not found in S3: '{key}'. "
                            f"Please ensure the file has been uploaded."
                        )
                    raise

                # Generate presigned URL for GET operation
                download_url = await s3_client.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": bucket_name,
                        "Key": key
                    },
                    ExpiresIn=expiration
                )

                logger.info(
                    f"Generated presigned download URL for bucket '{bucket_name}', "
                    f"key '{key}', expires in {expiration}s"
                )

                return {
                    "url": download_url,
                    "key": key,
                    "bucket_name": bucket_name,
                    "expires_in": expiration,
                    "method": "GET"
                }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = str(e)

            logger.error(f"S3 error generating presigned download URL: {error_message}")

            if error_code == "NoSuchBucket":
                raise Exception(
                    f"Bucket '{bucket_name}' does not exist. "
                    f"Please create the bucket manually in AWS S3."
                )
            elif error_code == "404" or error_code == "NoSuchKey":
                raise Exception(
                    f"File not found in S3: '{key}'. "
                    f"Please ensure the file has been uploaded."
                )
            else:
                raise Exception(f"Failed to generate presigned download URL: {error_message}")
        except Exception as e:
            logger.error(f"Unexpected error generating presigned download URL: {str(e)}")
            raise Exception(f"Failed to generate presigned download URL: {str(e)}")

    @staticmethod
    async def upload_to_s3(
        auth_config: Dict[str, str],
        key: str,
        content: Union[str, bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Upload content directly to S3 from server."""
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            # Build client kwargs based on auth type
            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                access_key = auth_config.get("aws_access_key_id")
                secret_key = auth_config.get("aws_secret_access_key")

                if not access_key or not secret_key:
                    raise ValueError("aws_access_key_id and aws_secret_access_key are required for access_key authentication")

                client_kwargs["aws_access_key_id"] = access_key
                client_kwargs["aws_secret_access_key"] = secret_key

            async with session.client("s3", **client_kwargs) as s3_client:
                if isinstance(content, str):
                    content = content.encode('utf-8')

                params = {
                    "Bucket": bucket_name,
                    "Key": key,
                    "Body": content,
                    "ContentType": content_type
                }

                if metadata:
                    params["Metadata"] = metadata

                response = await s3_client.put_object(**params)

                logger.info(
                    f"Uploaded content to S3: bucket='{bucket_name}', key='{key}', "
                    f"size={len(content)} bytes, etag={response.get('ETag')}"
                )

                return {
                    "bucket_name": bucket_name,
                    "key": key,
                    "etag": response["ETag"].strip('"'),
                    "version_id": response.get("VersionId"),
                    "location": f"s3://{bucket_name}/{key}"
                }

        except ClientError as e:
            # Artifact upload is best-effort: script generation and the PR flow are the
            # source of truth, so a failed upload is logged and skipped rather than
            # failing the whole deployment.
            error_code = e.response.get("Error", {}).get("Code")
            logger.error(
                f"S3 error uploading content to '{bucket_name}/{key}' "
                f"(code={error_code}) — skipping upload: {e}",
                exc_info=True,
            )
            return S3Integration._skipped_upload_result(bucket_name, key)
        except Exception as e:
            logger.error(
                f"Unexpected error uploading content to '{bucket_name}/{key}' "
                f"— skipping upload: {e}",
                exc_info=True,
            )
            return S3Integration._skipped_upload_result(bucket_name, key)

    @staticmethod
    def _skipped_upload_result(bucket_name: Optional[str], key: str) -> Dict[str, Any]:
        """Result shape returned when an upload is skipped after a failure.

        Mirrors a successful upload minus etag/version_id so callers that only read
        'location' keep working.
        """
        return {
            "bucket_name": bucket_name,
            "key": key,
            "etag": None,
            "version_id": None,
            "location": f"s3://{bucket_name}/{key}",
            "skipped": True,
        }

    @staticmethod
    async def list_objects(
        auth_config: Dict[str, str],
        prefix: str,
    ) -> list:
        """List object keys under a prefix. Returns [] if the bucket is missing.

        Used by the secret audit trail to find existing version-N.json entries
        for a key so the next version number can be computed.
        """
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                client_kwargs["aws_access_key_id"] = auth_config.get("aws_access_key_id")
                client_kwargs["aws_secret_access_key"] = auth_config.get("aws_secret_access_key")

            keys = []
            async with session.client("s3", **client_kwargs) as s3_client:
                paginator = s3_client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        keys.append(obj["Key"])
            return keys
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchBucket":
                logger.warning(f"Bucket '{bucket_name}' does not exist — returning empty object list")
                return []
            logger.error(f"S3 error listing objects under '{prefix}': {str(e)}")
            raise Exception(f"Failed to list S3 objects: {str(e)}")

    @staticmethod
    async def get_object(
        auth_config: Dict[str, str],
        key: str,
    ) -> Optional[str]:
        """Read an object's content as a UTF-8 string. Returns None if it doesn't exist."""
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                client_kwargs["aws_access_key_id"] = auth_config.get("aws_access_key_id")
                client_kwargs["aws_secret_access_key"] = auth_config.get("aws_secret_access_key")

            async with session.client("s3", **client_kwargs) as s3_client:
                response = await s3_client.get_object(Bucket=bucket_name, Key=key)
                body = await response["Body"].read()
                return body.decode("utf-8")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("NoSuchKey", "NoSuchBucket"):
                return None
            logger.error(f"S3 error reading object '{key}': {str(e)}")
            raise Exception(f"Failed to read S3 object: {str(e)}")

    @staticmethod
    async def get_objects_bulk(
        auth_config: Dict[str, str],
        keys: List[str],
        concurrency: int = 32,
    ) -> Dict[str, Optional[str]]:
        """Read many objects concurrently over ONE shared S3 client.

        Returns ``{key: content_str or None}``. A missing object (NoSuchKey) or a
        per-object error maps to None so one bad key never fails the batch. A
        single client + a connection pool sized to ``concurrency`` serves every
        read, so N keys cost ~``ceil(N / concurrency)`` round-trips instead of N
        sequential ones. (The default pool is 10, which would otherwise cap real
        parallelism well below ``concurrency``.)
        """
        result: Dict[str, Optional[str]] = {k: None for k in keys}
        if not keys:
            return result

        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        # Pool must be >= concurrency, else the shared client serialises beyond 10.
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            max_pool_connections=max(concurrency, 10),
        )

        client_kwargs: Dict[str, Any] = {"region_name": region, "config": config}
        if auth_type == "access_key":
            client_kwargs["aws_access_key_id"] = auth_config.get("aws_access_key_id")
            client_kwargs["aws_secret_access_key"] = auth_config.get("aws_secret_access_key")

        sem = asyncio.Semaphore(max(1, concurrency))
        session = aioboto3.Session()
        try:
            async with session.client("s3", **client_kwargs) as s3_client:
                async def _one(k: str):
                    async with sem:
                        try:
                            resp = await s3_client.get_object(Bucket=bucket_name, Key=k)
                            body = await resp["Body"].read()
                            return k, body.decode("utf-8")
                        except ClientError as e:
                            code = e.response.get("Error", {}).get("Code")
                            if code in ("NoSuchKey", "NoSuchBucket"):
                                return k, None
                            logger.error(f"S3 bulk read error for '{k}': {e}")
                            return k, None
                        except Exception as e:
                            logger.error(f"S3 bulk read error for '{k}': {e}")
                            return k, None

                for k, body in await asyncio.gather(*[_one(k) for k in keys]):
                    result[k] = body
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchBucket":
                logger.warning(f"Bucket '{bucket_name}' does not exist — returning empty bulk result")
                return result
            logger.error(f"S3 error during bulk read: {str(e)}")
            raise Exception(f"Failed bulk S3 read: {str(e)}")
        return result

    @staticmethod
    async def put_objects_bulk(
        auth_config: Dict[str, str],
        items: Dict[str, str],
        content_type: str = "application/octet-stream",
        concurrency: int = 32,
    ) -> Dict[str, bool]:
        """Write many objects concurrently over ONE shared S3 client.

        Takes ``{key: content_str}`` and returns ``{key: True/False}`` (False on a
        per-object failure, so one bad key never fails the batch). A single client
        + a connection pool sized to ``concurrency`` serves every write, so N
        objects cost ~``ceil(N / concurrency)`` round-trips and ONE TLS handshake /
        credential resolution instead of N separate client-per-call uploads (the
        slow path — the ``Found credentials`` log fires once, not per object).
        """
        result: Dict[str, bool] = {k: False for k in items}
        if not items:
            return result

        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        # Pool must be >= concurrency, else the shared client serialises beyond 10.
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            max_pool_connections=max(concurrency, 10),
        )

        client_kwargs: Dict[str, Any] = {"region_name": region, "config": config}
        if auth_type == "access_key":
            client_kwargs["aws_access_key_id"] = auth_config.get("aws_access_key_id")
            client_kwargs["aws_secret_access_key"] = auth_config.get("aws_secret_access_key")

        sem = asyncio.Semaphore(max(1, concurrency))
        session = aioboto3.Session()
        try:
            async with session.client("s3", **client_kwargs) as s3_client:
                async def _one(k: str, content: str):
                    async with sem:
                        try:
                            body = content.encode("utf-8") if isinstance(content, str) else content
                            await s3_client.put_object(
                                Bucket=bucket_name, Key=k, Body=body, ContentType=content_type
                            )
                            return k, True
                        except Exception as e:
                            logger.error(f"S3 bulk write error for '{k}': {e}")
                            return k, False

                for k, ok in await asyncio.gather(
                    *[_one(k, c) for k, c in items.items()]
                ):
                    result[k] = ok
        except ClientError as e:
            logger.error(f"S3 error during bulk write: {str(e)}")
            raise Exception(f"Failed bulk S3 write: {str(e)}")
        return result

    @staticmethod
    async def delete_object(
        auth_config: Dict[str, str],
        key: str,
    ) -> None:
        """Delete a single object. No error if it doesn't exist."""
        bucket_name = auth_config.get("bucket")
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "ap-south-1")
        config = S3Integration._get_config()

        try:
            session = aioboto3.Session()

            client_kwargs = {
                "region_name": region,
                "config": config
            }

            if auth_type == "access_key":
                client_kwargs["aws_access_key_id"] = auth_config.get("aws_access_key_id")
                client_kwargs["aws_secret_access_key"] = auth_config.get("aws_secret_access_key")

            async with session.client("s3", **client_kwargs) as s3_client:
                await s3_client.delete_object(Bucket=bucket_name, Key=key)
                logger.info(f"Deleted S3 object: bucket='{bucket_name}', key='{key}'")
        except ClientError as e:
            logger.error(f"S3 error deleting object '{key}': {str(e)}")
            raise Exception(f"Failed to delete S3 object: {str(e)}")
