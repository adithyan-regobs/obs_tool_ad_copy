import asyncio
import re
import logging
from typing import Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from app.core.config import settings

logger = logging.getLogger(__name__)


class SlackSocketModeHandler:
    """Handles Slack Socket Mode connection and event routing.

    Socket Mode uses WebSocket connection instead of HTTP webhooks,
    eliminating the need for public URLs or ngrok for local development.
    """

    def __init__(self):
        """Initialize Slack app with Socket Mode."""
        if not settings.slack_bot_token:
            raise ValueError("SLACK_BOT_TOKEN is required for Socket Mode")
        if not settings.slack_app_token:
            raise ValueError("SLACK_APP_TOKEN is required for Socket Mode")

        # Initialize Slack Bolt app with token and signing_secret for single-team setup
        # This uses AsyncSingleTeamAuthorization instead of OAuth/multi-team
        self.app = AsyncApp(
            token=settings.slack_bot_token,
            signing_secret=settings.slack_signing_secret,
        )
        self.client: AsyncWebClient = self.app.client

        # Socket Mode handler
        self.socket_handler = AsyncSocketModeHandler(
            self.app,
            settings.slack_app_token
        )

        # Register event listeners
        self._register_listeners()

        logger.info("Slack Socket Mode handler initialized")

    def _register_listeners(self):
        """Register all Slack event listeners."""

        # App mentions (@bot hello)
        @self.app.event("app_mention")
        async def handle_mention(event, say, ack):
            await ack()
            logger.info(f"App mentioned in channel {event['channel']}")

            # Route to event handler
            await self._route_to_event_handler(event, say)

        # Direct messages
        @self.app.event("message")
        async def handle_message(event, say, ack):
            await ack()

            # Ignore bot messages only
            if event.get("subtype") == "bot_message":
                return

            # Process both new messages and threaded replies
            channel_type = event.get("channel_type")
            is_thread_reply = event.get("thread_ts") and event.get("thread_ts") != event.get("ts")
            message_type = "thread reply" if is_thread_reply else "new message"
            logger.info(f"{message_type} received in {channel_type}: {event.get('text', '')[:50]}")

            # Route to event handler (handles both new messages and thread replies)
            await self._route_to_event_handler(event, say)

        # Slash commands (/infra)
        @self.app.command("/infra")
        async def handle_infra_command(ack, command, say):
            await ack()
            logger.info(f"/infra command received: {command['text']}")

            # Route to command handler
            await self._route_to_command_handler(command, say)

        # Interactive components (buttons, dropdowns)
        @self.app.action(re.compile(".*"))
        async def handle_action(ack, action, body):
            await ack()
            action_id = action.get("action_id", "unknown")
            logger.info(f"Interactive action received: {action_id}")

            # Route to interaction handler
            await self._route_to_interaction_handler(body)

        # Shortcuts
        @self.app.shortcut(re.compile(".*"))
        async def handle_shortcut(ack, shortcut, say):
            await ack()
            logger.info(f"Shortcut received: {shortcut.get('callback_id')}")
            # TODO: Handle shortcuts if needed

        # Error handler
        @self.app.error
        async def handle_error(error, body, logger):
            logger.error(f"Slack error: {error}")
            logger.error(f"Error body: {body}")

    async def start(self):
        """Start Socket Mode connection (non-blocking).

        This establishes a WebSocket connection to Slack and starts
        listening for events. Runs in background.
        """
        try:
            logger.info("Starting Slack Socket Mode connection...")
            await self.socket_handler.start_async()
            logger.info("Slack Socket Mode connected successfully")
        except Exception as e:
            logger.error(f"Failed to start Slack Socket Mode: {str(e)}")
            raise

    async def stop(self):
        """Stop Socket Mode connection gracefully."""
        try:
            logger.info("Stopping Slack Socket Mode connection...")
            await self.socket_handler.close_async()
            logger.info("Slack Socket Mode disconnected")
        except Exception as e:
            logger.error(f"Error stopping Slack Socket Mode: {str(e)}")

    def is_connected(self) -> bool:
        """Check if Socket Mode is currently connected."""
        # Socket Mode handler doesn't expose connection status directly,
        # but we can check if the handler exists
        return self.socket_handler is not None

    async def _route_to_event_handler(self, event: dict, say):
        """Route Slack event to SlackEventHandler.

        This is the bridge between Socket Mode and our event processing logic.
        Creates a new database session and event handler for each event.

        IMPORTANT: This is ONLY called from Slack - web UI requests go through
        the existing /api/v1/chat endpoint with JWT authentication and are unaffected.
        """
        from app.services.slack.event_handler import SlackEventHandler
        from app.services.slack.db_utils import managed_session

        # Get tenant code from environment configuration
        # In single-tenant deployment, this is set via SLACK_TENANT_CODE env var
        tenant_code = settings.slack_tenant_code

        if not tenant_code:
            logger.error("SLACK_TENANT_CODE not configured - cannot process Slack messages")
            await say(
                text="⚠️ Bot configuration error: SLACK_TENANT_CODE not set. "
                     "Please contact your administrator."
            )
            return

        try:
            async with managed_session() as db:
                handler = SlackEventHandler(
                    db=db,
                    slack_client=self.client,
                    tenant_code=tenant_code
                )
                await handler.handle_message(event, say)
        except Exception as e:
            logger.error(f"Error in event handler: {str(e)}", exc_info=True)
            await say(text="Sorry, something went wrong. Please try again.")

    async def _route_to_interaction_handler(self, payload: dict):
        """Route Slack interaction to SlackInteractionHandler.

        Handles button clicks, dropdown selections, and other interactive components.

        Args:
            payload: Slack interaction payload
        """
        from app.services.slack.interaction_handler import SlackInteractionHandler
        from app.services.slack.db_utils import managed_session

        # Get tenant code from environment
        tenant_code = settings.slack_tenant_code

        if not tenant_code:
            logger.error("SLACK_TENANT_CODE not configured - cannot process interactions")
            return

        try:
            async with managed_session() as db:
                handler = SlackInteractionHandler(
                    db=db,
                    slack_client=self.client,
                    tenant_code=tenant_code
                )
                await handler.handle_block_action(payload)
        except Exception as e:
            logger.error(f"Error in interaction handler: {str(e)}", exc_info=True)

    async def _route_to_command_handler(self, command: dict, say):
        """Route Slack slash command to SlackCommandHandler.

        Args:
            command: Slack command payload
            say: Slack say function
        """
        from app.services.slack.command_handler import SlackCommandHandler
        from app.services.slack.db_utils import managed_session

        # Get tenant code from environment
        tenant_code = settings.slack_tenant_code

        if not tenant_code:
            logger.error("SLACK_TENANT_CODE not configured - cannot process commands")
            await say(
                text="⚠️ Bot configuration error: SLACK_TENANT_CODE not set.",
                response_type="ephemeral"
            )
            return

        try:
            async with managed_session() as db:
                handler = SlackCommandHandler(
                    db=db,
                    slack_client=self.client,
                    tenant_code=tenant_code
                )
                await handler.handle_infra_command(command, say)
        except Exception as e:
            logger.error(f"Error in command handler: {str(e)}", exc_info=True)
            await say(
                text="❌ Error processing command. Please try again.",
                response_type="ephemeral"
            )
