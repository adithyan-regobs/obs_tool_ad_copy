"""
API endpoints for user self-service signup with organization creation
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.schemas.user_signup_schemas import (
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    CreateTrailApplicationRequest,
    CreateTrailApplicationResponse,
    ProvisionSubdomainRequest,
    ProvisionSubdomainResponse,
    ProvisionInfrastructureRequest,
    ProvisionInfrastructureResponse,
)
from app.schemas.sales_inquiry_schemas import (
    ValidateEmailRequest,
    ValidateEmailResponse,
    SalesInquiryRequest,
    SalesInquiryResponse,
)
from app.services.user_signup_service import UserSignupService
from app.services.sales_inquiry_service import SalesInquiryService
from app.services.domain_provisioning_service import DomainProvisioningService
from app.services.org_infrastructure_service import OrgInfrastructureService
from app.api.dependencies import get_db, get_clerk_user_id_from_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signup", tags=["signup"])


@router.post("/create-organization", response_model=CreateOrganizationResponse)
async def create_organization(
    request: CreateOrganizationRequest,
    clerk_user_id: str = Depends(get_clerk_user_id_from_jwt),
    db: AsyncSession = Depends(get_db)
):
    """
    Create organization and user account

    This endpoint is called after user signs up with Clerk.
    User exists in Clerk (has session) but NOT in our database yet.

    Flow:
    1. User signs up with Clerk (frontend) → Gets session
    2. User fills organization form (frontend)
    3. Frontend calls this endpoint with ALL data
    4. Backend creates tenant and user in database
    5. Backend updates Clerk public metadata with subdomain
    6. Frontend redirects to dashboard

    Security:
    - Requires valid Clerk JWT token
    - Validates subdomain availability
    - Prevents duplicate user creation

    Args:
        request: Organization creation request with user and org details
        clerk_user_id: Clerk user ID extracted from JWT token
        db: Database session

    Returns:
        CreateOrganizationResponse with user and tenant information

    Raises:
        400: User already exists or subdomain taken
        401: Invalid JWT token
        500: Server error during creation
    """
    logger.info("=" * 60)
    logger.info("[SIGNUP API] Received organization creation request")
    logger.info(f"[SIGNUP API] Clerk User ID (from JWT): {clerk_user_id}")
    logger.info(f"[SIGNUP API] Email: {request.email}")
    logger.info(f"[SIGNUP API] Name: {request.firstName} {request.lastName}")
    logger.info(f"[SIGNUP API] Organization: {request.organizationName}")
    logger.info(f"[SIGNUP API] Subdomain: {request.organizationSubdomain}")
    logger.info(f"[SIGNUP API] User Role: {request.userRole}")
    logger.info("=" * 60)

    # Initialize signup service
    signup_service = UserSignupService(db)

    try:
        # Create organization and user
        result = await signup_service.create_organization(
            clerk_user_id=clerk_user_id,
            email=request.email,
            first_name=request.firstName,
            last_name=request.lastName,
            organization_name=request.organizationName,
            organization_subdomain=request.organizationSubdomain,
            user_role=request.userRole,
            is_organization_owner=request.isOrganizationOwner
        )

        logger.info(
            f"[SIGNUP API] Successfully created organization. "
            f"User: {result['user'].code}, Tenant: {result['tenant'].code}"
        )

        return CreateOrganizationResponse(
            success=result["success"],
            message=result["message"],
            user=result["user"],
            tenant=result["tenant"],
        )

    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise

    except Exception as e:
        logger.error(f"[SIGNUP API] Unexpected error during organization creation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create organization: {str(e)}"
        )


@router.post("/create-trail-application", response_model=CreateTrailApplicationResponse)
async def create_trail_application(
    request: CreateTrailApplicationRequest,
    clerk_user_id: str = Depends(get_clerk_user_id_from_jwt),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a trail application and default resource group for a newly signed-up tenant.

    Called by frontend after successful organization creation.
    Creates an application named "{tenant_code}_trail" with a default resource group.

    Security:
    - Requires valid Clerk JWT token
    """
    logger.info(
        f"[SIGNUP API] Creating trail application for subdomain: "
        f"{request.subdomain} (clerk_user: {clerk_user_id})"
    )

    signup_service = UserSignupService(db)

    try:
        result = await signup_service.create_trail_application(
            subdomain=request.subdomain,
            clerk_user_id=clerk_user_id,
        )
        return CreateTrailApplicationResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"[SIGNUP API] Trail application creation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create trail application: {str(e)}"
        )


