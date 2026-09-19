"""
Chat API endpoint for Infrastructure Studio chatbot
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Tuple

from app.services.langgraph_chat_service import LangGraphChatService
from app.schemas.chat_schemas import (
    ChatRequestSchema,
    ChatResponseSchema,
    GetChatByContextRequest,
    GetChatByContextResponse,
    ChatContextSchema,
    ChatHistoryItemSchema,
    ChatHistoryListResponse,
)
from app.repository.chat_info_repository import ChatInfoRepository
from fastapi import Query
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponseSchema, summary="Infrastructure Chat")
async def chat(
    request: ChatRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Send a message to the Infrastructure Studio chatbot.

    The chatbot can help you:
    - Create AWS S3 buckets for object storage
    - Create AWS SQS queues (standard and FIFO) with optional dead letter queues

    This endpoint:
    - Finds or creates a chat session based on infrastructure context
    - Loads conversation history (summary + last 10 messages)
    - Generates AI response using OpenAI
    - Saves conversation to database
    - Auto-generates summaries when message count > threshold

    Security:
        - TODO Phase 2: Extract tenant_code and user_code from JWT
        - Currently requires tenant and user codes in context

    Request Body:
        - message: User's message text (required)
        - context: Infrastructure context (required)
            - tenants_mst_code: Tenant code
            - user_mst_code: User code
            - infra_vendor_enum: Infrastructure vendor (aws/gcp/azure/on_prem)
            - applications_mst_code: Application code (optional)
            - resource_group_mst_code: Resource group code (optional)
            - services_mst_code: Service code (optional)
            - environment_enum: Environment (dev/staging/prod) (optional)

    Response:
        - chat_info_code: Unique chat session identifier
        - response: AI-generated response
        - terraform_code: Extracted Terraform code if present
        - created_at: Timestamp of response

    Examples:
        # Create an S3 bucket
        POST /api/v1/chat
        Body: {
            "message": "Create an S3 bucket called user-uploads",
            "context": {
                "tenants_mst_code": "acme_corp",
                "user_mst_code": "user123",
                "infra_vendor_enum": "aws",
                "environment_enum": "prod"
            }
        }

        # Create an SQS queue
        POST /api/v1/chat
        Body: {
            "message": "Create a FIFO queue called order-processing with DLQ",
            "context": {
                "tenants_mst_code": "acme_corp",
                "user_mst_code": "user123",
                "infra_vendor_enum": "aws",
                "environment_enum": "prod"
            }
        }

        # Add Kong Gateway route
        POST /api/v1/chat
        Body: {
            "message": "Add a GET route ~/api/v1/users$ to partner-dashboard-api",
            "context": {
                "tenants_mst_code": "acme_corp",
                "user_mst_code": "user123",
                "infra_vendor_enum": "aws",
                "applications_mst_code": "webapp",
                "resource_group_mst_code": "rg-backend",
                "environment_enum": "prod"
            }
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Inject tenant and user codes from JWT into the context
        # This ensures the context always uses the authenticated user's data
        request.context.tenants_mst_code = tenant.code
        request.context.user_mst_code = user.code

        # Extract case codes from request
        case_type_code = request.case_type_code
        case_code = request.case_code

        # Inject case_type into context for chat history grouping
        request.context.case_type_ref_code = case_type_code

        # Log chat request
        logger.info(
            "CHAT REQUEST - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "infra_vendor": request.context.infra_vendor_enum,
                "environment": request.context.environment_enum,
                "endpoint": "/chat"
            }
        )
        logger.debug(
            f"Chat context: app={request.context.applications_mst_code}, rg={request.context.resource_group_mst_code}, service={request.context.services_mst_code}, case_type={case_type_code}, case={case_code}"
        )

        # Initialize LangGraph service
        chat_service = LangGraphChatService(db)

        # Execute chat workflow with authenticated context
        result = await chat_service.execute_chat(
            user_message=request.message,
            context=request.context,
            case_code=case_code,
            case_type_code=case_type_code
        )

        # Check for errors
        if result.get("error"):
            logger.error(
                f"Chat processing error: {result['error']}",
                extra={"chat_info_code": result.get("chat_info_code")}
            )
            raise HTTPException(
                status_code=500,
                detail=f"Chat processing error: {result['error']}"
            )

        # Build response
        response = ChatResponseSchema(
            chat_info_code=result["chat_info_code"],
            response=result["response"],
            terraform_code=result.get("terraform_code"),
            service_type=result.get("service_type"),
            is_ready=result.get("is_ready", False),
            parameters=result.get("parameters"),
            created_at=datetime.now()
        )

        logger.info(
            f"Chat response generated successfully",
            extra={
                "chat_info_code": result["chat_info_code"],
                "service_type": result.get("service_type"),
                "is_ready": result.get("is_ready", False)
            }
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to process chat message: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process chat message: {str(e)}"
        )


@router.post(
    "/get-by-context",
    summary="Get Chat Messages by Context",
    response_model=GetChatByContextResponse,
    status_code=200
)
async def get_chat_by_context(
    data: GetChatByContextRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> GetChatByContextResponse:
    """
    Fetch all chat messages for a specific infrastructure context combination.

    Security:
        - JWT authentication required
        - Tenant/user isolation enforced: automatically uses authenticated user's tenant and user codes
        - tenants_mst_code and user_mst_code automatically injected from JWT token

    This endpoint finds existing chat sessions based on the exact combination of context fields
    and returns all messages in chronological order. It does NOT create a new chat session.

    **Context Matching Strategy:**
    - Uses deterministic hashing to compute a unique chat_info_code from the context
    - All fields (including null values) must match exactly for the same chat session
    - Changing any field (even from null to a value) = different chat session

    **Required Fields:**
    - `infra_vendor_enum`: Infrastructure vendor (aws/gcp/azure/on_prem)

    **Optional Fields (nullable):**
    - `applications_mst_code`: Application code
    - `resource_group_mst_code`: Resource group code
    - `services_mst_code`: Service code
    - `environment_enum`: Environment (dev/staging/prod)

    **How it works:**
    1. Computes deterministic `chat_info_code` from context hash
    2. Checks if `chat_info` record exists with this code
    3. If exists: Returns all messages for that chat session
    4. If not exists: Returns empty result with `chat_exists: false`

    **Response Fields:**
    - `chat_info_code`: The chat session identifier (null if no chat found)
    - `chat_exists`: Boolean flag indicating whether chat exists
    - `total_messages`: Count of messages in the chat
    - `messages`: Array of message objects (ordered by created_at ascending)
    - `context`: Echo back of the input context for verification

    **Example Request:**
    ```json
    POST /api/v1/chat/get-by-context
    {
      "tenants_mst_code": "vance",
      "user_mst_code": "5a9da126-7fec-445c-8100-e43f4ba8fa46",
      "infra_vendor_enum": "aws",
      "applications_mst_code": null,
      "resource_group_mst_code": null,
      "services_mst_code": null,
      "environment_enum": null
    }
    ```

    **Example Response (Chat Exists):**
    ```json
    {
      "chat_info_code": "CHAT_A1B2C3D4E5F6G7H8",
      "chat_exists": true,
      "total_messages": 4,
      "messages": [
        {
          "id": 1,
          "code": "MSG_ABC123",
          "chat_info_code": "CHAT_A1B2C3D4E5F6G7H8",
          "role": "user",
          "message": "Create an S3 bucket called user-data",
          "summary_status": false,
          "created_at": "2025-01-05T10:00:00Z",
          "updated_at": null
        },
        {
          "id": 2,
          "code": "MSG_DEF456",
          "role": "agent",
          "message": "S3 bucket configuration created successfully...",
          "summary_status": false,
          "created_at": "2025-01-05T10:00:15Z",
          "updated_at": null
        }
      ],
      "context": {
        "tenants_mst_code": "vance",
        "user_mst_code": "5a9da126-7fec-445c-8100-e43f4ba8fa46",
        "infra_vendor_enum": "aws",
        "applications_mst_code": null,
        "resource_group_mst_code": null,
        "services_mst_code": null,
        "environment_enum": null
      }
    }
    ```

    **Example Response (No Chat Exists):**
    ```json
    {
      "chat_info_code": null,
      "chat_exists": false,
      "total_messages": 0,
      "messages": [],
      "context": { ... }
    }
    ```

    **HTTP Status Codes:**
    - 200: Success (even if no chat found - returns empty messages)
    - 400: Validation error (invalid enum value, missing required field)
    - 500: Database error or unexpected failure

    **Use Cases:**
    - Load chat history when user returns to same infrastructure context
    - Display conversation history in UI
    - Export chat logs for specific context
    - Check if user has chatted about specific infrastructure before
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Log request
        logger.info(
            "GET CHAT BY CONTEXT - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "infra_vendor": data.infra_vendor_enum,
                "environment": data.environment_enum,
                "endpoint": "/get-by-context"
            }
        )
        logger.debug(
            f"Context: app={data.applications_mst_code}, rg={data.resource_group_mst_code}, service={data.services_mst_code}"
        )

        # Initialize service
        chat_service = LangGraphChatService(db)

        # Build context schema with tenant/user from JWT
        # Context: geo_loc + case_type + product + env + vendor + service + user + tenant
        context = ChatContextSchema(
            tenants_mst_code=tenant.code,
            user_mst_code=user.code,
            infra_vendor_enum=data.infra_vendor_enum,
            applications_mst_code=data.applications_mst_code,
            resource_group_mst_code=data.resource_group_mst_code,
            services_mst_code=data.services_mst_code,
            environment_enum=data.environment_enum,
            geo_loc_mst_code=data.geo_loc_mst_code,
            case_type_ref_code=data.case_type_ref_code,
        )

        # Get chat messages by context
        result = await chat_service.get_chat_by_context(context)

        logger.info(
            f"Retrieved chat by context: exists={result['chat_exists']}, messages={result['total_messages']}",
            extra={
                "chat_info_code": result.get("chat_info_code"),
                "chat_exists": result["chat_exists"],
                "total_messages": result["total_messages"]
            }
        )

        # Build response
        return GetChatByContextResponse(
            chat_info_code=result["chat_info_code"],
            chat_exists=result["chat_exists"],
            total_messages=result["total_messages"],
            messages=result["messages"],
            context=context
        )

    except HTTPException:
        # Re-raise HTTP exceptions from service layer
        raise
    except ValueError as e:
        # Business logic validation errors
        logger.warning(f"Validation error in get_chat_by_context: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unexpected errors
        logger.error(
            f"Unexpected error in get_chat_by_context: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching chat messages"
        )


@router.get(
    "/history",
    summary="Get Chat History List",
    response_model=ChatHistoryListResponse,
    status_code=200
)
async def get_chat_history(
    limit: int = Query(50, ge=1, le=100, description="Maximum number of chat sessions to return"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> ChatHistoryListResponse:
    """
    Get chat history list for the current user.

    Returns a list of past chat sessions ordered by most recent activity.
    Each item includes context information for displaying in the sidebar.

    Display format: {case_type} - {product} ({env}, {geo_loc}) - {service}
    Example: "Kong - core (dev, mumbai)" or "S3 - core (staging, london) - payment-service"

    Security:
        - JWT authentication required
        - Returns only chats belonging to the authenticated user and tenant

    Query Parameters:
        - limit: Maximum number of chat sessions to return (1-100, default 50)

    Response:
        - chats: List of chat history items with context and message counts
        - total: Total number of chat sessions returned
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        logger.info(
            "GET CHAT HISTORY - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "limit": limit,
                "endpoint": "/chat/history"
            }
        )

        # Get chat history from repository
        chat_repo = ChatInfoRepository(db)
        history_items = await chat_repo.get_user_chat_history(
            tenants_mst_code=tenant.code,
            user_mst_code=user.code,
            limit=limit
        )

        # Convert to response schema
        chats = [
            ChatHistoryItemSchema(**item)
            for item in history_items
        ]

        logger.info(
            f"Retrieved {len(chats)} chat history items",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "total": len(chats)
            }
        )

        return ChatHistoryListResponse(
            chats=chats,
            total=len(chats)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_chat_history: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while fetching chat history"
        )
