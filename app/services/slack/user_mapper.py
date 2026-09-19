from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from app.db.models.user_mst_model import UserMstModel
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class SlackUserMapper:
    """Maps Slack users to DevLift users via email lookup.

    Since the application runs on client infrastructure (single-tenant per deployment),
    tenant_code is determined from environment configuration, not from Slack workspace mapping.
    """

    def __init__(self, db: AsyncSession, slack_client: Optional[AsyncWebClient] = None):
        self.db = db
        self.slack_client = slack_client or AsyncWebClient(token=settings.slack_bot_token)

    async def get_slack_user_email(self, slack_user_id: str) -> Optional[str]:
        """Get Slack user's email address via Slack API.

        Args:
            slack_user_id: Slack user ID (e.g., "U1234567890")

        Returns:
            User's email address or None if not found
        """
        try:
            response = await self.slack_client.users_info(user=slack_user_id)

            if response["ok"]:
                user_profile = response["user"]["profile"]
                email = user_profile.get("email")

                if email:
                    logger.info(f"Retrieved email for Slack user {slack_user_id}: {email}")
                    return email
                else:
                    logger.warning(f"No email found in profile for Slack user {slack_user_id}")
                    return None
            else:
                logger.error(f"Slack API error for user {slack_user_id}: {response.get('error')}")
                return None

        except SlackApiError as e:
            logger.error(f"Failed to get Slack user info for {slack_user_id}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting Slack user email: {str(e)}")
            return None

    async def get_slack_user_id_by_email(self, email: str) -> Optional[str]:
        """Look up a Slack user ID from an email address via users.lookupByEmail."""
        try:
            response = await self.slack_client.users_lookupByEmail(email=email)
            if response["ok"]:
                return response["user"]["id"]
            logger.warning(f"users.lookupByEmail failed for {email}: {response.get('error')}")
            return None
        except SlackApiError as e:
            logger.warning(f"Slack API error looking up {email}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error looking up Slack user by email {email}: {e}")
            return None

    async def map_slack_user_to_devlift_user(
        self,
        slack_user_id: str,
        tenant_code: str
    ) -> Optional[UserMstModel]:
        """Map Slack user to DevLift user by email and tenant.

        Args:
            slack_user_id: Slack user ID
            tenant_code: Tenant code from environment configuration

        Returns:
            UserMstModel if found, None otherwise
        """
        # Get Slack user's email
        email = await self.get_slack_user_email(slack_user_id)

        if not email:
            logger.warning(f"Cannot map Slack user {slack_user_id}: no email found")
            return None

        # Find DevLift user by email and tenant
        stmt = select(UserMstModel).where(
            UserMstModel.email_id == email,
            UserMstModel.tenants_mst_code == tenant_code,
            UserMstModel.is_deleted == False
        )

        result = await self.db.execute(stmt)
        user = result.scalars().first()

        if user:
            logger.info(
                f"Successfully mapped Slack user {slack_user_id} ({email}) "
                f"to DevLift user {user.code} (tenant: {tenant_code})"
            )
        else:
            logger.warning(
                f"No DevLift user found for Slack user {slack_user_id} "
                f"(email: {email}, tenant: {tenant_code})"
            )

        return user

    async def get_user_display_name(self, slack_user_id: str) -> str:
        """Get user's display name for messages.

        Args:
            slack_user_id: Slack user ID

        Returns:
            User's display name or "User"
        """
        try:
            response = await self.slack_client.users_info(user=slack_user_id)

            if response["ok"]:
                profile = response["user"]["profile"]
                # Try real_name first, fallback to display_name, then email
                return (
                    profile.get("real_name") or
                    profile.get("display_name") or
                    profile.get("email", "User")
                )
            else:
                return "User"

        except Exception as e:
            logger.error(f"Failed to get display name for {slack_user_id}: {str(e)}")
            return "User"

    def format_onboarding_message(self, slack_email: str) -> str:
        """Format onboarding message for users not found in system.

        Args:
            slack_email: User's email from Slack

        Returns:
            Formatted error message
        """
        return (
            f"👋 Welcome to Sage!\n"
            f"You're not set up in the system yet.\n"
            f"Please ask your admin to add you with email:\n"
            f"`{slack_email}`"
        )
