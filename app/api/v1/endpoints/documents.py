"""
Documentation files API - lists and serves written docs from S3.

Read-only by design. Files are published by dropping them in the bucket, the
same way documentation videos are; there is no upload path through the API.

The key travels as a query parameter, not a path segment. Document keys contain
folders — that is where categories come from — and an encoded %2F in a path is
the kind of thing proxies normalise. The videos endpoint puts its key in the
path, but video keys are flat, so it never exercises that.

Two ways to fetch one, because the browser needs both:

- /content returns the bytes through this API. The docx viewer reads the file
  with fetch() + arrayBuffer(), which is subject to CORS, so a presigned S3 URL
  would need a CORS rule on the bucket. Serving the bytes here needs no bucket
  configuration at all and keeps the file behind the same auth as the listing.
- /url returns a presigned URL, for the things that are not fetch(): the PDF
  <iframe> and the download link. Neither is a cross-origin read, so neither
  needs CORS.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.dependencies import get_current_user_and_tenant
from app.core.config import settings
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.utils.document_title import title_from_filename

router = APIRouter()
logger = logging.getLogger(__name__)

#: Formats the viewer can render. Legacy .doc is not one of them — mammoth
#: reads OOXML only, and a .doc renamed to .docx fails as "not a zip file".
CONTENT_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
}

MAX_KEYS = 500
URL_EXPIRY_SECONDS = 3600

#: S3 failures that are really configuration problems. The raw codes say
#: nothing useful to whoever is looking at the Documents tab, and the generic
#: "Failed to list documents" sends them hunting in the wrong place.
_CONFIG_ERRORS = {
    "InvalidAccessKeyId": (
        "The configured AWS access key is not recognised. Check "
        "DOCS_AWS_ACCESS_KEY_ID — the key may have been deleted or rotated."
    ),
    "SignatureDoesNotMatch": (
        "The configured AWS secret key does not match its access key. Check "
        "DOCS_AWS_SECRET_ACCESS_KEY."
    ),
    "AccessDenied": (
        "The configured AWS credentials cannot read the documentation bucket. "
        "They need s3:ListBucket on the bucket and s3:GetObject on its contents."
    ),
    "NoSuchBucket": (
        "The documentation bucket does not exist in this account or region. "
        "Check S3_DOCS_BUCKET and DOCS_AWS_REGION (or AWS_REGION, which it "
        "falls back to)."
    ),
    "PermanentRedirect": (
        "The documentation bucket is in a different region. Set "
        "DOCS_AWS_REGION to the bucket's own region."
    ),
}


def _explain(exc: ClientError) -> Optional[str]:
    """A message worth showing, or None to fall through to the generic one."""
    return _CONFIG_ERRORS.get(exc.response.get("Error", {}).get("Code", ""))


class DocumentItem(BaseModel):
    """One document in the library."""

    key: str
    #: The document's own title, read from the file. Falls back to the filename.
    title: str
    filename: str
    #: "docx", "pdf" or "md" — drives the icon and which viewer is used.
    format: str
    #: Top-level folder in the bucket, or "Uncategorised" for a loose file.
    category: str
    size: int
    last_modified: str


class DocumentListResponse(BaseModel):
    documents: List[DocumentItem]
    tenant: str


class DocumentUrlResponse(BaseModel):
    url: str
    document_key: str
    expires_in: int
    tenant: str


def _get_client_kwargs() -> Dict[str, Any]:
    # settings.docs_region, not settings.aws_region: DOCS_AWS_REGION when it is
    # set, AWS_REGION otherwise. AWS_REGION is shared by most of this service,
    # so it cannot be moved to follow this bucket alone.
    region = settings.docs_region
    return {
        "region_name": region,
        "config": Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        "endpoint_url": f"https://s3.{region}.amazonaws.com",
    }


def _get_session_kwargs() -> Dict[str, Any]:
    """
    Session credentials, static keys only when they are actually configured.

    Omitting the keys entirely lets boto3 use its normal chain: environment,
    shared config, or — the case that matters in ECS — the task role. Passing
    empty strings instead would send an empty access key and fail with
    InvalidAccessKeyId, which is why this is a conditional rather than a
    straight copy of the videos endpoint.

    This is the normal path, not a fallback: no DOCS_AWS_* keys are expected to
    be set.
    """
    kwargs: Dict[str, Any] = {"region_name": settings.docs_region}

    access_key, secret_key = settings.docs_aws_credentials
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    return kwargs


def check_tenant_access(tenant: TenantsMstModel) -> None:
    """Reject tenants that are not on the documentation allowlist."""
    allowed = [t.lower() for t in settings.allowed_docs_tenants_list]

    if tenant.code.lower() not in allowed:
        logger.warning(
            f"Tenant '{tenant.code}' attempted to access documents but is not authorized"
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your organization ({tenant.name}) does not have access to documentation. "
                f"Please contact support for access."
            ),
        )


def _extension(key: str) -> Optional[str]:
    lowered = key.lower()
    for extension in CONTENT_TYPES:
        if lowered.endswith(extension):
            return extension
    return None


def _category(key: str) -> str:
    """
    Top-level folder as the category, so the library groups itself by how the
    bucket is laid out and needs no database.
    """
    if "/" not in key:
        return "Uncategorised"
    folder = key.split("/", 1)[0]
    return " ".join(word.capitalize() for word in folder.replace("_", " ").replace("-", " ").split())


@router.get("/list", response_model=DocumentListResponse, summary="List Documents")
async def list_documents(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    List the documentation files available to this tenant.

    Titles come from the filename and categories from the top-level folder,
    so a listing is one S3 call: no object is read. Reading the real title out
    of each document meant downloading the whole library on every listing.

    **Errors:**
    - 403: Tenant not authorized to access documentation
    - 500: Failed to list documents from S3
    """
    user, tenant = user_and_tenant
    check_tenant_access(tenant)

    if not settings.s3_docs_bucket:
        raise HTTPException(status_code=500, detail="Documentation storage is not configured")

    try:
        session = aioboto3.Session(**_get_session_kwargs())
        async with session.client("s3", **_get_client_kwargs()) as s3_client:
            response = await s3_client.list_objects_v2(
                Bucket=settings.s3_docs_bucket, MaxKeys=MAX_KEYS
            )

            documents = [
                DocumentItem(
                    key=obj["Key"],
                    title=title_from_filename(obj["Key"]),
                    filename=obj["Key"].rsplit("/", 1)[-1],
                    format=_extension(obj["Key"]).lstrip("."),
                    category=_category(obj["Key"]),
                    size=obj.get("Size", 0),
                    last_modified=obj["LastModified"].isoformat(),
                )
                for obj in response.get("Contents", [])
                if _extension(obj["Key"]) and not obj["Key"].endswith("/")
            ]
            documents.sort(key=lambda d: (d.category, d.title.lower()))

            logger.info(
                f"User {user.email_id} from tenant {tenant.code} listed {len(documents)} documents"
            )
            return DocumentListResponse(documents=documents, tenant=tenant.code)

    except HTTPException:
        raise
    except ClientError as exc:
        logger.error(f"S3 error listing documents: {str(exc)}")
        raise HTTPException(status_code=500, detail=_explain(exc) or "Failed to list documents")
    except Exception as exc:
        logger.error(f"Error listing documents: {str(exc)}")
        raise HTTPException(status_code=500, detail=f"Failed to list documents: {str(exc)}")


