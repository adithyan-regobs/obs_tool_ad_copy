"""
Infrastructure Chat Agent API endpoint.

This endpoint provides an intelligent chat interface for AWS infrastructure management.
It uses LangGraph for orchestration and supports multi-tenant configurations.
"""
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Tuple
from datetime import datetime
import logging

from app.services.bot_service import ChatService
from app.schemas.infra_chat_schemas import (
    InfraChatRequestSchema,
    InfraChatResponseSchema,
    UpdatePlacementParamsRequestSchema,
    UpdatePlacementParamsResponseSchema,
    ChatHistoryRequestSchema,
    ChatHistoryMessageSchema,
    ChatHistoryResponseSchema,
)
from app.api.dependencies import get_current_user_and_tenant, get_db
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/infra-chat", response_model=InfraChatResponseSchema, summary="Infrastructure Chat Agent")
async def infra_chat(
    request: InfraChatRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Send a message to the Infrastructure Chat Agent.

    The agent can help you:
    - **CREATE**: Create AWS resources (S3 buckets, SQS queues, Lambda functions, etc.)
    - **REFERENCE**: Look up information about existing resources and tools
    - **QA**: Answer questions about your infrastructure

    **How it works:**
    1. Detects intent using deterministic rules or LLM fallback
    2. Checks RBAC permissions based on user role (admin/viewer)
    3. Extracts parameters for resource creation (if CREATE intent)
    4. Returns a structured response with intent, parameters, and status

    **Security:**
    - JWT authentication required
    - Tenant and user codes automatically extracted from JWT token
    - RBAC enforced: Admins can create resources, viewers can only query

    **Request Body:**
    - `message`: User's message text (required)
    - `conversation_id`: Unique conversation identifier for maintaining context (required)

    **Note:** Placement parameters (environment, geo_loc, application, etc.) should be set
    separately using the `/update-placement-params` endpoint before or during the conversation.

    **Response:**
    - `conversation_id`: Echo of the conversation ID
    - `response`: Human-readable response message
    - `intent`: Detected intent (CREATE/REFERENCE/QA/UNSUPPORTED)
    - `resource`: Resource type (for CREATE intent)
    - `parameters`: Extracted parameters (for CREATE intent)
    - `created_at`: Timestamp of response

    **Examples:**

    *Create an S3 bucket:*
    ```json
    # Step 1: Set placement parameters
    POST /api/v1/update-placement-params
    {
        "conversation_id": "conv-123",
        "placement_parameters": {
            "infra_vendor_enum": "AWS",
            "environment_enum": "dev",
            "geo_loc_mst_code": "london",
            "case_type_ref_code": "create-s3"
        }
    }

    # Step 2: Send creation message
    POST /api/v1/infra-chat
    {
        "message": "Create an S3 bucket called user-uploads",
        "conversation_id": "conv-123"
    }
    ```

    *Create an SQS queue:*
    ```json
    POST /api/v1/infra-chat
    {
        "message": "Create a FIFO queue named order-processing in prod environment",
        "conversation_id": "conv-123"
    }
    ```

    *Query existing tools:*
    ```json
    POST /api/v1/infra-chat
    {
        "message": "What tools are available?",
        "conversation_id": "conv-456"
    }
    ```
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Populate JWT fields in request schema
        request.tenants_mst_code = tenant.code
        request.user_mst_code = user.code

        # Log chat request with thread_id for easy tracking
        thread_id = request.conversation_id
        logger.info(f"[THREAD: {thread_id}] Processing infra chat request")
        logger.info(
            "INFRA CHAT REQUEST - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "conversation_id": request.conversation_id,
                "endpoint": "/infra-chat",
            }
        )
        logger.debug(
            f"User message: {request.message[:100]}..."  # Truncate for log
        )

        # Initialize chat service
        chat_service = ChatService()

        # Process message through the infra chat agent graph
        result = await chat_service.process_message(request)

        # Extract human-readable response
        response_text = chat_service.get_response_text(result)

        # Build response schema - use confirmed_parameters if available (after confirmation), otherwise collected_parameters
        attribute_parameters = result.get("confirmed_parameters") or result.get("collected_parameters")
        placement_parameters = result.get("collected_placement_parameters")

        response = InfraChatResponseSchema(
            conversation_id=request.conversation_id,
            response=response_text,
            intent=result.get("turn_intent"),
            resource=result.get("turn_resource"),
            cases=result.get("cases"),  # Add cases from resource meta
            attribute_parameters=attribute_parameters,
            placement_parameters=placement_parameters,
            is_ready=result.get("is_ready", False),
            queue_status=result.get("queue_status"),
            created_at=datetime.now(),
            # Structured parameter metadata for frontend rendering
            remaining_placement_parameters=result.get("remaining_placement_parameters"),
            remaining_attribute_parameters=result.get("remaining_parameters"),
            remaining_reference_parameters=result.get("remaining_reference_parameters"),
            # For REFERENCE list_services - service matches for clickable UI buttons
            matched_services=result.get("matched_services"),
        )

        logger.info(
            f"Infra chat response generated successfully",
            extra={
                "conversation_id": request.conversation_id,
                "tenant_code": tenant.code,
                "user_code": user.code,
                "intent": result.get("turn_intent")
            }
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to process infra chat message: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None,
                "conversation_id": request.conversation_id if 'request' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process chat message: {str(e)}"
        )


