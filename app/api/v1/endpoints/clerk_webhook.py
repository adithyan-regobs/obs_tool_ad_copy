from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from svix.webhooks import Webhook, WebhookVerificationError
from app.core.config import settings
from app.services.clerk_webhook_service import ClerkWebhookService
from app.schemas.clerk_webhook_schemas import ClerkWebhookRequest, ClerkWebhookResponse
from app.api.dependencies import get_db
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/clerk-webhook", response_model=ClerkWebhookResponse, summary="Clerk Webhook Handler")
async def clerk_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Webhook endpoint for Clerk authentication events.

    Handles user creation, updates, and deletion from Clerk.

    Events supported:
    - user.created: Creates new user and tenant if needed
    - user.updated: Updates existing user information
    - user.deleted: Soft deletes user

    Request Body:
    - type: Event type (user.created, user.updated, user.deleted)
    - data: User data from Clerk including:
      - clerkId: Clerk user ID
      - firstName, lastName: User name
      - email_address: User email
      - organizationName: Organization/tenant name
      - organizationSubdomain: Tenant code
      - isOrganizationOwner: Whether user owns the org
      - userRole: User's role

    Returns:
        ClerkWebhookResponse with processing status
    """
    raw_body = await request.body()

    try:
        Webhook(settings.clerk_signing_secret).verify(raw_body, dict(request.headers))
    except WebhookVerificationError as e:
        logger.warning(f"Rejected Clerk webhook with invalid signature: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        webhook_data = ClerkWebhookRequest.model_validate_json(raw_body)
    except ValidationError as e:
        logger.error(f"Malformed Clerk webhook payload: {str(e)}")
        raise HTTPException(status_code=422, detail="Malformed webhook payload")

    try:
        logger.info(f"Received Clerk webhook: {webhook_data.type}")

        # Initialize service (transaction management happens in service layer)
        service = ClerkWebhookService(db)

        # Process webhook (commits/rollbacks handled in service)
        result = await service.process_webhook(
            event_type=webhook_data.type,
            raw_data=webhook_data.data
        )

        return ClerkWebhookResponse(**result)

    except Exception as e:
        logger.error(f"Error processing Clerk webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {str(e)}")
