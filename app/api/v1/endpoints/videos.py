"""
Video streaming API endpoint - Provides presigned URLs for documentation videos.
Only accessible to authorized tenants (aspora, vance).
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Tuple, List, Dict, Any
import logging
import aioboto3
from botocore.exceptions import ClientError
from botocore.config import Config

from app.api.dependencies import get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.core.config import settings
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class VideoItem(BaseModel):
    """Video metadata response"""
    key: str
    name: str
    size: int
    last_modified: str


class VideoListResponse(BaseModel):
    """Response for listing available videos"""
    videos: List[VideoItem]
    tenant: str


class VideoUrlResponse(BaseModel):
    """Response containing presigned URL for video"""
    url: str
    video_key: str
    expires_in: int
    tenant: str


def _get_s3_config() -> Config:
    """Get boto3 config for S3 operations with regional endpoint."""
    return Config(
        signature_version='s3v4',
        s3={'addressing_style': 'virtual'}
    )


def _get_client_kwargs() -> Dict[str, Any]:
    """Get kwargs for creating S3 client."""
    return {
        "region_name": settings.aws_region,
        "config": _get_s3_config(),
        "endpoint_url": f"https://s3.{settings.aws_region}.amazonaws.com"
    }


def _get_session_kwargs() -> Dict[str, Any]:
    """Get kwargs for aioboto3 session."""
    return {
        "aws_access_key_id": settings.video_aws_access_key_id,
        "aws_secret_access_key": settings.video_aws_secret_access_key,
        "region_name": settings.aws_region
    }


def check_tenant_access(tenant: TenantsMstModel) -> None:
    """Check if tenant is allowed to access videos."""
    allowed_tenants = settings.allowed_video_tenants_list

    if tenant.code.lower() not in [t.lower() for t in allowed_tenants]:
        logger.warning(f"Tenant '{tenant.code}' attempted to access videos but is not authorized")
        raise HTTPException(
            status_code=403,
            detail=f"Your organization ({tenant.name}) does not have access to documentation videos. "
                   f"Please contact support for access."
        )


@router.get("/list", response_model=VideoListResponse, summary="List Available Videos")
async def list_videos(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    List all available documentation videos.

    Only accessible to authorized tenants (aspora, vance).

    **Response:**
    - videos: List of available videos with metadata
    - tenant: Current tenant code

    **Errors:**
    - 403: Tenant not authorized to access videos
    - 500: Failed to list videos from S3
    """
    user, tenant = user_and_tenant

    # Check tenant authorization
    check_tenant_access(tenant)

    try:
        session = aioboto3.Session(**_get_session_kwargs())
        async with session.client("s3", **_get_client_kwargs()) as s3_client:
            response = await s3_client.list_objects_v2(
                Bucket=settings.s3_video_bucket,
                MaxKeys=100
            )

            videos = []
            for obj in response.get("Contents", []):
                # Only include video files
                key = obj["Key"]
                if key.lower().endswith((".mp4", ".webm", ".mov", ".avi")):
                    videos.append(VideoItem(
                        key=key,
                        name=key.replace("_", " ").replace(".mp4", "").replace(".webm", ""),
                        size=obj["Size"],
                        last_modified=obj["LastModified"].isoformat()
                    ))

            logger.info(f"User {user.email_id} from tenant {tenant.code} listed {len(videos)} videos")

            return VideoListResponse(
                videos=videos,
                tenant=tenant.code
            )

    except ClientError as e:
        logger.error(f"S3 error listing videos: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list videos")
    except Exception as e:
        logger.error(f"Error listing videos: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list videos: {str(e)}")


@router.get("/url/{video_key:path}", response_model=VideoUrlResponse, summary="Get Video URL")
async def get_video_url(
    video_key: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get a presigned URL to stream a specific video.

    Only accessible to authorized tenants (aspora, vance).
    The URL is valid for 1 hour (3600 seconds).

    **Path Parameters:**
    - video_key: The S3 key of the video (e.g., "Service_Onboarding ECS.mp4")

    **Response:**
    - url: Presigned URL for streaming the video
    - video_key: The requested video key
    - expires_in: URL expiration time in seconds
    - tenant: Current tenant code

    **Errors:**
    - 403: Tenant not authorized to access videos
    - 404: Video not found
    - 500: Failed to generate URL

    **Example:**
    ```
    GET /api/v1/videos/url/Service_Onboarding%20ECS.mp4
    ```
    """
    user, tenant = user_and_tenant

    # Check tenant authorization
    check_tenant_access(tenant)

    expiration = 3600  # 1 hour

    try:
        session = aioboto3.Session(**_get_session_kwargs())
        async with session.client("s3", **_get_client_kwargs()) as s3_client:
            # First verify the object exists
            try:
                await s3_client.head_object(
                    Bucket=settings.s3_video_bucket,
                    Key=video_key
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "404":
                    raise HTTPException(status_code=404, detail=f"Video not found: {video_key}")
                raise

            # Generate presigned URL
            url = await s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.s3_video_bucket,
                    "Key": video_key
                },
                ExpiresIn=expiration
            )

            logger.info(f"Generated presigned URL for video '{video_key}' for user {user.email_id} (tenant: {tenant.code})")

            return VideoUrlResponse(
                url=url,
                video_key=video_key,
                expires_in=expiration,
                tenant=tenant.code
            )

    except HTTPException:
        raise
    except ClientError as e:
        logger.error(f"S3 error generating presigned URL: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate video URL")
    except Exception as e:
        logger.error(f"Error generating video URL: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate video URL: {str(e)}")