@router.get("/content", summary="Get Document Content")
async def get_document_content(
    key: str = Query(..., description="S3 key of the document, e.g. getting-started/onboarding.docx"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Return a document's raw bytes.

    This is what the in-browser .docx viewer reads. It goes through the API
    rather than a presigned URL because the viewer uses fetch(), which a
    presigned S3 URL would only satisfy with a CORS rule on the bucket.

    **Errors:**
    - 403: Tenant not authorized
    - 404: Document not found
    - 415: Not a supported document format
    """
    user, tenant = user_and_tenant
    check_tenant_access(tenant)

    extension = _extension(key)
    if not extension:
        raise HTTPException(
            status_code=415,
            detail=f"Not a supported document format: {key}",
        )

    try:
        session = aioboto3.Session(**_get_session_kwargs())
        async with session.client("s3", **_get_client_kwargs()) as s3_client:
            try:
                response = await s3_client.get_object(
                    Bucket=settings.s3_docs_bucket, Key=key
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise HTTPException(
                        status_code=404, detail=f"Document not found: {key}"
                    )
                raise

            data = await response["Body"].read()

            logger.info(
                f"Served document '{key}' to user {user.email_id} (tenant: {tenant.code})"
            )
            return Response(
                content=data,
                media_type=CONTENT_TYPES[extension],
                headers={
                    # inline: the browser renders it rather than saving it. The
                    # download button uses the presigned URL instead.
                    "Content-Disposition": f'inline; filename="{key.rsplit("/", 1)[-1]}"',
                    "Cache-Control": "private, max-age=300",
                },
            )

    except HTTPException:
        raise
    except ClientError as exc:
        logger.error(f"S3 error reading document: {str(exc)}")
        raise HTTPException(status_code=500, detail=_explain(exc) or "Failed to read document")
    except Exception as exc:
        logger.error(f"Error reading document: {str(exc)}")
        raise HTTPException(status_code=500, detail=f"Failed to read document: {str(exc)}")


@router.get("/url", response_model=DocumentUrlResponse, summary="Get Document URL")
async def get_document_url(
    key: str = Query(..., description="S3 key of the document"),
    disposition: str = Query(
        "inline",
        pattern="^(inline|attachment)$",
        description="inline renders in the browser; attachment forces a download",
    ),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Presigned URL for a document, valid for one hour.

    Used for the things a fetch() cannot serve: rendering a PDF in an <iframe>
    and the download link. Neither is a cross-origin read, so neither needs a
    CORS rule on the bucket.

    `disposition=attachment` signs the URL with a Content-Disposition override,
    which is the only way to force a download here: the HTML `download`
    attribute is ignored on cross-origin links, so a presigned S3 URL would
    otherwise just navigate and let the browser decide.

    **Errors:**
    - 403: Tenant not authorized
    - 404: Document not found
    """
    user, tenant = user_and_tenant
    check_tenant_access(tenant)

    try:
        session = aioboto3.Session(**_get_session_kwargs())
        async with session.client("s3", **_get_client_kwargs()) as s3_client:
            try:
                await s3_client.head_object(Bucket=settings.s3_docs_bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise HTTPException(
                        status_code=404, detail=f"Document not found: {key}"
                    )
                raise

            params = {"Bucket": settings.s3_docs_bucket, "Key": key}
            if disposition == "attachment":
                filename = key.rsplit("/", 1)[-1]
                params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'

            url = await s3_client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=URL_EXPIRY_SECONDS,
            )

            logger.info(
                f"Generated presigned URL for '{key}' for user {user.email_id} "
                f"(tenant: {tenant.code})"
            )
            return DocumentUrlResponse(
                url=url,
                document_key=key,
                expires_in=URL_EXPIRY_SECONDS,
                tenant=tenant.code,
            )

    except HTTPException:
        raise
    except ClientError as exc:
        logger.error(f"S3 error generating presigned URL: {str(exc)}")
        raise HTTPException(
            status_code=500, detail=_explain(exc) or "Failed to generate document URL"
        )
    except Exception as exc:
        logger.error(f"Error generating document URL: {str(exc)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate document URL: {str(exc)}")
