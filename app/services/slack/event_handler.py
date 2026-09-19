"""Slack Event Handler

Handles Slack message events and routes to appropriate services.
Uses placement_cache for state management and PlacementUIHelper for UI.

Flow:
1. Slack message → Call infraChat (creates ticket with slack_id & channel_id)
2. infraChat returns contract with remaining_placement_parameters
3. Store placement params contract in memory cache
4. Show placement parameters as buttons/dropdowns from contract options
5. User selects → Store in cache (NO infraChat call)
6. Repeat until all placement params collected in cache
7. When all collected → Call infraChat ONCE with all params
8. infraChat collects attribute parameters conversationally
9. When is_ready: true → Show summary + Deploy/Cancel buttons
10. Deploy → add-to-queue + create-pr
"""
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient

from app.services.slack.base_handler import BaseSlackHandler
from app.services.bot_service import ChatService
from app.services.ticket_service import TicketService
from app.services.slack import placement_cache
from app.services.slack.shared import PlacementUIHelper
from app.services.slack.shared.infrastructure_request_builder import is_v2_text_placement_resource
from app.schemas.infra_chat_schemas import (
    InfraChatRequestSchema,
    UpdatePlacementParamsRequestSchema
)
import logging

logger = logging.getLogger(__name__)


class SlackEventHandler(BaseSlackHandler):
    """Handles Slack events using infraChat service (contract-driven)."""

    def __init__(self, db: AsyncSession, slack_client: AsyncWebClient, tenant_code: str):
        super().__init__(db, slack_client, tenant_code)
        self.chat_service = ChatService()
        self.ticket_service = TicketService(db)
        self.placement_ui = PlacementUIHelper(slack_client, self.block_builder)

    async def handle_message(self, event: Dict[str, Any], say):
        """Handle Slack message event."""
        processing_msg_ts = None
        channel_id = None
        thread_ts = None

        try:
            if event.get("subtype"):
                return

            channel_id = event["channel"]
            thread_ts = event.get("thread_ts", event["ts"])
            slack_user_id = event["user"]
            user_message = event.get("text", "")

            logger.info(f"Slack message from {slack_user_id}: {user_message[:100]}")

            # Show processing indicator immediately
            processing_msg_ts = await self.placement_ui.show_processing_indicator(channel_id, thread_ts)

            # Map Slack user to DevLift user
            user = await self.get_or_map_user(slack_user_id, say, channel_id, thread_ts)
            if not user:
                if processing_msg_ts:
                    await self.placement_ui.delete_processing_indicator(channel_id, processing_msg_ts)
                return

            # Create or get ticket (ticket creation with Slack details)
            ticket_code = await self._create_or_get_ticket(
                user=user,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_message=user_message
            )

            # Check if conversation is locked (another operation in progress)
            if await placement_cache.is_conversation_locked(ticket_code):
                lock_info = await placement_cache.get_conversation_lock_info(ticket_code)
                operation = lock_info.get("operation", "processing") if lock_info else "processing"
                logger.info(f"Ignoring message - conversation {ticket_code} is locked for: {operation}")
                # Update processing indicator to inform user their message is ignored
                if processing_msg_ts:
                    await self.slack_client.chat_update(
                        channel=channel_id,
                        ts=processing_msg_ts,
                        text=f"Please wait, your previous request is still being processed.",
                        blocks=[{
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"⏳ *Please wait, your previous request is still being processed.*"
                            }
                        }]
                    )
                    # Store this wait message so it can be deleted when lock is released
                    await placement_cache.store_pending_wait_message(
                        conversation_id=ticket_code,
                        channel_id=channel_id,
                        message_ts=processing_msg_ts
                    )
                return

            # Check if there's a pending placement parameter selection
            if await self._check_pending_placement_selection(ticket_code, thread_ts, say):
                if processing_msg_ts:
                    await self.placement_ui.delete_processing_indicator(channel_id, processing_msg_ts)
                return  # User needs to complete selection first

            # Invalidate any pending deployment buttons for this conversation
            await self._invalidate_pending_deployment_buttons(ticket_code)

            # Use ticket code as conversation_id for infraChat
            await self._call_infra_chat_and_process(
                user, user_message, channel_id, thread_ts, ticket_code, say, processing_msg_ts
            )

        except Exception as e:
            logger.error(f"Error handling message: {str(e)}", exc_info=True)
            if processing_msg_ts and channel_id:
                await self.placement_ui.delete_processing_indicator(channel_id, processing_msg_ts)
            await say(
                blocks=self.block_builder.build_error_message("Something went wrong. Please try again."),
                thread_ts=event.get("thread_ts", event.get("ts"))
            )

    async def _create_or_get_ticket(
        self,
        user,
        channel_id: str,
        thread_ts: str,
        user_message: str
    ) -> str:
        """Create or get existing ticket for this Slack thread.

        First message creates ticket, subsequent messages retrieve existing ticket.

        Args:
            user: User object
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            user_message: User's message (for ticket name)

        Returns:
            Ticket code to use as conversation_id
        """
        try:
            # Check if ticket already exists for this thread
            # Use source_ref_id to store slack thread identifier
            slack_thread_id = f"{channel_id}_{thread_ts}"

            existing_ticket = await self.ticket_service.get_ticket_by_source_ref(
                source="slack",
                source_ref_id=slack_thread_id
            )

            if existing_ticket:
                logger.info(f"Found existing ticket {existing_ticket.code} for thread {slack_thread_id}")
                return existing_ticket.code

            # Create new ticket
            ticket_name = user_message[:50] if len(user_message) > 50 else user_message

            ticket = await self.ticket_service.generate_ticket_number(
                tenants_mst_code=self.tenant_code,
                user_mst_code=user.code,
                name=ticket_name,
                description=f"Slack conversation from channel {channel_id}",
                source="slack",
                source_ref_id=slack_thread_id
            )

            # Commit the ticket to the database so subsequent messages in the same
            # thread can find it (they use a different session)
            await self.db.commit()

            logger.info(f"Created ticket {ticket.code} ({ticket.ticket_number}) for Slack thread {slack_thread_id}")
            return ticket.code

        except Exception as e:
            logger.error(f"Error creating/getting ticket: {str(e)}", exc_info=True)
            # Fallback to old method if ticket creation fails
            return f"slack_{channel_id}_{thread_ts}"

    async def _call_infra_chat_and_process(
        self, user, user_message, channel_id, thread_ts, conversation_id, say, processing_msg_ts=None
    ):
        """Call infraChat and process contract response."""
        try:
            # Acquire lock to prevent concurrent processing
            if not await placement_cache.acquire_conversation_lock(conversation_id, "processing your request"):
                logger.warning(f"Could not acquire lock for {conversation_id}")
                if processing_msg_ts:
                    await self.placement_ui.update_processing_indicator(
                        channel_id, processing_msg_ts,
                        "⏳ Please wait, I'm still processing. Try again in a moment."
                    )
                return

            logger.info(f"Calling infraChat - user: {user.code}, conv: {conversation_id}")

            request = InfraChatRequestSchema(
                message=user_message,
                conversation_id=conversation_id,
                tenants_mst_code=self.tenant_code,
                user_mst_code=user.code
            )

            result = await self.chat_service.process_message(request)
            response_text = self.chat_service.get_response_text(result)
            is_ready = result.get("is_ready", False)
            remaining_placement = result.get("remaining_placement_parameters", {})
            collected_parameters = result.get("collected_parameters", {})
            collected_placement_parameters = result.get("collected_placement_parameters", {})
            turn_resource = result.get("turn_resource", "")

            logger.info(f"Contract - is_ready: {is_ready}, remaining_placement: {len(remaining_placement)}, collected_parameters: {collected_parameters}")

            # Delete processing indicator before showing response
            if processing_msg_ts:
                await self.placement_ui.delete_processing_indicator(channel_id, processing_msg_ts)
                processing_msg_ts = None

            # For v2 text-placement resources (e.g., S3), skip button-based placement UI.
            # Placement params are collected from user text via MCP tools, not buttons.
            if remaining_placement and not is_v2_text_placement_resource(turn_resource):
                # V1 flow: Store placement params contract and show button UI
                await placement_cache.store_placement_state(
                    conversation_id=conversation_id,
                    remaining_params=remaining_placement,
                    user_mst_code=user.code,
                    tenant_code=self.tenant_code
                )

                # Show UI for first placement parameter using shared helper
                await self.placement_ui.show_next_param_ui(
                    conversation_id, channel_id, thread_ts, response_text, say
                )

                # Check if all params were auto-selected
                if await placement_cache.is_placement_complete(conversation_id):
                    await self._finalize_placement_params(conversation_id, channel_id, thread_ts, say)
            elif is_ready:
                # Ready to deploy - use shared helper
                await self.placement_ui.show_deployment_confirmation(
                    conversation_id, channel_id, thread_ts, collected_parameters, response_text, say,
                    collected_placement_parameters=collected_placement_parameters
                )
            else:
                # Conversational attribute collection (both v1 and v2)
                if response_text:
                    formatted_text = self.format_response_for_slack(response_text)
                    await say(text=formatted_text, thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"Error in infraChat: {str(e)}", exc_info=True)
            # Delete processing indicator if still present
            if processing_msg_ts:
                await self.placement_ui.delete_processing_indicator(channel_id, processing_msg_ts)
            await say(text="Something went wrong. Please try again.", thread_ts=thread_ts)
        finally:
            # Delete any pending "please wait" messages before releasing lock
            await self._delete_pending_wait_messages(conversation_id)
            # Always release the lock
            await placement_cache.release_conversation_lock(conversation_id)

    async def _finalize_placement_params(self, conversation_id: str, channel_id: str, thread_ts: str, say):
        """Send all collected placement params to infraChat at once.

        Called when all placement params have been collected in cache.
        """
        try:
            state = await placement_cache.get_placement_state(conversation_id)
            if not state:
                logger.error(f"No cache state found for {conversation_id}")
                await say(text="Error: Session expired. Please start over.", thread_ts=thread_ts)
                return

            collected_params = state["collected"]
            user_mst_code = state["user_mst_code"]
            tenant_code = state["tenant_code"]

            logger.info(f"Finalizing placement params for {conversation_id}: {collected_params}")

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

            # Call infraChat to continue (attribute collection or is_ready)
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
                await self.placement_ui.show_deployment_confirmation(
                    conversation_id, channel_id, thread_ts, collected_parameters, response_text, say
                )
            else:
                # Conversational attribute collection
                if response_text:
                    formatted_text = self.format_response_for_slack(response_text)
                    await say(text=formatted_text, thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"Error finalizing placement params: {str(e)}", exc_info=True)
            await say(text="Something went wrong. Please try again.", thread_ts=thread_ts)

    async def _invalidate_pending_deployment_buttons(self, conversation_id: str) -> None:
        """Invalidate (remove/update) any pending deployment buttons when user sends new input.

        When a user sends a new message while a deploy button is shown, the previous
        configuration may no longer be valid. This updates the old message to remove
        the buttons and show a "Configuration outdated" message.

        Args:
            conversation_id: Ticket code (conversation identifier)
        """
        try:
            pending_msg = await placement_cache.get_pending_deployment_message(conversation_id)

            if not pending_msg:
                return

            channel_id = pending_msg.get("channel_id")
            message_ts = pending_msg.get("message_ts")

            if not channel_id or not message_ts:
                return

            logger.info(f"Invalidating deployment buttons for {conversation_id} at {message_ts}")

            # Update the message to show it's been updated
            invalidated_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🔄 *New configuration updated*"
                    }
                }
            ]

            await self.slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=invalidated_blocks,
                text="New configuration updated"
            )

            # Clear the pending message from cache
            await placement_cache.clear_pending_deployment_message(conversation_id)

            logger.info(f"Successfully invalidated deployment buttons for {conversation_id}")

        except Exception as e:
            logger.warning(f"Failed to invalidate deployment buttons: {str(e)}")
            # Non-blocking - don't fail the main flow if button invalidation fails
            await placement_cache.clear_pending_deployment_message(conversation_id)

    async def _check_pending_placement_selection(self, conversation_id: str, thread_ts: str, say) -> bool:
        """Check if there's a pending placement parameter selection and notify user.

        When a user sends a new message while placement parameter buttons are shown,
        remind them to complete the selection first.

        Args:
            conversation_id: Ticket code (conversation identifier)
            thread_ts: Thread timestamp
            say: Slack say function

        Returns:
            True if there's a pending selection (caller should stop processing), False otherwise
        """
        try:
            pending_msg = await placement_cache.get_pending_placement_message(conversation_id)

            if not pending_msg:
                return False

            # There's a pending placement selection - remind user to complete it
            logger.info(f"Pending placement selection for {conversation_id}, asking user to complete it")

            result = await say(
                text="Please complete the selection above before sending a new message.",
                thread_ts=thread_ts,
                blocks=[{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ *Please complete the selection above before sending a new message.*"
                    }
                }]
            )

            # Store this reminder message so we can delete it when user makes a selection
            if result and result.get("ts"):
                await placement_cache.store_pending_reminder_message(
                    conversation_id=conversation_id,
                    channel_id=pending_msg.get("channel_id"),
                    message_ts=result["ts"]
                )

            return True

        except Exception as e:
            logger.warning(f"Error checking pending placement selection: {str(e)}")
            return False

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
