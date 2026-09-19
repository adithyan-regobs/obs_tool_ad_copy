"""Base Slack Handler with shared utilities."""
import re
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient

from app.db.models.user_mst_model import UserMstModel
from app.services.slack.user_mapper import SlackUserMapper
from app.services.slack.block_builder import SlackBlockBuilder
import logging

logger = logging.getLogger(__name__)


class BaseSlackHandler:
    """Base class for Slack handlers with common utilities."""

    # Approval keywords that trigger deployment
    APPROVAL_KEYWORDS = {"approve", "deploy", "yes", "confirm"}

    def __init__(self, db: AsyncSession, slack_client: AsyncWebClient, tenant_code: str):
        """Initialize base handler with shared services.

        Args:
            db: Database session
            slack_client: Slack API client
            tenant_code: Tenant code
        """
        self.db = db
        self.slack_client = slack_client
        self.tenant_code = tenant_code

        # Shared services
        self.user_mapper = SlackUserMapper(db, slack_client)
        self.block_builder = SlackBlockBuilder()

    async def get_or_map_user(
        self,
        slack_user_id: str,
        say=None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None
    ) -> Optional[UserMstModel]:
        """Get DevLift user from Slack user ID, send onboarding message if not found.

        Args:
            slack_user_id: Slack user ID
            say: Optional Slack say function for error messages
            channel_id: Optional channel ID for error messages
            thread_ts: Optional thread timestamp for error messages

        Returns:
            UserMstModel if found, None otherwise
        """
        user = await self.user_mapper.map_slack_user_to_devlift_user(
            slack_user_id,
            self.tenant_code
        )

        if not user and say:
            email = await self.user_mapper.get_slack_user_email(slack_user_id)
            blocks = self.block_builder.build_onboarding_message(email or "unknown")

            if channel_id and thread_ts:
                await say(blocks=blocks, thread_ts=thread_ts)
            else:
                await say(blocks=blocks)

        return user

    def is_approval_keyword(self, message: str) -> bool:
        """Check if message contains approval keyword.

        Args:
            message: User message text

        Returns:
            True if message is an approval keyword
        """
        return message.lower().strip() in self.APPROVAL_KEYWORDS

    @staticmethod
    def format_response_for_slack(text: str) -> str:
        """Format LLM response text for Slack display.

        Converts custom formatting tags to Slack mrkdwn:
        - <&b>text</&b> → *text* (bold)
        - <&h>text → • text (bullet point header)

        Args:
            text: Raw response text from LLM

        Returns:
            Formatted text for Slack
        """
        if not text:
            return text

        # Convert <&b>...</&b> to Slack bold (*...*)
        formatted = re.sub(r'<&b>(.*?)</&b>', r'*\1*', text, flags=re.DOTALL)

        # Convert <&h> to bullet point (•)
        formatted = formatted.replace('<&h>', '• ')

        return formatted

    async def send_slack_message(
        self,
        channel_id: str,
        thread_ts: str,
        text: Optional[str] = None,
        blocks: Optional[list] = None
    ):
        """Send a message to Slack.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            text: Message text
            blocks: Block Kit blocks
        """
        await self.slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            blocks=blocks
        )

    async def update_slack_message(
        self,
        channel_id: str,
        message_ts: str,
        text: Optional[str] = None,
        blocks: Optional[list] = None
    ):
        """Update an existing Slack message.

        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp to update
            text: Message text
            blocks: Block Kit blocks
        """
        await self.slack_client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=text or "Updated",
            blocks=blocks
        )
