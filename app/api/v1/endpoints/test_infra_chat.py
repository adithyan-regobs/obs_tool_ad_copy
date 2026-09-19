"""
Test endpoint for infra_chat without authentication.

This endpoint is ONLY for testing and should be disabled in production.
"""
from fastapi import APIRouter, HTTPException
from datetime import datetime
import logging

from app.services.bot_service import ChatService
from app.schemas.infra_chat_schemas import (
    InfraChatRequestSchema,
    InfraChatResponseSchema,
    UpdatePlacementParamsRequestSchema,
    UpdatePlacementParamsResponseSchema,
    ClearStateRequestSchema,
    ClearStateResponseSchema,
    ChatHistoryRequestSchema,
    ChatHistoryResponseSchema,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["test"])


@router.post("/test-infra-chat", response_model=InfraChatResponseSchema, summary="Test Infrastructure Chat (No Auth)")
async def test_infra_chat(request: InfraChatRequestSchema):
    """
    Test endpoint for infra_chat WITHOUT authentication.

    This is for testing purposes only.

    **Query Parameters:**
    - `tenant_id`: Override tenant ID (default: "aspora")
    - `user_id`: Override user ID (default: "admin@aspora")

    **Note:** Placement parameters should be set separately using `/update-placement-params` endpoint.

    **Example:**
    ```bash
    # Step 1: Set placement parameters (optional)
    curl -X POST "http://localhost:8000/api/v1/update-placement-params" \\
      -H "Content-Type: application/json" \\
      -d '{
        "conversation_id": "test-001",
        "placement_parameters": {
          "infra_vendor_enum": "AWS",
          "environment_enum": "dev",
          "geo_loc_mst_code": "london",
          "case_type_ref_code": "create-s3"
        }
      }'

    # Step 2: Send message
    curl -X POST "http://localhost:8000/api/v1/test-infra-chat?tenant_id=aspora&user_id=admin@aspora" \\
      -H "Content-Type: application/json" \\
      -d '{
        "message": "create s3 bucket named my-test-bucket",
        "conversation_id": "test-001"
      }'
    ```
    """
    try:
        from fastapi import Query

        # Extract tenant_id and user_id from query params (with defaults)
        tenant_id = "aspora"
        user_id = "af7c5d64-2744-430a-893f-9cf3ddbaf5b8"  # Valid user code from database

        # Populate JWT fields in request schema
        request.tenants_mst_code = tenant_id
        request.user_mst_code = user_id

        logger.info(
            f"[TEST] Infra chat request - tenant={tenant_id}, user={user_id}, "
            f"conversation={request.conversation_id}"
        )
        logger.debug(f"[TEST] User message: {request.message[:100]}...")

        # Initialize chat service
        chat_service = ChatService()

        # Process message
        result = await chat_service.process_message(request)

        # Extract response
        response_text = chat_service.get_response_text(result)

        # Build response - use confirmed_parameters if available (after confirmation), otherwise collected_parameters
        attribute_parameters = result.get("confirmed_parameters") or result.get("collected_parameters")
        placement_parameters = result.get("collected_placement_parameters")

        response = InfraChatResponseSchema(
            conversation_id=request.conversation_id,
            response=response_text,
            intent=result.get("turn_intent"),
            resource=result.get("turn_resource"),
            cases=result.get("cases"),
            attribute_parameters=attribute_parameters,
            placement_parameters=placement_parameters,
            is_ready=result.get("is_ready", False),
            created_at=datetime.now(),
            # Structured parameter metadata for frontend rendering
            remaining_placement_parameters=result.get("remaining_placement_parameters"),
            remaining_attribute_parameters=result.get("remaining_parameters")
        )

        logger.info(
            f"[TEST] Response generated - intent={result.get('turn_intent')}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEST] Failed to process message: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process chat message: {str(e)}"
        )


