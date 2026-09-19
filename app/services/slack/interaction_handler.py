"""Slack Interaction Handler

Handles interactive components like buttons and dropdowns.
Uses in-memory cache to collect placement params, then sends all at once to infraChat.
"""
import asyncio
from typing import Dict, Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient
from fastapi import HTTPException

from app.services.slack.base_handler import BaseSlackHandler
from app.services.bot_service import ChatService
from app.services.ticket_service import TicketService
from app.services.slack.db_utils import managed_session
from app.services.transaction_queue_service import TransactionQueueService
from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.services.infrastructure_creation_service import InfrastructureCreationService
from app.services.slack import placement_cache
from app.services.slack.shared import PlacementUIHelper, InfrastructureRequestBuilder
from app.services.slack.shared.infrastructure_request_builder import (
    is_v2_text_placement_resource,
    validate_placement_params,
    resolve_kong_host_config_code,
)
from app.services.slack.deployment_handler import DeploymentHandler, DeploymentContext
from app.schemas.infra_chat_schemas import (
    InfraChatRequestSchema,
    UpdatePlacementParamsRequestSchema
)
from app.core.enum import WorkflowSourceTableEnum
import logging

logger = logging.getLogger(__name__)


class SlackInteractionHandler(BaseSlackHandler):
    """Handles Slack interactive component actions."""

    def __init__(self, db: AsyncSession, slack_client: AsyncWebClient, tenant_code: str):
        super().__init__(db, slack_client, tenant_code)
        self.chat_service = ChatService()
        self.ticket_service = TicketService(db)
        self.queue_service = TransactionQueueService(db)
        self.pr_workflow_service = ScriptPRWorkflowService(db)
        self.infrastructure_service = InfrastructureCreationService(db)
        self.placement_ui = PlacementUIHelper(slack_client, self.block_builder)
        self.request_builder = InfrastructureRequestBuilder(tenant_code)
        self.deployment_handler = DeploymentHandler(db, slack_client, tenant_code)

    async def handle_block_action(self, payload: Dict[str, Any]):
        """Handle block action (button click, dropdown selection, etc.).

        Args:
            payload: Slack interaction payload
        """
        try:
            action = payload["actions"][0]
            action_id = action["action_id"]
            action_type = action["type"]

            channel_id = payload["channel"]["id"]
            message_ts = payload["message"]["ts"]
            thread_ts = payload["message"].get("thread_ts", message_ts)

            logger.info(f"Block action: {action_id} (type: {action_type})")
            logger.info(f"Channel: {channel_id}, message_ts: {message_ts}, thread_ts: {thread_ts}")

            # Get conversation_id to check lock
            conversation_id = self._extract_conversation_id(action, channel_id, thread_ts)
            if conversation_id:
                # Check if conversation is locked (LLM is processing or button already clicked)
                if await placement_cache.is_conversation_locked(conversation_id):
                    logger.info(f"Ignoring action - conversation {conversation_id} is locked (double-click or concurrent operation)")
                    return  # Silently ignore - button already updated by first click

            # Route to appropriate handler
            if action_id.startswith("select_"):
                await self._handle_placement_parameter_selection(
                    payload, channel_id, thread_ts, message_ts, action
                )
            elif action_id == "preview_deployment":
                await self._handle_preview_action(payload, channel_id, thread_ts, message_ts, action)
            elif action_id == "deploy_infrastructure" or action_id == "create_pr":
                await self._handle_create_pr_action(payload, channel_id, thread_ts, message_ts, action)
            elif action_id == "cancel_deployment":
                await self._handle_cancel_action(payload, channel_id, thread_ts, message_ts)
            else:
                logger.warning(f"Unknown action_id: {action_id}")
                await self._respond_to_action(payload, "⚠️ Unknown action")

        except Exception as e:
            logger.error(f"Error handling block action: {str(e)}", exc_info=True)
            await self._respond_to_action(payload, "❌ Something went wrong. Please try again.")

    async def _handle_placement_parameter_selection(
        self,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        action: Dict[str, Any]
    ):
        """Handle placement parameter selection (button/dropdown).

        Uses in-memory cache to store selections. Only calls infraChat when all params collected.

        Args:
            payload: Slack interaction payload
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            message_ts: Message timestamp (for updating the button message)
            action: Action details
        """
        try:
            action_id = action["action_id"]

            # Get selected value and conversation_id from button value
            # Format: "conversation_id|selected_value"
            if action["type"] == "static_select":
                raw_value = action["selected_option"]["value"]
                selected_label = action["selected_option"]["text"]["text"]
            elif action["type"] == "button":
                raw_value = action["value"]
                selected_label = action["text"]["text"]
            else:
                logger.error(f"Unsupported action type: {action['type']}")
                return

            # Parse conversation_id and selected_value from button value
            if "|" in raw_value:
                conversation_id, selected_value = raw_value.split("|", 1)
            else:
                selected_value = raw_value
                slack_thread_id = f"{channel_id}_{thread_ts}"
                ticket = await self.ticket_service.get_ticket_by_source_ref(
                    source="slack",
                    source_ref_id=slack_thread_id
                )
                if not ticket:
                    logger.error(f"No ticket found for slack_thread_id: {slack_thread_id}")
                    await self._post_message(
                        channel_id,
                        thread_ts,
                        "❌ Error: Conversation not found. Please start a new conversation."
                    )
                    return
                conversation_id = ticket.code

            # Extract param_name from action_id
            action_id_without_prefix = action_id.replace("select_", "")
            if f"_{selected_value}" in action_id_without_prefix:
                param_name = action_id_without_prefix.rsplit(f"_{selected_value}", 1)[0]
            else:
                param_name = action_id_without_prefix

            logger.info(f"Selected {param_name}={selected_value} for conversation {conversation_id}")

            # Get cache state
            cache_state = await placement_cache.get_placement_state(conversation_id)

            if not cache_state:
                logger.error(f"No cache state for {conversation_id}")
                await self._post_message(
                    channel_id,
                    thread_ts,
                    "❌ Error: Session expired. Please start a new conversation."
                )
                return

            # Get display name from cache for the param being selected
            display_name = param_name.replace("_", " ").title()
            if param_name in cache_state.get("remaining", {}):
                param_meta = cache_state["remaining"][param_name]
                display_name = param_meta.get("name", display_name)

            # IMMEDIATELY update the button message to show selected value (removes buttons)
            await self._update_message_to_selected(
                channel_id, message_ts, display_name, selected_label
            )

            # Clear the pending placement message since selection was made
            await placement_cache.clear_pending_placement_message(conversation_id)

            # Delete any "please complete selection" reminder message
            await self._delete_pending_reminder_message(conversation_id)

            # Acquire lock to prevent double-click processing
            if not await placement_cache.acquire_conversation_lock(conversation_id, "processing selection"):
                logger.info(f"Selection already being processed for {conversation_id}")
                return  # Button already updated, just skip duplicate processing

            try:
                # Store selection in cache (pass label for services_mst_code to derive api_name)
                await placement_cache.update_placement_selection(
                    conversation_id, param_name, selected_value, selected_label
                )

                # Check if all params collected
                if await placement_cache.is_placement_complete(conversation_id):
                    await self._finalize_placement_params(conversation_id, channel_id, thread_ts)
                else:
                    # Use shared helper for showing next param UI
                    # This may auto-select single-option params, so check completion after
                    await self.placement_ui.show_next_param_ui(conversation_id, channel_id, thread_ts)

                    # Check again after show_next_param_ui (it may have auto-selected remaining params)
                    if await placement_cache.is_placement_complete(conversation_id):
                        await self._finalize_placement_params(conversation_id, channel_id, thread_ts)
            finally:
                # Delete any pending "please wait" messages before releasing lock
                await self._delete_pending_wait_messages(conversation_id)
                # Release lock after processing
                await placement_cache.release_conversation_lock(conversation_id)

        except Exception as e:
            logger.error(f"Error handling placement parameter selection: {str(e)}", exc_info=True)
            await self._post_message(channel_id, thread_ts, "❌ Something went wrong. Please try again.")

    async def _update_message_to_processing(
        self,
        channel_id: str,
        message_ts: str,
        status_text: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None
    ):
        """Update message to show processing status (removes buttons).

        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp to update
            status_text: Status text to display (e.g., "Generating preview...")
            original_blocks: Original message blocks from payload (preferred over API fetch)
        """
        try:
            # Use provided blocks, or fetch if not available
            if not original_blocks:
                result = await self.slack_client.conversations_history(
                    channel=channel_id,
                    latest=message_ts,
                    limit=1,
                    inclusive=True
                )
                if result.get("messages"):
                    original_blocks = result["messages"][0].get("blocks", [])
                else:
                    original_blocks = []

            # Keep all blocks except the actions block, add status as context
            new_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            new_blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"⏳ {status_text}"
                }]
            })

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=new_blocks,
                text=status_text
            )
        except Exception as e:
            logger.warning(f"Could not update message to processing: {e}")

    async def _update_message_to_selected(
        self,
        channel_id: str,
        message_ts: str,
        display_name: str,
        selected_label: str
    ):
        """Update the button message to show selected value as text.

        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp to update
            display_name: Display name of the parameter
            selected_label: Label of the selected option
        """
        try:
            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{display_name}:* {selected_label}"
                        }
                    }
                ],
                text=f"{display_name}: {selected_label}"
            )
        except Exception as e:
            logger.warning(f"Could not update message to selected: {e}")

    async def _finalize_placement_params(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str
    ):
        """Send all collected placement params to infraChat at once."""
        try:
            state = await placement_cache.get_placement_state(conversation_id)
            if not state:
                logger.error(f"No cache state found for {conversation_id}")
                await self._post_message(
                    channel_id,
                    thread_ts,
                    "❌ Error: Session expired. Please start a new conversation."
                )
                return

            collected_params = state["collected"]
            user_mst_code = state["user_mst_code"]
            tenant_code = state["tenant_code"]

            logger.info(f"Finalizing placement params for {conversation_id}: {collected_params}")
            logger.info(f"[FINALIZE] _service_mst_name in collected_params: {collected_params.get('_service_mst_name')}")
            logger.info(f"[FINALIZE] _applications_mst_name in collected_params: {collected_params.get('_applications_mst_name')}")
            logger.info(f"[FINALIZE] applications_mst_code in collected_params: {collected_params.get('applications_mst_code')}")

            # Update all placement parameters at once
            request = UpdatePlacementParamsRequestSchema(
                conversation_id=conversation_id,
                placement_parameters=collected_params,
                tenants_mst_code=tenant_code,
                user_mst_code=user_mst_code
            )
            await self.chat_service.update_placement_parameters(request)

            # Clear cache
            await placement_cache.clear_placement_state(conversation_id)

            # Call infraChat to continue
            next_request = InfraChatRequestSchema(
                message="[All placement parameters collected]",
                conversation_id=conversation_id,
                tenants_mst_code=tenant_code,
                user_mst_code=user_mst_code
            )

            result = await self.chat_service.process_message(next_request)
            response_text = self.chat_service.get_response_text(result)
            is_ready = result.get("is_ready", False)
            collected_parameters = result.get("collected_parameters", {})

            if is_ready:
                # Use shared helper for deployment confirmation
                await self.placement_ui.show_deployment_confirmation(
                    conversation_id, channel_id, thread_ts, collected_parameters, response_text
                )
            else:
                if response_text:
                    formatted_text = self.format_response_for_slack(response_text)
                    await self._post_message(channel_id, thread_ts, formatted_text)

        except Exception as e:
            logger.error(f"Error finalizing placement params: {str(e)}", exc_info=True)
            await self._post_message(channel_id, thread_ts, "❌ Something went wrong. Please try again.")

    async def _handle_preview_action(
        self,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        action: Dict[str, Any]
    ):
        """Handle Preview button click.

        Non-blocking: immediately acknowledges and processes in background.
        """
        conversation_id = action.get("value")
        logger.info(f"Preview action for conversation {conversation_id}")

        # Get original blocks from payload to preserve configuration summary
        original_blocks = payload.get("message", {}).get("blocks", [])

        # Immediately update message to show processing (removes buttons)
        await self._update_message_to_processing(channel_id, message_ts, "Generating preview...", original_blocks)

        # Acquire lock to prevent double-click
        if not await placement_cache.acquire_conversation_lock(conversation_id, "generating preview"):
            logger.info(f"Preview already being processed for {conversation_id}")
            return

        # Fire and forget - process in background
        asyncio.create_task(
            self._process_preview_background(
                conversation_id, channel_id, thread_ts, message_ts, original_blocks
            )
        )

    async def _process_preview_background(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: List[Dict[str, Any]]
    ):
        """Background task for preview generation.

        Creates its own DB session since the original request context is gone.
        """
        context = None
        try:
            async with managed_session() as db:
                # Create fresh services with new session
                ticket_service = TicketService(db)
                deployment_handler = DeploymentHandler(db, self.slack_client, self.tenant_code)

                # Build deployment context with fresh services
                context = await self._build_deployment_context_with_services(
                    conversation_id, channel_id, thread_ts, message_ts, original_blocks,
                    ticket_service=ticket_service
                )
                if not context:
                    return

                # Validate required placement params for v2 text-placement resources
                if is_v2_text_placement_resource(context.turn_resource):
                    missing_fields = validate_placement_params(context.collected_placement_parameters or {})
                    if missing_fields:
                        await self._post_message(
                            channel_id, thread_ts,
                            f"Cannot generate preview: missing required parameters: {', '.join(missing_fields)}. "
                            f"Please provide these values and try again."
                        )
                        return

                # Delegate to deployment handler
                result = await deployment_handler.handle_preview(context)

                if not result.success:
                    logger.error(f"Preview failed with error: {result.error_message}")
                    message = self._format_error_message_for_context(
                        context=context,
                        base_message="❌ Preview failed. Please try again or contact support if the issue persists.",
                        error_message=result.error_message,
                        status_code=result.error_status_code
                    )
                    await self._post_message(channel_id, thread_ts, message)

        except Exception as e:
            logger.error(f"Error in background preview: {str(e)}", exc_info=True)
            status_code = e.status_code if isinstance(e, HTTPException) else None
            message = self._format_error_message_for_context(
                context=context,
                base_message="❌ Preview failed. Please try again or contact support if the issue persists.",
                error_message=str(e),
                status_code=status_code
            )
            await self._post_message(channel_id, thread_ts, message)
        finally:
            # Delete any pending "please wait" messages before releasing lock
            await self._delete_pending_wait_messages(conversation_id)
            await placement_cache.release_conversation_lock(conversation_id)

    async def _build_deployment_context(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[DeploymentContext]:
        """Build deployment context from conversation state.

        Args:
            conversation_id: Ticket code
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            message_ts: Message timestamp
            original_blocks: Original message blocks to preserve config summary

        Returns:
            DeploymentContext or None if context cannot be built
        """
        slack_thread_id = f"{channel_id}_{thread_ts}"
        ticket = await self.ticket_service.get_ticket_by_source_ref(
            source="slack",
            source_ref_id=slack_thread_id
        )

        if not ticket:
            await self._post_message(channel_id, thread_ts, "❌ Error: Ticket not found.")
            return None

        # thread_id is just the conversation_id (ticket code)
        # Must match bot_service.process_message which uses request.conversation_id directly
        config = {"configurable": {"thread_id": conversation_id}}
        logger.info(f"[BUILD_CONTEXT] Looking up state with thread_id={conversation_id}")
        state = await (await self.chat_service._get_graph()).aget_state(config)

        if not state or not state.values:
            logger.error(f"[BUILD_CONTEXT] State not found for thread_id={conversation_id}, state={state}")
            await self._post_message(channel_id, thread_ts, "❌ Error: Conversation state not found.")
            return None

        state_values = state.values
        turn_resource = state_values.get("turn_resource") or state_values.get("state_hint", {}).get("running_resource")

        # Debug logging for Kong Gateway api_name issue
        collected_placement_params = state_values.get("collected_placement_parameters", {})
        logger.info(f"[BUILD_CONTEXT] conversation_id={conversation_id}")
        logger.info(f"[BUILD_CONTEXT] collected_placement_parameters: {collected_placement_params}")
        logger.info(f"[BUILD_CONTEXT] _service_mst_name: {collected_placement_params.get('_service_mst_name')}")
        logger.info(f"[BUILD_CONTEXT] service_mst_code: {collected_placement_params.get('service_mst_code')}")
        logger.info(f"[BUILD_CONTEXT] api_name: {collected_placement_params.get('api_name')}")

        if not turn_resource:
            await self._post_message(channel_id, thread_ts, "❌ Error: Resource type not found.")
            return None

        # After confirmation, params move to confirmed_parameters and collected_parameters is cleared
        # So we need to check confirmed_parameters first, then fall back to collected_parameters
        collected_params = state_values.get("confirmed_parameters") or state_values.get("collected_parameters", {})

        # For v2 text-placement resources, derive case_type_ref_code from cases
        # since placement_param_validator (which normally sets it) is bypassed
        collected_placement = state_values.get("collected_placement_parameters", {})
        if is_v2_text_placement_resource(turn_resource) and "case_type_ref_code" not in collected_placement:
            cases = state_values.get("cases", [])
            if cases:
                collected_placement["case_type_ref_code"] = cases[0]
                logger.info(f"[BUILD_CONTEXT] Derived case_type_ref_code='{cases[0]}' from cases for v2 resource")

        return DeploymentContext(
            conversation_id=conversation_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            ticket=ticket,
            turn_resource=turn_resource,
            collected_parameters=collected_params,
            collected_placement_parameters=collected_placement,
            original_blocks=original_blocks
        )

    async def _build_deployment_context_with_services(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None,
        ticket_service: Optional[TicketService] = None
    ) -> Optional[DeploymentContext]:
        """Build deployment context using provided services (for background tasks).

        Args:
            conversation_id: Ticket code
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            message_ts: Message timestamp
            original_blocks: Original message blocks to preserve config summary
            ticket_service: TicketService instance to use (for background tasks with fresh session)

        Returns:
            DeploymentContext or None if context cannot be built
        """
        svc = ticket_service or self.ticket_service

        slack_thread_id = f"{channel_id}_{thread_ts}"
        ticket = await svc.get_ticket_by_source_ref(
            source="slack",
            source_ref_id=slack_thread_id
        )

        if not ticket:
            await self._post_message(channel_id, thread_ts, "❌ Error: Ticket not found.")
            return None

        config = {"configurable": {"thread_id": conversation_id}}
        logger.info(f"[BUILD_CONTEXT] Looking up state with thread_id={conversation_id}")
        state = await (await self.chat_service._get_graph()).aget_state(config)

        if not state or not state.values:
            logger.error(f"[BUILD_CONTEXT] State not found for thread_id={conversation_id}, state={state}")
            await self._post_message(channel_id, thread_ts, "❌ Error: Conversation state not found.")
            return None

        state_values = state.values
        turn_resource = state_values.get("turn_resource") or state_values.get("state_hint", {}).get("running_resource")

        # Debug logging for placement parameters
        collected_placement_params = state_values.get("collected_placement_parameters", {})
        logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] conversation_id={conversation_id}")
        logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] state_values keys: {list(state_values.keys())}")
        logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] collected_placement_parameters: {collected_placement_params}")
        logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] applications_mst_code: {collected_placement_params.get('applications_mst_code')}")
        logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] geo_loc_mst_code: {collected_placement_params.get('geo_loc_mst_code')}")

        if not turn_resource:
            await self._post_message(channel_id, thread_ts, "❌ Error: Resource type not found.")
            return None

        # After confirmation, params move to confirmed_parameters and collected_parameters is cleared
        # So we need to check confirmed_parameters first, then fall back to collected_parameters
        collected_params = state_values.get("confirmed_parameters") or state_values.get("collected_parameters", {})

        # For v2 text-placement resources, derive case_type_ref_code from cases
        # since placement_param_validator (which normally sets it) is bypassed
        if is_v2_text_placement_resource(turn_resource) and "case_type_ref_code" not in collected_placement_params:
            cases = state_values.get("cases", [])
            if cases:
                collected_placement_params["case_type_ref_code"] = cases[0]
                logger.info(f"[BUILD_CONTEXT_WITH_SERVICES] Derived case_type_ref_code='{cases[0]}' from cases for v2 resource")

        return DeploymentContext(
            conversation_id=conversation_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            ticket=ticket,
            turn_resource=turn_resource,
            collected_parameters=collected_params,
            collected_placement_parameters=collected_placement_params,
            original_blocks=original_blocks
        )

    @staticmethod
    def _is_kong_context(context: Optional[DeploymentContext]) -> bool:
        if not context:
            return False
        case_type = (context.collected_placement_parameters or {}).get("case_type_ref_code", "")
        return case_type == "add_route" or context.turn_resource == "kong_gateway"

    def _format_error_message_for_context(
        self,
        context: Optional[DeploymentContext],
        base_message: str,
        error_message: Optional[str],
        status_code: Optional[int] = None
    ) -> str:
        if (status_code == 400 or self._is_kong_context(context)) and error_message:
            return f"{base_message}\nError: {error_message}"
        return base_message

    @staticmethod
    def _extract_error_details(error_value: Optional[object]) -> tuple[Optional[str], Optional[int]]:
        if isinstance(error_value, dict):
            return error_value.get("message"), error_value.get("status_code")
        if error_value is None:
            return None, None
        return str(error_value), None

    async def _handle_create_pr_action(
        self,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        action: Dict[str, Any]
    ):
        """Handle Create PR button click.

        Non-blocking: immediately acknowledges and processes in background.
        """
        conversation_id = action.get("value")
        logger.info(f"Create PR action for conversation {conversation_id}")

        # Get original blocks from payload to preserve configuration summary
        original_blocks = payload.get("message", {}).get("blocks", [])

        # Immediately update message to show processing (removes buttons)
        await self._update_message_to_processing(channel_id, message_ts, "Creating Pull Request...", original_blocks)

        # Acquire lock to prevent double-click
        if not await placement_cache.acquire_conversation_lock(conversation_id, "creating PR"):
            logger.info(f"PR creation already being processed for {conversation_id}")
            return

        # Check preview state before spawning background task
        preview_state = await placement_cache.get_preview_state(conversation_id)

        # Fire and forget - process in background
        asyncio.create_task(
            self._process_create_pr_background(
                conversation_id, channel_id, thread_ts, message_ts, original_blocks, preview_state
            )
        )

    async def _process_create_pr_background(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: List[Dict[str, Any]],
        preview_state: Optional[Dict[str, Any]]
    ):
        """Background task for PR creation.

        Creates its own DB session since the original request context is gone.
        """
        context = None
        try:
            async with managed_session() as db:
                # Create fresh services with new session
                ticket_service = TicketService(db)
                deployment_handler = DeploymentHandler(db, self.slack_client, self.tenant_code)

                context = await self._build_deployment_context_with_services(
                    conversation_id, channel_id, thread_ts, message_ts, original_blocks,
                    ticket_service=ticket_service
                )

                if preview_state and preview_state.get("previewed"):
                    # Reuse existing queue item from preview
                    await self._handle_create_pr_after_preview_background(
                        conversation_id=conversation_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        message_ts=message_ts,
                        queue_item_id=preview_state["queue_item_id"],
                        queue_code=preview_state["queue_code"],
                        ticket_service=ticket_service,
                        deployment_handler=deployment_handler
                    )
                else:
                    # Original flow - create infrastructure and queue, then PR
                    await self._handle_create_pr_fresh_background(
                        conversation_id=conversation_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        message_ts=message_ts,
                        original_blocks=original_blocks,
                        ticket_service=ticket_service,
                        db=db
                    )
        except Exception as e:
            logger.error(f"Error in background PR creation: {str(e)}", exc_info=True)
            status_code = e.status_code if isinstance(e, HTTPException) else None
            message = self._format_error_message_for_context(
                context=context,
                base_message="❌ PR creation failed. Please try again or contact support if the issue persists.",
                error_message=str(e),
                status_code=status_code
            )
            await self._post_message(channel_id, thread_ts, message)
        finally:
            # Delete any pending "please wait" messages before releasing lock
            await self._delete_pending_wait_messages(conversation_id)
            await placement_cache.release_conversation_lock(conversation_id)

    async def _handle_create_pr_after_preview(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        queue_item_id: int,
        queue_code: str
    ):
        """Create PR using existing queue item from preview.

        Note: The message_ts here is for the button message posted AFTER preview,
        not the original confirmation message. The original confirmation (with config)
        is preserved - we only update this button message.

        The "Creating Pull Request..." status is already shown by _handle_create_pr_action
        via _update_message_to_processing, so we don't duplicate it here.
        """
        logger.info(f"Creating PR after preview, reusing queue_item_id={queue_item_id}, queue_code={queue_code}")

        # Get ticket for user code
        slack_thread_id = f"{channel_id}_{thread_ts}"
        ticket = await self.ticket_service.get_ticket_by_source_ref(
            source="slack",
            source_ref_id=slack_thread_id
        )

        if not ticket:
            await self._post_message(channel_id, thread_ts, "❌ Error: Ticket not found.")
            return

        # Build context for deployment handler
        context = await self._build_deployment_context(
            conversation_id, channel_id, thread_ts, message_ts
        )

        if context:
            # Use deployment handler
            success, pr_url, queue_code_or_error = await self.deployment_handler.handle_create_pr_after_preview(
                context=context,
                queue_item_id=queue_item_id,
                queue_code=queue_code
            )

            if success:
                if pr_url:
                    # Extract PR number from URL
                    pr_number = pr_url.rstrip('/').split('/')[-1] if pr_url else ""
                    # Simple update - config is in original message above
                    await self._update_button_message_simple(
                        channel_id=channel_id,
                        message_ts=message_ts,
                        text=f"✅ *Pull Request Created!*\n\n📋 Queue Item: `{queue_code_or_error}`\n🔗 <{pr_url}|PR #{pr_number}>"
                    )
                else:
                    await self._update_button_message_simple(
                        channel_id=channel_id,
                        message_ts=message_ts,
                        text=f"⚠️ *Added to queue but no PR created*\n\n📋 Queue Item: `{queue_code_or_error}`"
                    )
            else:
                logger.error(f"PR creation failed: {queue_code_or_error}")
                error_message, status_code = self._extract_error_details(queue_code_or_error)
                message = self._format_error_message_for_context(
                    context=context,
                    base_message="❌ PR creation failed. Please try again or contact support if the issue persists.",
                    error_message=error_message,
                    status_code=status_code
                )
                await self._post_message(channel_id, thread_ts, message)

        # Clear pending deployment message
        await placement_cache.clear_pending_deployment_message(conversation_id)

    async def _handle_create_pr_after_preview_background(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        queue_item_id: int,
        queue_code: str,
        ticket_service: TicketService,
        deployment_handler: DeploymentHandler
    ):
        """Background version: Create PR using existing queue item from preview."""
        logger.info(f"Creating PR after preview (background), reusing queue_item_id={queue_item_id}, queue_code={queue_code}")

        # Build context for deployment handler using provided services
        context = await self._build_deployment_context_with_services(
            conversation_id, channel_id, thread_ts, message_ts,
            ticket_service=ticket_service
        )

        if context:
            # Use deployment handler
            success, pr_url, queue_code_or_error = await deployment_handler.handle_create_pr_after_preview(
                context=context,
                queue_item_id=queue_item_id,
                queue_code=queue_code
            )

            if success:
                if pr_url:
                    pr_number = pr_url.rstrip('/').split('/')[-1] if pr_url else ""
                    await self._update_button_message_simple(
                        channel_id=channel_id,
                        message_ts=message_ts,
                        text=f"✅ *Pull Request Created!*\n\n📋 Queue Item: `{queue_code_or_error}`\n🔗 <{pr_url}|PR #{pr_number}>"
                    )
                else:
                    await self._update_button_message_simple(
                        channel_id=channel_id,
                        message_ts=message_ts,
                        text=f"⚠️ *Added to queue but no PR created*\n\n📋 Queue Item: `{queue_code_or_error}`"
                    )
            else:
                logger.error(f"PR creation failed (background): {queue_code_or_error}")
                error_message, status_code = self._extract_error_details(queue_code_or_error)
                message = self._format_error_message_for_context(
                    context=context,
                    base_message="❌ PR creation failed. Please try again or contact support if the issue persists.",
                    error_message=error_message,
                    status_code=status_code
                )
                await self._post_message(channel_id, thread_ts, message)

        # Clear pending deployment message
        await placement_cache.clear_pending_deployment_message(conversation_id)

    async def _handle_create_pr_fresh_background(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: List[Dict[str, Any]],
        ticket_service: TicketService,
        db: AsyncSession
    ):
        """Background version: Create PR from scratch (no preview done first)."""
        slack_thread_id = f"{channel_id}_{thread_ts}"
        ticket = await ticket_service.get_ticket_by_source_ref(
            source="slack",
            source_ref_id=slack_thread_id
        )

        if not ticket:
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Ticket not found. Cannot proceed with PR creation."
            )
            return

        config = {"configurable": {"thread_id": conversation_id}}
        state = await (await self.chat_service._get_graph()).aget_state(config)

        if not state or not state.values:
            logger.error(f"[CREATE_PR_FRESH_BG] State not found for thread_id={conversation_id}")
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Conversation state not found. Cannot proceed with PR creation."
            )
            return

        state_values = state.values
        turn_resource = state_values.get("turn_resource") or state_values.get("state_hint", {}).get("running_resource")
        # After confirmation, params move to confirmed_parameters and collected_parameters is cleared
        # So we need to check confirmed_parameters first, then fall back to collected_parameters
        collected_parameters = state_values.get("confirmed_parameters") or state_values.get("collected_parameters", {})
        collected_placement_parameters = state_values.get("collected_placement_parameters", {})

        if not turn_resource:
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Resource type not found in conversation state."
            )
            return

        # For v2 text-placement resources (e.g., S3), derive case_type_ref_code from cases
        # since placement_param_validator (which normally sets it) is bypassed
        if is_v2_text_placement_resource(turn_resource) and "case_type_ref_code" not in collected_placement_parameters:
            cases = state_values.get("cases", [])
            if cases:
                collected_placement_parameters["case_type_ref_code"] = cases[0]
                logger.info(f"[CREATE_PR_FRESH_BG] Derived case_type_ref_code='{cases[0]}' from cases for v2 resource")

        # Validate required placement params before proceeding
        if is_v2_text_placement_resource(turn_resource):
            missing_fields = validate_placement_params(collected_placement_parameters)
            if missing_fields:
                await self._post_message(
                    channel_id,
                    thread_ts,
                    f"Cannot create PR: missing required parameters: {', '.join(missing_fields)}. "
                    f"Please provide these values and try again."
                )
                return

        logger.info(f"Creating infrastructure for resource type: {turn_resource}")

        # Create fresh services with the background session
        queue_service = TransactionQueueService(db)
        pr_workflow_service = ScriptPRWorkflowService(db)
        infrastructure_service = InfrastructureCreationService(db)

        case_type = collected_placement_parameters.get("case_type_ref_code", "")

        # Database user management cases that skip infrastructure_mst creation (matching frontend)
        database_user_management_cases = [
            "user_management",
            "mysql_user_management",
            "postgresql_user_management",
            "database_user_management"
        ]
        database_creation_cases = [
            "create_database",
            "database_creation"
        ]

        if case_type == "add_route" or turn_resource == "kong_gateway":
            # Kong routes go to kong_route_configs table, not infrastructure_mst
            kong_request = self.request_builder.build_infrastructure_request(
                turn_resource="kong_gateway",
                collected_parameters=collected_parameters,
                collected_placement_parameters=collected_placement_parameters
            )
            logger.info(f"Creating Kong route config: {kong_request}")
            kong_response = await infrastructure_service.create_resource(
                tenant_code=self.tenant_code,
                request=kong_request,
                user_email=ticket.user_mst_code
            )
            infrastructure_code = kong_response.code
            logger.info(f"Created Kong route config: {infrastructure_code}")
        elif case_type in database_user_management_cases or "database_user" in turn_resource:
            # Database user management skips infrastructure_mst creation (matching frontend behavior)
            # Goes directly to transaction queue - file locator handles multiple files per server
            infrastructure_code = None
            logger.info(f"Skipping infrastructure_mst creation for database user management: case_type={case_type}, turn_resource={turn_resource}")
        elif case_type in database_creation_cases or turn_resource == "database_infrastructuretype_ref":
            # Database creation skips infrastructure_mst creation (matching deployment_handler behavior)
            infrastructure_code = None
            logger.info(f"Skipping infrastructure_mst creation for database creation: case_type={case_type}, turn_resource={turn_resource}")
        else:
            infrastructure_request = self.request_builder.build_infrastructure_request(
                turn_resource=turn_resource,
                collected_parameters=collected_parameters,
                collected_placement_parameters=collected_placement_parameters
            )

            logger.info(f"Creating infrastructure record: {infrastructure_request}")
            infrastructure_response = await infrastructure_service.create_resource(
                tenant_code=self.tenant_code,
                request=infrastructure_request,
                user_email=ticket.user_mst_code
            )

            infrastructure_code = infrastructure_response.code
            logger.info(f"Created infrastructure: {infrastructure_code}")

        config_snapshot = self.request_builder.build_config_snapshot(
            turn_resource=turn_resource,
            collected_parameters=collected_parameters,
            collected_placement_parameters=collected_placement_parameters
        )

        # DEBUG: Log product_name before adding to queue
        logger.info(f"[DB_USER_MGMT_DEBUG] config_snapshot built - product_name='{config_snapshot.get('product_name')}', case_type='{case_type}', turn_resource='{turn_resource}'")

        case_ref_code = collected_placement_parameters.get("case_type_ref_code")

        if case_type == "add_route" or turn_resource == "kong_gateway":
            # Kong rows are keyed on the kong HOST service's config
            # (SERVICE_CONFIG + add_route — Ajmal's KongGatewayTab pattern),
            # never on the KRC route record: that row (infrastructure_code)
            # stays as route inventory only, referenced from the snapshot.
            scope_code = await resolve_kong_host_config_code(
                db, self.tenant_code, config_snapshot
            )
            if scope_code:
                table_name = WorkflowSourceTableEnum.SERVICE_CONFIG
                infrastructure_code = scope_code
            else:
                # Host config unresolved — a pre-v2 kong service (host in a
                # different application, name without "kong", or never given a
                # service config for this env/region). Keep the legacy
                # KRC + INFRASTRUCTURE keying instead of refusing, so old
                # gateways keep saving routes; add_item_to_queue applies the
                # same fallback.
                table_name = WorkflowSourceTableEnum.INFRASTRUCTURE
                logger.info(
                    "kong add_route: no host config resolved — keeping legacy "
                    "keying %s/INFRASTRUCTURE", infrastructure_code,
                )
        elif case_type in database_user_management_cases or "database_user" in turn_resource:
            # Database user management has no infrastructure record
            table_name = None
        elif case_type in database_creation_cases or turn_resource == "database_infrastructuretype_ref":
            # Database creation has no infrastructure record
            table_name = None
        else:
            table_name = WorkflowSourceTableEnum.INFRASTRUCTURE

        queue_item = await queue_service.add_item_to_queue(
            user_code=ticket.user_mst_code,
            tenant_code=self.tenant_code,
            transaction_code=infrastructure_code,
            table_name=table_name,
            config_snapshot=config_snapshot,
            case_ref_code=case_ref_code,
            ticket_code=ticket.code,
            queue_code=None
        )

        # Persist queue item before starting PR workflow to avoid rollback on later failures
        await db.commit()
        await db.refresh(queue_item)

        logger.info(f"Added to queue: {queue_item.code} (ID: {queue_item.id})")

        pr_result = await pr_workflow_service.create(
            user_code=ticket.user_mst_code,
            tenant_code=self.tenant_code,
            queue_ids=[queue_item.id],
            all_pending_queues=False
        )

        gitops_responses = pr_result.get("gitops_responses", {})

        pr_url = None
        for response in gitops_responses.values():
            if "pr" in response:
                pr_url = response["pr"].get("pr_url") or response["pr"].get("html_url")
                break

        if pr_url:
            await self._update_message_to_pr_created(
                channel_id=channel_id,
                message_ts=message_ts,
                pr_url=pr_url,
                queue_code=queue_item.code,
                original_blocks=original_blocks
            )
        else:
            await self._update_message_to_no_pr(
                channel_id=channel_id,
                message_ts=message_ts,
                queue_code=queue_item.code,
                original_blocks=original_blocks
            )

        await placement_cache.clear_pending_deployment_message(conversation_id)

    async def _handle_create_pr_fresh(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None
    ):
        """Create PR from scratch (no preview done first).

        The "Creating Pull Request..." status is already shown by _handle_create_pr_action
        via _update_message_to_processing, so we don't duplicate it here.

        Args:
            original_blocks: Original message blocks to preserve config summary
        """
        slack_thread_id = f"{channel_id}_{thread_ts}"
        ticket = await self.ticket_service.get_ticket_by_source_ref(
            source="slack",
            source_ref_id=slack_thread_id
        )

        if not ticket:
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Ticket not found. Cannot proceed with PR creation."
            )
            return

        # thread_id is just the conversation_id (ticket code)
        # Must match bot_service.process_message which uses request.conversation_id directly
        config = {"configurable": {"thread_id": conversation_id}}
        state = await (await self.chat_service._get_graph()).aget_state(config)

        if not state or not state.values:
            logger.error(f"[CREATE_PR_FRESH] State not found for thread_id={conversation_id}")
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Conversation state not found. Cannot proceed with PR creation."
            )
            return

        state_values = state.values

        # Get resource type and parameters from graph state
        turn_resource = state_values.get("turn_resource") or state_values.get("state_hint", {}).get("running_resource")
        # After confirmation, params move to confirmed_parameters and collected_parameters is cleared
        # So we need to check confirmed_parameters first, then fall back to collected_parameters
        collected_parameters = state_values.get("confirmed_parameters") or state_values.get("collected_parameters", {})
        collected_placement_parameters = state_values.get("collected_placement_parameters", {})

        if not turn_resource:
            await self._post_message(
                channel_id,
                thread_ts,
                "❌ Error: Resource type not found in conversation state."
            )
            return

        # For v2 text-placement resources (e.g., S3), derive case_type_ref_code from cases
        # since placement_param_validator (which normally sets it) is bypassed
        if is_v2_text_placement_resource(turn_resource) and "case_type_ref_code" not in collected_placement_parameters:
            cases = state_values.get("cases", [])
            if cases:
                collected_placement_parameters["case_type_ref_code"] = cases[0]
                logger.info(f"[CREATE_PR_FRESH] Derived case_type_ref_code='{cases[0]}' from cases for v2 resource")

        # Validate required placement params before proceeding
        if is_v2_text_placement_resource(turn_resource):
            missing_fields = validate_placement_params(collected_placement_parameters)
            if missing_fields:
                await self._post_message(
                    channel_id,
                    thread_ts,
                    f"Cannot create PR: missing required parameters: {', '.join(missing_fields)}. "
                    f"Please provide these values and try again."
                )
                return

        logger.info(f"Creating infrastructure for resource type: {turn_resource}")
        logger.info(f"Collected parameters: {collected_parameters}")
        logger.info(f"Placement parameters: {collected_placement_parameters}")

        # Get case type to determine the flow
        case_type = collected_placement_parameters.get("case_type_ref_code", "")

        # Database user management cases that skip infrastructure_mst creation (matching frontend)
        database_user_management_cases = [
            "user_management",
            "mysql_user_management",
            "postgresql_user_management",
            "database_user_management"
        ]
        database_creation_cases = [
            "create_database",
            "database_creation"
        ]

        # Kong Gateway routes go to kong_route_configs, not infrastructure_mst
        if case_type == "add_route" or turn_resource == "kong_gateway":
            kong_request = self.request_builder.build_infrastructure_request(
                turn_resource="kong_gateway",
                collected_parameters=collected_parameters,
                collected_placement_parameters=collected_placement_parameters
            )
            logger.info(f"Creating Kong route config: {kong_request}")
            kong_response = await self.infrastructure_service.create_resource(
                tenant_code=self.tenant_code,
                request=kong_request,
                user_email=ticket.user_mst_code
            )
            infrastructure_code = kong_response.code
            logger.info(f"Created Kong route config: {infrastructure_code}")
        elif case_type in database_user_management_cases or "database_user" in turn_resource:
            # Database user management skips infrastructure_mst creation (matching frontend behavior)
            # Goes directly to transaction queue - file locator handles multiple files per server
            infrastructure_code = None
            logger.info(f"Skipping infrastructure_mst creation for database user management: case_type={case_type}, turn_resource={turn_resource}")
        elif case_type in database_creation_cases or turn_resource == "database_infrastructuretype_ref":
            # Database creation skips infrastructure_mst creation (matching deployment_handler behavior)
            infrastructure_code = None
            logger.info(f"Skipping infrastructure_mst creation for database creation: case_type={case_type}, turn_resource={turn_resource}")
        else:
            # Step 1: Build InfrastructureCreateRequest using shared builder
            infrastructure_request = self.request_builder.build_infrastructure_request(
                turn_resource=turn_resource,
                collected_parameters=collected_parameters,
                collected_placement_parameters=collected_placement_parameters
            )

            # Step 2: Create infrastructure_mst record
            logger.info(f"Creating infrastructure record: {infrastructure_request}")
            infrastructure_response = await self.infrastructure_service.create_resource(
                tenant_code=self.tenant_code,
                request=infrastructure_request,
                user_email=ticket.user_mst_code
            )

            infrastructure_code = infrastructure_response.code
            logger.info(f"Created infrastructure: {infrastructure_code}")

        # Step 3: Build config_snapshot for queue using shared builder
        config_snapshot = self.request_builder.build_config_snapshot(
            turn_resource=turn_resource,
            collected_parameters=collected_parameters,
            collected_placement_parameters=collected_placement_parameters
        )

        # Step 4: Add to transaction queue
        # case_ref_code must be a valid case_ref.code (e.g., "create_bucket", "create_queue", "add_route")
        case_ref_code = collected_placement_parameters.get("case_type_ref_code")

        # Determine table_name based on case type
        if case_type == "add_route" or turn_resource == "kong_gateway":
            # Same keying as the first flow above: anchor on the kong HOST
            # service's config; the KRC row stays as route inventory only.
            scope_code = await resolve_kong_host_config_code(
                self.db, self.tenant_code, config_snapshot
            )
            if scope_code:
                table_name = WorkflowSourceTableEnum.SERVICE_CONFIG
                infrastructure_code = scope_code
            else:
                # Host config unresolved — a pre-v2 kong service (host in a
                # different application, name without "kong", or never given a
                # service config for this env/region). Keep the legacy
                # KRC + INFRASTRUCTURE keying instead of refusing, so old
                # gateways keep saving routes; add_item_to_queue applies the
                # same fallback.
                table_name = WorkflowSourceTableEnum.INFRASTRUCTURE
                logger.info(
                    "kong add_route: no host config resolved — keeping legacy "
                    "keying %s/INFRASTRUCTURE", infrastructure_code,
                )
        elif case_type in database_user_management_cases or "database_user" in turn_resource:
            # Database user management has no infrastructure record
            table_name = None
        elif case_type in database_creation_cases or turn_resource == "database_infrastructuretype_ref":
            # Database creation has no infrastructure record
            table_name = None
        else:
            table_name = WorkflowSourceTableEnum.INFRASTRUCTURE

        queue_item = await self.queue_service.add_item_to_queue(
            user_code=ticket.user_mst_code,
            tenant_code=self.tenant_code,
            transaction_code=infrastructure_code,
            table_name=table_name,
            config_snapshot=config_snapshot,
            case_ref_code=case_ref_code,
            ticket_code=ticket.code,
            queue_code=None
        )

        # Persist queue item before starting PR workflow to avoid rollback on later failures
        await self.db.commit()
        await self.db.refresh(queue_item)

        logger.info(f"Added to queue: {queue_item.code} (ID: {queue_item.id})")

        # Step 5: Create PR
        pr_result = await self.pr_workflow_service.create(
            user_code=ticket.user_mst_code,
            tenant_code=self.tenant_code,
            queue_ids=[queue_item.id],
            all_pending_queues=False
        )

        gitops_responses = pr_result.get("gitops_responses", {})

        pr_url = None
        for response in gitops_responses.values():
            if "pr" in response:
                # The PR result uses "pr_url" key, not "html_url"
                pr_url = response["pr"].get("pr_url") or response["pr"].get("html_url")
                break

        if pr_url:
            # Update message in place to show success with PR link
            await self._update_message_to_pr_created(
                channel_id=channel_id,
                message_ts=message_ts,
                pr_url=pr_url,
                queue_code=queue_item.code,
                original_blocks=original_blocks
            )
        else:
            # Update message in place to show warning
            await self._update_message_to_no_pr(
                channel_id=channel_id,
                message_ts=message_ts,
                queue_code=queue_item.code,
                original_blocks=original_blocks
            )

        # Clear pending deployment message since PR was handled
        await placement_cache.clear_pending_deployment_message(conversation_id)

    async def _update_button_message_simple(
        self,
        channel_id: str,
        message_ts: str,
        text: str
    ):
        """Simple message update - replaces entire message with just text.

        Used for the post-preview button message which doesn't have config to preserve.
        """
        try:
            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text}
                }],
                text=text
            )
        except Exception as e:
            logger.warning(f"Could not update button message: {e}")

    async def _update_message_to_creating_pr(
        self,
        channel_id: str,
        message_ts: str
    ):
        """Update the message to show PR creation in progress (keeps config summary)."""
        try:
            # Fetch original message to preserve configuration summary
            result = await self.slack_client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                limit=1,
                inclusive=True
            )

            original_blocks = []
            if result.get("messages"):
                original_blocks = result["messages"][0].get("blocks", [])

            # Keep all blocks except the actions block, add status
            new_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "⏳ *Creating Pull Request...*"
                }
            })

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=new_blocks,
                text="Creating Pull Request..."
            )
        except Exception as e:
            logger.warning(f"Could not update message to creating PR: {e}")

    async def _update_message_to_pr_created(
        self,
        channel_id: str,
        message_ts: str,
        pr_url: str,
        queue_code: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None
    ):
        """Update the message to show PR created successfully (keeps config summary)."""
        try:
            # Use provided blocks (preserves config) or fetch if not available
            if not original_blocks:
                result = await self.slack_client.conversations_history(
                    channel=channel_id,
                    latest=message_ts,
                    limit=1,
                    inclusive=True
                )
                original_blocks = result.get("messages", [{}])[0].get("blocks", [])

            # Keep all blocks except actions block and context blocks (status messages)
            new_blocks = [b for b in original_blocks if b.get("type") != "actions" and b.get("type") != "context"]

            # Extract PR number from URL (e.g., https://github.com/org/repo/pull/5213 -> 5213)
            pr_number = pr_url.rstrip('/').split('/')[-1] if pr_url else ""

            # Add success message
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Pull Request Created!*\n\n📋 Queue Item: `{queue_code}`\n🔗 <{pr_url}|PR #{pr_number}>"
                }
            })

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=new_blocks,
                text=f"Pull Request Created: {pr_url}"
            )
        except Exception as e:
            logger.warning(f"Could not update message to PR created: {e}")

    async def _update_message_to_no_pr(
        self,
        channel_id: str,
        message_ts: str,
        queue_code: str,
        original_blocks: Optional[List[Dict[str, Any]]] = None
    ):
        """Update the message to show queue added but no PR (keeps config summary)."""
        try:
            # Use provided blocks (preserves config) or fetch if not available
            if not original_blocks:
                result = await self.slack_client.conversations_history(
                    channel=channel_id,
                    latest=message_ts,
                    limit=1,
                    inclusive=True
                )
                original_blocks = result.get("messages", [{}])[0].get("blocks", [])

            # Keep all blocks except actions block and context blocks (status messages)
            new_blocks = [b for b in original_blocks if b.get("type") != "actions" and b.get("type") != "context"]

            # Add warning message
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *Added to queue but no PR created*\n\n📋 Queue Item: `{queue_code}`"
                }
            })

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=new_blocks,
                text=f"Added to queue: {queue_code}"
            )
        except Exception as e:
            logger.warning(f"Could not update message to no PR: {e}")

    async def _handle_cancel_action(
        self,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: str,
        message_ts: str
    ):
        """Handle Cancel button click."""
        try:
            # Get conversation_id from the action value
            action = payload["actions"][0]
            conversation_id = action.get("value")

            # Update the message in place to show cancelled
            await self._update_message_to_cancelled(channel_id, message_ts)

            # Clear pending deployment message since it was cancelled
            if conversation_id:
                await placement_cache.clear_pending_deployment_message(conversation_id)

        except Exception as e:
            logger.error(f"Error cancelling: {str(e)}", exc_info=True)

    async def _update_message_to_cancelled(
        self,
        channel_id: str,
        message_ts: str
    ):
        """Update the message to show cancelled status (keeps config summary)."""
        try:
            # Fetch original message to preserve configuration summary
            result = await self.slack_client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                limit=1,
                inclusive=True
            )

            original_blocks = []
            if result.get("messages"):
                original_blocks = result["messages"][0].get("blocks", [])

            # Keep all blocks except actions
            new_blocks = [b for b in original_blocks if b.get("type") != "actions"]

            # Add cancelled message
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "❌ *Deployment cancelled*"
                }
            })

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=new_blocks,
                text="Deployment cancelled"
            )
        except Exception as e:
            logger.warning(f"Could not update message to cancelled: {e}")

    async def _respond_to_action(self, payload: Dict[str, Any], text: str):
        """Send ephemeral response to interaction."""
        response_url = payload.get("response_url")
        if response_url:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    response_url,
                    json={
                        "text": text,
                        "replace_original": False,
                        "response_type": "ephemeral"
                    }
                )

    async def _post_message(self, channel_id: str, thread_ts: str, text: str):
        """Post a message to Slack."""
        await self.slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text
        )

    async def _delete_pending_reminder_message(self, conversation_id: str) -> None:
        """Delete any pending 'please complete selection' reminder message.

        Args:
            conversation_id: Ticket code
        """
        try:
            reminder_msg = await placement_cache.get_pending_reminder_message(conversation_id)
            if not reminder_msg:
                return

            channel_id = reminder_msg.get("channel_id")
            message_ts = reminder_msg.get("message_ts")

            if channel_id and message_ts:
                await self.slack_client.chat_delete(
                    channel=channel_id,
                    ts=message_ts
                )
                logger.info(f"Deleted reminder message for {conversation_id}")

            await placement_cache.clear_pending_reminder_message(conversation_id)

        except Exception as e:
            logger.warning(f"Could not delete reminder message: {e}")
            await placement_cache.clear_pending_reminder_message(conversation_id)

    async def _delete_pending_wait_messages(self, conversation_id: str) -> None:
        """Delete any pending 'please wait' messages when processing completes.

        When the lock is released, we delete any wait messages that were shown
        to users who sent messages while processing was in progress.

        Args:
            conversation_id: Ticket code
        """
        try:
            wait_msg = await placement_cache.get_pending_wait_message(conversation_id)
            if not wait_msg:
                return

            channel_id = wait_msg.get("channel_id")
            message_ts = wait_msg.get("message_ts")

            if channel_id and message_ts:
                await self.slack_client.chat_delete(
                    channel=channel_id,
                    ts=message_ts
                )
                logger.info(f"Deleted wait message for {conversation_id}")

            await placement_cache.clear_pending_wait_message(conversation_id)

        except Exception as e:
            logger.warning(f"Could not delete wait message: {e}")
            await placement_cache.clear_pending_wait_message(conversation_id)

    def _extract_conversation_id(self, action: Dict[str, Any], channel_id: str, thread_ts: str) -> Optional[str]:
        """Extract conversation_id from action value.

        Args:
            action: Slack action payload
            channel_id: Slack channel ID
            thread_ts: Thread timestamp

        Returns:
            Conversation ID (ticket code) or None if not found
        """
        try:
            # For buttons with value containing conversation_id
            raw_value = action.get("value", "")

            # Direct conversation_id in value (for preview, create_pr, cancel buttons)
            if raw_value and "|" not in raw_value and not raw_value.startswith("select_"):
                return raw_value

            # Format: "conversation_id|selected_value"
            if "|" in raw_value:
                return raw_value.split("|", 1)[0]

            # For dropdown selections
            if action.get("type") == "static_select":
                selected_value = action.get("selected_option", {}).get("value", "")
                if "|" in selected_value:
                    return selected_value.split("|", 1)[0]

            return None

        except Exception as e:
            logger.warning(f"Could not extract conversation_id: {e}")
            return None
