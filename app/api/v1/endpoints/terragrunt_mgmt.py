"""
Terragrunt Management API Endpoints

Handles terragrunt configuration file management operations.
"""

from typing import Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.services.terragrunt_mgmt_service import TerragruntMgmtService
from app.domain.validators.github_rules import GitHubValidationError
from app.schemas.github_schemas import (
    CommitTerragruntRequest,
    CommitTerragruntResponse,
    GetTerragruntContentRequest,
    GetTerragruntContentResponse
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/push-infra-config",
    summary="Push Infrastructure Configuration",
    response_model=CommitTerragruntResponse,
    response_model_exclude_none=True,
    status_code=201
)
async def push_infra_config(
    data: CommitTerragruntRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> CommitTerragruntResponse:
    """
    Push a terragrunt.hcl infrastructure configuration file to a GitHub repository.

    Security:
        - JWT authentication required
        - Tenant isolation enforced: files committed under authenticated user's tenant
        - Tenant subdomain automatically injected from JWT token

    This endpoint performs the following operations:
    1. Extracts tenant subdomain from JWT authentication
    2. Validates GitHub repository format and parameters
    3. Validates GitHub token format
    4. Validates environment name and terragrunt content
    5. Determines region based on environment from settings
    6. Uses version index from settings
    7. Constructs file path: tenant/{tenant}/{environment}/{version_index}/{region}/{resource_type}/terragrunt.hcl
    8. Commits file to GitHub using the GitHub API

    **Requirements:**
    - JWT authentication (valid Clerk token with organizationSubdomain claim)
    - GitHub token must be configured in environment variables (GITHUB_TOKEN)
    - Token must have repo write permissions
    - Repository must exist and be accessible
    - Branch must exist in the repository

    **File Path Structure:**
    The terragrunt file will be committed to: `tenant/{tenant}/{environment}/{version_index}/{region}/{resource_type}/terragrunt.hcl`

    **Configuration (Environment Variables):**
    - INFRA_VERSION_INDEX: Version index for all environments (default: "01")
    - INFRA_REGION_DEV: Region for dev environment (default: "ap-south-1")
    - INFRA_REGION_STAGING: Region for staging environment (default: "ap-south-1")
    - INFRA_REGION_PROD: Region for prod environment (default: "us-west-2")
    - INFRA_RESOURCE_TYPE: Default resource type folder (default: "s3-bucket")

    **Versioning:**
    If a file already exists at the target path, a versioned file will be created:
    - First conflict: `tenant/{tenant}/{environment}/{version_index}/{region}/{resource_type}/terragrunt-v2.hcl`
    - Second conflict: `tenant/{tenant}/{environment}/{version_index}/{region}/{resource_type}/terragrunt-v3.hcl`
    - And so on...

    **Request Body:**
    - terragrunt_content: HCL configuration content (required)
    - environment: Environment name (required)
    - github_repository: Repository in 'owner/repo' format (required)
    - branch_name: Target branch name (required)
    - commit_message: Custom commit message (optional)
    - resource_type: Resource type folder name like "s3-bucket", "sqs" (optional, defaults to INFRA_RESOURCE_TYPE)

    **Returns:**
    - success: Boolean indicating success
    - commit_sha: Git commit SHA
    - file_path: Final path where file was committed
    - commit_url: URL to view the commit on GitHub
    - html_url: URL to view the file on GitHub
    - message: Success message

    **Errors:**
    - 400: Validation error (invalid repository format, empty parameters, etc.)
    - 401: GitHub authentication failed (invalid or expired token)
    - 403: GitHub API rate limit exceeded or insufficient permissions
    - 404: Repository or branch not found
    - 500: Internal server error or GitHub API error

    **Example:**
    ```json
    {
      "terragrunt_content": "terraform {\\n  source = \\"....\\"\\n}",
      "environment": "dev",
      "github_repository": "myorg/infrastructure",
      "branch_name": "main",
      "commit_message": "Add dev environment terragrunt config"
    }
    ```

    **Response:**
    ```json
    {
      "success": true,
      "commit_sha": "abc123def456...",
      "file_path": "tenant/acme-corp/dev/01/ap-south-1/s3-bucket/terragrunt.hcl",
      "commit_url": "https://github.com/myorg/infrastructure/commit/abc123",
      "html_url": "https://github.com/myorg/infrastructure/blob/main/tenant/acme-corp/dev/01/ap-south-1/s3-bucket/terragrunt.hcl",
      "message": "Terragrunt file successfully committed to tenant/acme-corp/dev/01/ap-south-1/s3-bucket/terragrunt.hcl"
    }
    ```
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log tenant isolation
        logger.info("="*80)
        logger.info("🔍 PUSH INFRA CONFIG - TENANT ISOLATION CHECK")
        logger.info(f"   User Code: {user.code}")
        logger.info(f"   User Email: {user.email_id}")
        logger.info(f"   Tenant Code: {tenant.code}")
        logger.info(f"   Tenant Name: {tenant.name}")
        logger.info(f"   Tenant Subdomain: {tenant.subdomain}")
        logger.info(f"   Environment: {data.environment}")
        logger.info(f"   GitHub Repo: {data.github_repository}")
        logger.info("="*80)

        # Initialize service with db session (required for gateway routes)
        service = TerragruntMgmtService(session=db)
        # Call service with tenant code and user code from JWT
        result = await service.commit_terragrunt_file(
            terragrunt_content=data.terragrunt_content,
            tenant=tenant.code,  # Use tenant code from JWT
            user_code=user.code,  # Use user code from JWT (for chat message insertion)
            environment=data.environment,
            github_repository=data.github_repository,
            branch_name=data.branch_name,
            commit_message=data.commit_message,
            user_email=user.email_id,  # User email for commit message attribution
            resource_type=data.resource_type,
            parameters=data.parameters,
            product_name=data.product_name,
            product_code=data.product_code,
            resource_group_code=data.resource_group_code,
            region=data.region,
            infra_vendor=data.infra_vendor,
            service_code=data.service_code,
            geo_loc=data.geo_loc_code,
            case_type_ref_code=data.case_type_ref_code
        )

        return result
    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except GitHubValidationError as e:
        # Business logic validation errors
        logger.error(f"Validation error in push_infra_config: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Map specific GitHub errors to appropriate HTTP status codes
        error_message = str(e)

        if "authentication failed" in error_message.lower() or "invalid or expired token" in error_message.lower():
            raise HTTPException(status_code=401, detail=error_message)
        elif "rate limit" in error_message.lower() or "insufficient permissions" in error_message.lower():
            raise HTTPException(status_code=403, detail=error_message)
        elif "not found" in error_message.lower():
            raise HTTPException(status_code=404, detail=error_message)
        else:
            # Unexpected errors
            logger.error(f"Unexpected error in push_infra_config: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to push infrastructure configuration: {error_message}")


@router.post(
    "/get-terragrunt-content",
    summary="Get Terragrunt Content Preview",
    response_model=GetTerragruntContentResponse
)
async def get_terragrunt_content(
    data: GetTerragruntContentRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
) -> GetTerragruntContentResponse:
    """
    Render terragrunt HCL content preview based on resource type and parameters.

    **Resource Types & Required Parameters:**

    - `create_bucket` (S3):
      - identifier: Bucket name (required)
      - versioning: Enable versioning (optional, default: false)
      - enable_s3_replication: Enable cross-account replication (optional, default: false)
      - cross_account_account_id: 12-digit AWS account ID (required if replication enabled)

    - `create_queue` (SQS):
      - identifier: Queue name (required)
      - create_dlq: Create dead letter queue (optional, default: true)
      - fifo_queue: FIFO queue (optional, default: true)
      - visibility_timeout_seconds: Visibility timeout (optional, 0-43200)
      - max_receive_count: Max receive count before DLQ (optional, 1-1000)
      - message_retention_seconds: Message retention (optional, 60-1209600, default: 345600)
      - dlq_message_retention_seconds: DLQ message retention (optional, 60-1209600, default: 1209600)
      - cross_account_ids: List of AWS account IDs for cross-account access (optional)

    - `table_management` (DynamoDB):
      - identifier: Table name (required)
      - partition_key: Partition key attribute name (required)
      - partition_key_type: S/N/B (optional, default: S)

    - `add_route` (Kong Gateway):
      - api_name: Kong API name (required)
      - method: HTTP method (required)
      - route: Route pattern (required)

    - `database_creation` (Database):
      - database_name: Name of the database to create (required)
      - db_server_name: Name of the database server (required)
      - condition: Optional dict with {'type': 'mysql'} or {'type': 'postgresql'}
    """
    try:
        user, tenant = user_and_tenant
        service = TerragruntMgmtService()  # No session needed for template rendering

        result = service.get_terragrunt_content(
            resource_type=data.resource_type,
            parameters=data.parameters,
            condition=data.condition
        )

        return GetTerragruntContentResponse(**result)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error rendering terragrunt content: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to render content: {str(e)}")
