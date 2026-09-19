"""
File Manager API Endpoints

Provides endpoints for viewing files stored in S3 via presigned URLs.
"""
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.file_manager_service import FileManagerService
from app.schemas.file_manager_schemas import (
    PresignedUrlResponse,
    FileTypeEnum
)

router = APIRouter()


@router.get(
    "/view/{queue_item_id}",
    response_model=PresignedUrlResponse,
    summary="Get Presigned URL for HCL File"
)
async def get_hcl_presigned_url(
    queue_item_id: int,
    file_type: Optional[FileTypeEnum] = Query(
        FileTypeEnum.PREVIEW,
        description="Type of file to view: 'preview' (default) or 'original'"
    ),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Generate a presigned URL for viewing HCL file stored in S3.

    This endpoint generates a time-limited presigned URL that allows
    direct access to HCL files stored in S3. The frontend can use this
    URL to display the file content to the user.

    **Security:**
        - JWT authentication required
        - Tenant isolation enforced
        - Presigned URL expires after 1 hour

    **Path Parameters:**
        - `queue_item_id`: ID of the queue item (integer)

    **Query Parameters:**
        - `file_type`: Type of file to view (optional)
            - `preview` (default): Returns the preview HCL file
            - `original`: Returns the original HCL file

    **Response:**
        ```json
        {
            "url": "https://my-bucket.s3.amazonaws.com/preview/sqs/payment-queue.hcl?X-Amz-Signature=...",
            "key": "preview/sqs/payment-queue.hcl",
            "bucket_name": "aspora-hcl-artifacts",
            "expires_in": 3600,
            "method": "GET",
            "file_type": "preview"
        }
        ```

    **Example Usage:**
        ```bash
        # Get preview file (default)
        GET /api/v1/file-manager/view/123

        # Get original file
        GET /api/v1/file-manager/view/123?file_type=original
        ```

    **Error Responses:**
        - `404`: Queue item not found OR file not found in S3
        - `400`: No HCL file uploaded OR preview file not available
        - `500`: Internal server error

    **Use Cases:**
        - Preview HCL content before deployment
        - View generated scripts from deploy queue
        - Download HCL files for offline review
        - Display HCL content in UI editor
    """
    user, tenant = current_user_tenant

    try:
        service = FileManagerService(db)

        result = await service.get_hcl_presigned_url(
            queue_item_id=queue_item_id,
            file_type=file_type
        )

        return PresignedUrlResponse(**result)

    except ValueError as e:
        # Business logic errors (not found, validation)
        raise HTTPException(
            status_code=404 if "not found" in str(e).lower() else 400,
            detail=str(e)
        )
    except Exception as e:
        # Unexpected errors (S3 access, etc.)
        logger = __import__('logging').getLogger(__name__)
        logger.error(
            f"Failed to generate presigned URL for queue item {queue_item_id}: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate presigned URL: {str(e)}"
        )