@router.post("/update-placement-params", response_model=UpdatePlacementParamsResponseSchema, summary="Update Placement Parameters")
async def update_placement_params(
    request: UpdatePlacementParamsRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Update placement parameters for a conversation.

    This endpoint allows you to set or update placement parameters (like environment,
    geo_loc, application, etc.) for a conversation. This can be called:

    1. **Before starting a conversation** - to pre-populate placement parameters
    2. **During an active conversation** - to update existing placement parameters

    Placement parameters are merged with existing values, not replaced.

    **Security:**
    - JWT authentication required
    - Tenant and user codes automatically extracted from JWT token

    **Request Body:**
    - `conversation_id`: Unique conversation identifier (required)
    - `placement_parameters`: Dictionary of placement parameters to update (required)

    **Response:**
    - `conversation_id`: Echo of the conversation ID
    - `status`: "success" or "error"
    - `collected_placement_parameters`: Complete set of placement parameters after update
    - `message`: Human-readable status message

    **Example:**

    *Set placement parameters before conversation:*
    ```json
    POST /api/v1/update-placement-params
    {
        "conversation_id": "conv-123",
        "placement_parameters": {
            "infra_vendor_enum": "AWS",
            "applications_mst_code": "app-001",
            "environment_enum": "prod",
            "geo_loc_mst_code": "us-east-1"
        }
    }
    ```

    *Update placement parameters during conversation:*
    ```json
    POST /api/v1/update-placement-params
    {
        "conversation_id": "conv-123",
        "placement_parameters": {
            "environment_enum": "dev"
        }
    }
    ```
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Populate JWT fields in request schema
        request.tenants_mst_code = tenant.code
        request.user_mst_code = user.code

        # Log request
        logger.info(
            "UPDATE PLACEMENT PARAMS REQUEST - Authenticated access",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "conversation_id": request.conversation_id,
                "endpoint": "/update-placement-params",
            }
        )
        logger.debug(
            f"Placement parameters to update: {request.placement_parameters}"
        )

        # Initialize chat service
        chat_service = ChatService()

        # Update placement parameters in graph checkpoint
        result = await chat_service.update_placement_parameters(request)

        # Build response schema
        response = UpdatePlacementParamsResponseSchema(
            conversation_id=request.conversation_id,
            status=result["status"],
            collected_placement_parameters=result.get("collected_placement_parameters", {}),
            message=result.get("message")
        )

        logger.info(
            f"Placement parameters updated successfully",
            extra={
                "conversation_id": request.conversation_id,
                "tenant_code": tenant.code,
                "user_code": user.code,
                "status": result["status"]
            }
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to update placement parameters: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None,
                "conversation_id": request.conversation_id if 'request' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update placement parameters: {str(e)}"
        )


