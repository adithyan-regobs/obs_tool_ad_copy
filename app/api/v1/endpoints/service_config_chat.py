"""
Service Config Chat API endpoint
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Tuple
import logging

from app.services.service_config_chat_service import ServiceConfigChatService
from app.schemas.service_config_chat_schemas import (
    ServiceConfigChatRequestSchema,
    ServiceConfigChatResponseSchema,
)
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/service-config-chat",
    response_model=ServiceConfigChatResponseSchema,
    summary="Service Config Assistant",
)
async def service_config_chat(
    request: ServiceConfigChatRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Chat with the Service Config Assistant.

    The assistant helps find REFERENCE configs from other services to use as templates.

    The assistant can help you:
    - List available services (excluding current service)
    - Show configuration for a selected reference service
    - Output reference configuration as JSON for form autofill

    Request Body:
        - message: User's message text
        - context: Service config context (required)
            - service_code: Current service code being configured (required)
            - geo_loc_mst_code: Geographic location code (required)
            - environment_enum: Environment (dev/staging/prod) (required)
            - reference_service_name: Reference service name (optional, set after selection)

    Response:
        - chat_info_code: Unique chat session identifier
        - response: AI-generated response
        - intent: Detected user intent
        - matched_services: List of matched reference services (if applicable)
        - reference_service_code: Selected reference service code (if high-confidence match)
        - config_json: Reference configuration JSON (if output_json intent)
        - is_ready: True if config is ready for form autofill

    Example:
        POST /api/v1/service-config-chat
        Body: {
            "message": "List services",
            "context": {
                "service_code": "PAYMENT_API",
                "geo_loc_mst_code": "mumbai",
                "environment_enum": "dev"
            }
        }
    """
    try:
        user, tenant = user_and_tenant

        logger.info(
            "SERVICE CONFIG CHAT - Request received",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "service_code": request.context.service_code,
                "geo_loc": request.context.geo_loc_mst_code,
                "environment": request.context.environment_enum,
                "reference_service": request.context.reference_service_name,
            }
        )

        service = ServiceConfigChatService(db)
        response = await service.execute(
            request=request,
            tenants_mst_code=tenant.code,
            user_mst_code=user.code,
        )

        logger.info(
            "SERVICE CONFIG CHAT - Response sent",
            extra={
                "chat_info_code": response.chat_info_code,
                "intent": response.intent,
                "is_ready": response.is_ready,
            }
        )

        return response

    except Exception as e:
        logger.error(f"SERVICE CONFIG CHAT - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
