"""Slack Command Handler

Handles slash commands like /infra deploy history, /infra chat history, /infra help.
"""
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient

from app.services.slack.block_builder import SlackBlockBuilder
from app.repository.chat_info_repository import ChatInfoRepository
import logging

logger = logging.getLogger(__name__)


class SlackCommandHandler:
    """Handles /infra slash commands."""

    def __init__(self, db: AsyncSession, slack_client: AsyncWebClient, tenant_code: str):
        """Initialize command handler.

        Args:
            db: Database session
            slack_client: Slack API client
            tenant_code: Tenant code
        """
        self.db = db
        self.slack_client = slack_client
        self.tenant_code = tenant_code
        self.block_builder = SlackBlockBuilder()
        self.chat_info_repo = ChatInfoRepository(db)

    async def handle_infra_command(self, command: Dict[str, Any], say):
        """Handle /infra command.

        Supported commands:
        - /infra help
        - /infra deploy history
        - /infra chat history

        Args:
            command: Slack command payload
            say: Slack say function
        """
        try:
            # Extract command text
            text = command.get("text", "").strip().lower()
            user_id = command["user_id"]
            channel_id = command["channel_id"]

            logger.info(f"/infra command from {user_id}: {text}")

            # Parse command
            if not text or text == "help":
                await self._handle_help(say)
            elif text == "deploy history" or text == "deployments":
                await self._handle_deploy_history(say, user_id)
            elif text == "chat history" or text == "history":
                await self._handle_chat_history(say, user_id)
            else:
                # Unknown command
                await say(
                    text=f"❓ Unknown command: `{text}`\n\nUse `/infra help` to see available commands.",
                    response_type="ephemeral"
                )

        except Exception as e:
            logger.error(f"Error handling /infra command: {str(e)}", exc_info=True)
            await say(
                text="❌ Something went wrong. Please try again.",
                response_type="ephemeral"
            )

    async def _handle_help(self, say):
        """Show help message.

        Args:
            say: Slack say function
        """
        blocks = self.block_builder.build_help_message()
        await say(
            blocks=blocks,
            text="Sage Help",
            response_type="ephemeral"
        )

    async def _handle_deploy_history(self, say, user_id: str):
        """Show deployment history.

        Args:
            say: Slack say function
            user_id: Slack user ID
        """
        # TODO: Fetch actual deployment history from database
        # For now, show placeholder

        deployments = []  # Placeholder - will fetch from infrastructure_mst table

        blocks = self.block_builder.build_deployment_history(deployments)
        await say(
            blocks=blocks,
            text="Deployment History",
            response_type="ephemeral"
        )

    async def _handle_chat_history(self, say, user_id: str):
        """Show chat history.

        Args:
            say: Slack say function
            user_id: Slack user ID
        """
        try:
            # Fetch recent chat sessions for this tenant from Slack
            from sqlalchemy import select, and_
            from app.db.models.chat_info_model import ChatInfoModel

            # Get last 10 Slack chat sessions
            stmt = select(ChatInfoModel).where(
                and_(
                    ChatInfoModel.tenants_mst_code == self.tenant_code,
                    ChatInfoModel.source == "slack",
                    ChatInfoModel.is_deleted == False
                )
            ).order_by(ChatInfoModel.created_at.desc()).limit(10)

            result = await self.db.execute(stmt)
            chat_sessions = result.scalars().all()

            if not chat_sessions:
                await say(
                    text="📋 *Chat History*\n\nNo chat history found.",
                    response_type="ephemeral"
                )
                return

            # Build history message
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📋 Chat History"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Your {len(chat_sessions)} most recent conversations:"
                    }
                }
            ]

            for session in chat_sessions:
                # Format timestamp
                created_at = session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "Unknown"

                # Build session description
                app_name = session.application.name if session.application else "N/A"
                env = session.environment_enum.value if session.environment_enum else "N/A"

                text = f"*{session.name or 'Chat Session'}*\n"
                text += f"App: {app_name} | Env: {env}\n"
                text += f"Created: {created_at}"

                # Add link to Slack thread if available
                if session.slack_channel_id and session.slack_thread_ts:
                    # Slack permalink format
                    workspace_id = "TODO"  # Would need to get from workspace mapping
                    # text += f"\n<https://slack.com/app_redirect?channel={session.slack_channel_id}&message_ts={session.slack_thread_ts}|View Thread>"

                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": text
                    }
                })

            blocks.append({
                "type": "divider"
            })

            await say(
                blocks=blocks,
                text="Chat History",
                response_type="ephemeral"
            )

        except Exception as e:
            logger.error(f"Error fetching chat history: {str(e)}", exc_info=True)
            await say(
                text="❌ Error fetching chat history. Please try again.",
                response_type="ephemeral"
            )