@router.post(
    "/infra-chat/history",
    response_model=ChatHistoryResponseSchema,
    summary="Get Chat History",
    status_code=200
)
async def get_chat_history(
    request: ChatHistoryRequestSchema,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
) -> ChatHistoryResponseSchema:
    """
    Retrieve chat history for a specific conversation.

    Returns all messages (user and agent) for the conversation in chronological order.
    Automatically filtered by tenant from JWT token for security.

    **Security:**
    - JWT authentication required
    - Tenant isolation enforced: Only messages for authenticated tenant are returned
    - Thread ID format: {tenant_code}:{conversation_id}

    **Request Body:**
    - `conversation_id`: Unique conversation identifier (required)

    **Response:**
    - `conversation_id`: The conversation identifier
    - `thread_id`: Full thread identifier (tenant:conversation)
    - `total_messages`: Number of messages returned
    - `messages`: Array of message objects ordered by id (ascending)

    **Example Request:**
    ```json
    POST /api/v1/infra-chat/history
    {
        "conversation_id": "conv-123"
    }
    ```

    **Example Response:**
    ```json
    {
      "conversation_id": "conv-123",
      "thread_id": "aspora:conv-123",
      "total_messages": 4,
      "messages": [
        {
          "id": "uuid-1",
          "code": "MSG-001",
          "role": "user",
          "message": "create s3",
          "intent": "CREATE",
          "resource": "s3",
          "created_at": "2026-01-08T12:40:45Z"
        },
        {
          "id": "uuid-2",
          "code": "MSG-002",
          "role": "agent",
          "message": "I need a few more details...",
          "intent": "CREATE",
          "resource": "s3",
          "created_at": "2026-01-08T12:40:46Z"
        }
      ]
    }
    ```

    **HTTP Status Codes:**
    - 200: Success (even if no messages found - returns empty array)
    - 400: Validation error (invalid conversation_id)
    - 401: Unauthorized (invalid or missing JWT token)
    - 500: Server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Build thread_id from tenant and conversation_id
        thread_id = request.conversation_id
        if request.ticket_id is not None:
            from app.repository.ticket_repository import TicketRepository

            ticket_repo = TicketRepository(db)
            ticket = await ticket_repo.get_by_id(request.ticket_id)
            if not ticket or ticket.tenants_mst_code != tenant.code:
                raise HTTPException(
                    status_code=404,
                    detail=f"Ticket {request.ticket_id} not found"
                )
            thread_id = ticket.code



        # Initialize chat service
        chat_service = ChatService()

        # Get conversation display history (excludes internal LLM workflow trail)
        messages = await chat_service.get_conversation_history(
            db=db,
            tenant_code=tenant.code,
            conversation_id=thread_id,
            ticket_id=request.ticket_id
        )

        # Convert ORM models to Pydantic schemas
        message_schemas = [
            ChatHistoryMessageSchema(
                id=str(msg.id),
                code=msg.code,
                role=msg.role,
                message=msg.message,
                intent=msg.intent,
                resource=msg.resource,
                created_at=msg.created_at
            )
            for msg in messages
        ]
        latest_placement_parameters = None
        for msg in reversed(messages):
            if msg.placement_parameters:
                latest_placement_parameters = msg.placement_parameters
                break

        # Build response
        response = ChatHistoryResponseSchema(
            conversation_id=thread_id,
            thread_id=thread_id,
            total_messages=len(message_schemas),
            messages=message_schemas,
            placement_parameters=latest_placement_parameters
        )


        logger.info(
            f"Chat history retrieved successfully: {len(message_schemas)} messages",
            extra={
                "user_code": user.code,
                "tenant_code": tenant.code,
                "conversation_id": request.conversation_id,
                "total_messages": len(message_schemas)
            }
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to retrieve chat history: {str(e)}",
            exc_info=True,
            extra={
                "user_code": user.code if 'user' in locals() else None,
                "tenant_code": tenant.code if 'tenant' in locals() else None,
                "conversation_id": request.conversation_id if 'request' in locals() else None
            }
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve chat history: {str(e)}"
        )
