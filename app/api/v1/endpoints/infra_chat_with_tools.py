"""
Infrastructure Chat with Tools API endpoint.

Production-grade using MCP tools via LangGraph with checkpointed state.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Tuple
from datetime import datetime
import logging

from app.services.infra_chat_with_tools_service import InfraChatWithToolsService
from app.api.dependencies import get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel


logger = logging.getLogger(__name__)

router = APIRouter()


class InfraChatWithToolsRequest(BaseModel):
    """Request schema for infra chat with tools."""
    message: str
    conversation_id: str  # Required for state persistence
    environment: Optional[str] = None
    geo_loc_code: Optional[str] = None


class InfraChatWithToolsResponse(BaseModel):
    """Response schema - raw output for now."""
    conversation_id: str
    response: str
    intent: Optional[str] = None
    raw_output: Optional[dict] = None
    created_at: datetime


@router.post(
    "/infra-chat-with-tools",
    response_model=InfraChatWithToolsResponse,
    summary="Infrastructure Chat with MCP Tools"
)
async def infra_chat_with_tools(
    request: InfraChatWithToolsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Send a message to the Infrastructure Chat Agent with MCP tools.

    Uses LangGraph with checkpointed state for multi-turn conversations.

    **Tools available:**
    - `list_services`: List all services that have configurations
    - `show_service_config`: Show configuration for a specific service

    **Example:**
    ```json
    POST /api/v1/infra-chat-with-tools
    {
        "message": "list services",
        "conversation_id": "conv-123",
        "environment": "prod",
        "geo_loc_code": "us-east-1"
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        logger.info(
            "INFRA CHAT WITH TOOLS REQUEST",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "conversation_id": request.conversation_id,
                "message": request.message,
            }
        )

        # Build context from request
        context = {
            "tenant_code": tenant.code,
            "environment": request.environment,
            "geo_loc_code": request.geo_loc_code,
        }

        # Build thread_id: tenant:conversation for isolation
        thread_id = f"{tenant.code}:{request.conversation_id}"

        # Invoke service with thread_id for checkpointer
        service = InfraChatWithToolsService()
        result = await service.chat(request.message, thread_id, context)

        # Extract response text from messages
        messages = result.get("messages", [])
        response_text = ""
        if messages:
            last_message = messages[-1]
            response_text = getattr(last_message, "content", str(last_message))

        return InfraChatWithToolsResponse(
            conversation_id=request.conversation_id,
            response=response_text,
            intent=result.get("current_intent"),
            raw_output={"message_count": len(messages)},
            created_at=datetime.now(),
        )

    except Exception as e:
        logger.error(
            f"Failed to process infra chat with tools: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process chat message: {str(e)}"
        )