@router.post("/provision-subdomain", response_model=ProvisionSubdomainResponse)
async def provision_subdomain(
    request: ProvisionSubdomainRequest,
    clerk_user_id: str = Depends(get_clerk_user_id_from_jwt),
):
    """
    Provision subdomain routing (Vercel domain) for a newly created organization.

    Called by frontend after organization creation. Adds {subdomain}.devlift.ai
    to the Vercel project. DNS is handled by the wildcard CNAME in Cloudflare.

    This is idempotent — safe to call multiple times for the same subdomain.

    Security:
    - Requires valid Clerk JWT token
    """
    logger.info(
        f"[SIGNUP API] Provisioning subdomain: {request.subdomain} "
        f"(clerk_user: {clerk_user_id})"
    )

    service = DomainProvisioningService()

    try:
        result = await service.provision_subdomain(request.subdomain)
        return ProvisionSubdomainResponse(**result)

    except Exception as e:
        logger.error(f"[SIGNUP API] Subdomain provisioning failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to provision subdomain: {str(e)}"
        )


@router.post("/provision-infrastructure", response_model=ProvisionInfrastructureResponse)
async def provision_infrastructure(
    request: ProvisionInfrastructureRequest,
    clerk_user_id: str = Depends(get_clerk_user_id_from_jwt),
    db: AsyncSession = Depends(get_db),
):
    """
    Provision infrastructure repo for an organization.

    Called by frontend after successful signup. Creates a per-org GitHub repo
    (Devlift-ai/{subdomain}-infrastructure), fetches scaffold from the source
    infra repo, renders tenant-specific terragrunt files, and commits to main.

    This is idempotent — if the repo already exists, it detects no changes.

    Security:
    - Requires valid Clerk JWT token

    Args:
        request: Contains the organization subdomain
        clerk_user_id: Clerk user ID extracted from JWT token

    Returns:
        ProvisionInfrastructureResponse with repo URL and status
    """
    logger.info(
        f"[INFRA API] Provision infrastructure request for subdomain: "
        f"{request.subdomain} (clerk_user: {clerk_user_id})"
    )

    try:
        infra_service = OrgInfrastructureService(db)
        result = await infra_service.provision_infrastructure(
            subdomain=request.subdomain,
            user_email=clerk_user_id,
        )

        return ProvisionInfrastructureResponse(
            status=result.get("status", "error"),
            repo_url=result.get("repo_url"),
            files_committed=result.get("files_committed"),
            message=result.get("message"),
            error=result.get("error"),
        )

    except Exception as e:
        logger.error(f"[INFRA API] Infrastructure provisioning failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to provision infrastructure: {str(e)}"
        )


# ─── Public endpoints (no auth required) ────────────────────────────


@router.post("/validate-email", response_model=ValidateEmailResponse)
async def validate_email(
    request: ValidateEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate whether an email is eligible for signup.

    PUBLIC endpoint — no authentication required.
    Called before Google OAuth to gate signup access.

    Current validation: basic @ check (will be elaborated in future).
    If not eligible, frontend shows a sales inquiry form instead.
    """
    logger.info(f"[SIGNUP API] Email validation request for: {request.email}")

    service = SalesInquiryService(db)

    try:
        result = await service.validate_email(request.email)
        return ValidateEmailResponse(**result)

    except Exception as e:
        logger.error(f"[SIGNUP API] Email validation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Email validation failed: {str(e)}"
        )


@router.post("/sales-inquiry", response_model=SalesInquiryResponse)
async def submit_sales_inquiry(
    request: SalesInquiryRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a sales inquiry form.

    PUBLIC endpoint — no authentication required.
    Called when an unknown user fills the interest form after failing
    email validation during signup.

    Saves the inquiry to the sales_inquiries table for the sales team.
    """
    logger.info(f"[SIGNUP API] Sales inquiry submission from: {request.email}")

    service = SalesInquiryService(db)

    try:
        result = await service.create_inquiry(request)
        return SalesInquiryResponse(**result)

    except Exception as e:
        logger.error(f"[SIGNUP API] Sales inquiry submission failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit sales inquiry: {str(e)}"
        )