@router.post("/test-update-placement-params", response_model=UpdatePlacementParamsResponseSchema, summary="Test Update Placement Parameters (No Auth)")
async def test_update_placement_params(request: UpdatePlacementParamsRequestSchema):
    """
    Test endpoint for updating placement parameters WITHOUT authentication.

    This is for testing purposes only.

    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/api/v1/test-update-placement-params" \\
      -H "Content-Type: application/json" \\
      -d '{
        "conversation_id": "test-001",
        "placement_parameters": {
          "infra_vendor_enum": "AWS",
          "environment_enum": "dev",
          "geo_loc_mst_code": "london",
          "case_type_ref_code": "create-s3"
        },
        "case_code": "create-s3"
      }'
    ```
    """
    try:
        # Use default tenant and user for testing
        tenant_id = "aspora"
        user_id = "af7c5d64-2744-430a-893f-9cf3ddbaf5b8"  # Valid user code from database

        # Populate JWT fields in request schema
        request.tenants_mst_code = tenant_id
        request.user_mst_code = user_id

        logger.info(
            f"[TEST] Update placement params - tenant={tenant_id}, user={user_id}, "
            f"conversation={request.conversation_id}"
        )
        logger.debug(f"[TEST] Placement parameters: {request.placement_parameters}")

        # Initialize chat service
        chat_service = ChatService()

        # Update placement parameters
        result = await chat_service.update_placement_parameters(request)

        # Build response
        response = UpdatePlacementParamsResponseSchema(
            conversation_id=request.conversation_id,
            status=result["status"],
            collected_placement_parameters=result.get("collected_placement_parameters", {}),
            message=result.get("message")
        )

        logger.info(
            f"[TEST] Placement parameters updated - status={result['status']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEST] Failed to update placement params: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update placement parameters: {str(e)}"
        )


@router.post("/test-clear-state", response_model=ClearStateResponseSchema, summary="Test Clear State (No Auth)")
async def test_clear_state(request: ClearStateRequestSchema):
    """
    Test endpoint for clearing conversation state WITHOUT authentication.

    This endpoint clears the same state values that are cleared when a resource is created:
    - collected_parameters (attribute parameters)
    - remaining_parameters
    - state_hint (workflow state)

    Preserves:
    - collected_placement_parameters (for reuse across multiple resource creations)
    - session_state_history
    - Other persisted state

    This is useful for testing to reset the conversation to a clean state while keeping
    placement parameters intact.

    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/api/v1/test-clear-state" \\
      -H "Content-Type: application/json" \\
      -d '{
        "conversation_id": "test-001"
      }'
    ```
    """
    try:
        # Use default tenant and user for testing
        tenant_id = "aspora"
        user_id = "af7c5d64-2744-430a-893f-9cf3ddbaf5b8"  # Valid user code from database

        # Populate JWT fields in request schema
        request.tenants_mst_code = tenant_id
        request.user_mst_code = user_id

        logger.info(
            f"[TEST] Clear state - tenant={tenant_id}, user={user_id}, "
            f"conversation={request.conversation_id}"
        )

        # Initialize chat service
        chat_service = ChatService()

        # Clear state
        result = await chat_service.clear_state(request)

        # Build response
        response = ClearStateResponseSchema(
            conversation_id=request.conversation_id,
            status=result["status"],
            message=result["message"],
            cleared_items=result["cleared_items"]
        )

        logger.info(
            f"[TEST] State cleared - status={result['status']}, items={result['cleared_items']}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEST] Failed to clear state: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear state: {str(e)}"
        )


@router.post("/test-chat-history", response_model=ChatHistoryResponseSchema, summary="Test Chat History (No Auth)")
async def test_chat_history(request: ChatHistoryRequestSchema):
    """
    Test endpoint for retrieving chat history WITHOUT authentication.

    This is for testing purposes only.

    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/api/v1/test-chat-history" \\
      -H "Content-Type: application/json" \\
      -d '{
        "conversation_id": "test-conv-1"
      }'
    ```
    """
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.api.dependencies import get_db
        from app.services.bot_service import ChatService
        from app.schemas.infra_chat_schemas import ChatHistoryMessageSchema
        from app.db.session import AsyncSessionLocal

        # Use default tenant for testing
        tenant_id = "aspora"

        logger.info(
            f"[TEST] Chat history request - tenant={tenant_id}, "
            f"conversation={request.conversation_id}"
        )

        # Build thread_id
        thread_id = f"{tenant_id}:{request.conversation_id}"

        # Create database session
        async with AsyncSessionLocal() as db:
            # Initialize chat service
            chat_service = ChatService()

            # Get conversation display history (excludes internal LLM workflow trail)
            messages = await chat_service.get_conversation_history(
                db=db,
                tenant_code=tenant_id,
                conversation_id=request.conversation_id
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
                conversation_id=request.conversation_id,
                thread_id=thread_id,
                total_messages=len(message_schemas),
                messages=message_schemas,
                placement_parameters=latest_placement_parameters
            )

        logger.info(
            f"[TEST] Chat history retrieved - messages={len(message_schemas)}"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TEST] Failed to retrieve chat history: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve chat history: {str(e)}"
        )
