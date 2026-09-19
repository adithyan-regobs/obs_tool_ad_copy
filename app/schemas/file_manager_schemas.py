"""
File Manager Schemas

Schemas for file viewing operations (presigned URLs for S3 files).
"""
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum


class FileTypeEnum(str, Enum):
    """File type to view"""
    PREVIEW = "preview"
    ORIGINAL = "original"


class PresignedUrlResponse(BaseModel):
    """Response schema for presigned URL generation"""
    url: str = Field(..., description="Presigned URL for S3 object")
    key: str = Field(..., description="S3 object key")
    bucket_name: str = Field(..., description="S3 bucket name")
    expires_in: int = Field(..., description="URL expiration time in seconds")
    method: str = Field(..., description="HTTP method for the URL")
    file_type: FileTypeEnum = Field(..., description="Type of file (preview or original)")

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://my-bucket.s3.amazonaws.com/preview/sqs/payment-queue.hcl?...",
                "key": "preview/sqs/payment-queue.hcl",
                "bucket_name": "aspora-hcl-artifacts",
                "expires_in": 3600,
                "method": "GET",
                "file_type": "preview"
            }
        }
