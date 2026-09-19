"""Shared Placement UI utilities for Slack.

Consolidates UI building and display logic used by both
event_handler and interaction_handler.
"""
from typing import Dict, Any, List, Optional
from slack_sdk.web.async_client import AsyncWebClient

from app.services.slack import placement_cache
from app.services.slack.block_builder import SlackBlockBuilder
from app.services.slack.base_handler import BaseSlackHandler
import logging

logger = logging.getLogger(__name__)


class PlacementUIHelper:
    """Helper class for placement parameter UI operations.

    Consolidates duplicated placement UI logic from event_handler
    and interaction_handler into a single reusable class.
    """

    def __init__(self, slack_client: AsyncWebClient, block_builder: SlackBlockBuilder):
        """Initialize the helper.

        Args:
            slack_client: Slack API client for posting messages
            block_builder: Block builder for creating Slack UI blocks
        """
        self.slack_client = slack_client
        self.block_builder = block_builder

    async def show_processing_indicator(
        self,
        channel_id: str,
        thread_ts: str
    ) -> Optional[str]:
        """Show a processing indicator message while LLM is generating.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp

        Returns:
            Message timestamp if sent, None on error
        """
        try:
            result = await self.slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="Processing...",
                blocks=[{
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": "⏳ Processing..."
                    }]
                }]
            )
            if result and result.get("ts"):
                return result["ts"]
            return None
        except Exception as e:
            logger.warning(f"Could not show processing indicator: {e}")
            return None

    async def update_processing_indicator(
        self,
        channel_id: str,
        message_ts: str,
        new_text: str,
        blocks: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Update the processing indicator with the actual response.

        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp of the processing indicator
            new_text: New text to display
            blocks: Optional blocks to display
        """
        try:
            if blocks:
                await self.slack_client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=new_text,
                    blocks=blocks
                )
            else:
                # Use context block for consistent muted style
                await self.slack_client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=new_text,
                    blocks=[{
                        "type": "context",
                        "elements": [{
                            "type": "mrkdwn",
                            "text": new_text
                        }]
                    }]
                )
        except Exception as e:
            logger.warning(f"Could not update processing indicator: {e}")

    async def delete_processing_indicator(
        self,
        channel_id: str,
        message_ts: str
    ) -> None:
        """Delete the processing indicator message.

        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp of the processing indicator
        """
        try:
            await self.slack_client.chat_delete(
                channel=channel_id,
                ts=message_ts
            )
        except Exception as e:
            logger.warning(f"Could not delete processing indicator: {e}")

    def build_placement_ui(
        self,
        param_meta: Dict[str, Any],
        param_name: str,
        conversation_id: str
    ) -> List[Dict[str, Any]]:
        """Build Slack UI blocks for a placement parameter.

        Args:
            param_meta: Parameter metadata from contract
            param_name: Parameter name
            conversation_id: Ticket code to embed in button values

        Returns:
            List of Slack Block Kit blocks
        """
        try:
            display_name = param_meta.get("name", param_name)
            value_source = param_meta.get("value_source", {})
            options = value_source.get("options", [])

            if options:
                # Convert contract format to block_builder format
                # Contract: [{"label": "AWS", "value": "aws"}]
                # Builder needs: [{"code": "aws", "name": "AWS"}]
                formatted_options = [
                    {"code": opt["value"], "name": opt["label"]}
                    for opt in options
                ]

                return self.block_builder.build_selection_prompt(
                    selection_type=param_name,
                    options=formatted_options,
                    prompt_text=f"Please select {display_name}:",
                    conversation_id=conversation_id
                )
            else:
                # Text input fallback
                return [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{display_name}*\n\nPlease type your response:"
                        }
                    }
                ]

        except Exception as e:
            logger.error(f"Error building placement UI: {str(e)}", exc_info=True)
            return []

    async def show_next_param_ui(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        response_text: Optional[str] = None,
        say=None
    ) -> bool:
        """Show UI for next placement parameter from cache.

        Handles auto-selection of single-option params.

        Args:
            conversation_id: Ticket code
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            response_text: Optional text to send before UI
            say: Optional Slack say function

        Returns:
            True if parameter was shown or auto-selected, False if all complete or error
        """
        try:
            if response_text:
                formatted_text = BaseSlackHandler.format_response_for_slack(response_text)
                if say:
                    await say(text=formatted_text, thread_ts=thread_ts)
                else:
                    await self.slack_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=formatted_text
                    )

            next_param = await placement_cache.get_next_param(conversation_id)
            if not next_param:
                logger.warning(f"No remaining params in cache for {conversation_id}")
                return False

            param_name, param_meta = next_param
            value_source = param_meta.get("value_source", {})
            options = value_source.get("options", [])

            # Auto-select single options
            if options and len(options) == 1:
                auto_selected_value = options[0]["value"]
                display_name = param_meta.get("name", param_name)

                logger.info(f"Auto-selecting only option for {param_name}: {auto_selected_value}")

                auto_select_text = f"*{display_name}:* {options[0]['label']}"
                if say:
                    await say(text=auto_select_text, thread_ts=thread_ts)
                else:
                    await self.slack_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=auto_select_text
                    )

                await placement_cache.update_placement_selection(
                    conversation_id, param_name, auto_selected_value, options[0]['label']
                )

                # Recursively check for next param
                if not await placement_cache.is_placement_complete(conversation_id):
                    return await self.show_next_param_ui(
                        conversation_id, channel_id, thread_ts, None, say
                    )
                return True

            # Build and send UI for user selection
            blocks = self.build_placement_ui(param_meta, param_name, conversation_id)
            if blocks:
                if say:
                    result = await say(blocks=blocks, thread_ts=thread_ts)
                else:
                    result = await self.slack_client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        blocks=blocks
                    )

                # Store the message_ts so we can invalidate buttons if user sends new input
                if result and result.get("ts"):
                    await placement_cache.store_pending_placement_message(
                        conversation_id=conversation_id,
                        channel_id=channel_id,
                        message_ts=result["ts"]
                    )
            return True

        except Exception as e:
            logger.error(f"Error showing placement UI from cache: {str(e)}", exc_info=True)
            error_text = "Error displaying parameter selection."
            if say:
                await say(text=error_text, thread_ts=thread_ts)
            else:
                await self.slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=error_text
                )
            return False

    async def show_deployment_confirmation(
        self,
        conversation_id: str,
        channel_id: str,
        thread_ts: str,
        collected_parameters: Optional[Dict[str, Any]] = None,
        response_text: Optional[str] = None,
        say=None,
        collected_placement_parameters: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Show deployment confirmation with Create PR/Cancel buttons.

        Args:
            conversation_id: Ticket code
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            collected_parameters: Parameters to display in summary (v1: nested dict with
                placement_parameters/attribute_parameters sub-keys; v2: flat attribute dict)
            response_text: Optional text to include at top
            say: Optional Slack say function
            collected_placement_parameters: Separate placement params dict (v2 text-placement flow)

        Returns:
            Message timestamp if sent, None on error
        """
        try:
            # Build header
            header_text = "*Configuration ready*\n\nCreate a Pull Request to deploy this infrastructure?"
            if response_text:
                formatted_response = BaseSlackHandler.format_response_for_slack(response_text)
                header_text = f"{formatted_response}\n\n---\n\n{header_text}"

            summary_blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": header_text}
                }
            ]

            # Add parameter summary if available
            if collected_parameters:
                # Try v1 nested format first (placement_parameters/attribute_parameters sub-keys)
                placement = collected_parameters.get("placement_parameters", {})
                attributes = collected_parameters.get("attribute_parameters", {})

                # If nested format is empty, use v2 flat format:
                # collected_parameters IS the attributes dict, placement comes separately
                if not placement and not attributes:
                    attributes = collected_parameters
                    placement = collected_placement_parameters or {}

                if placement or attributes:
                    param_text = "*Configuration Summary:*\n"

                    if placement:
                        param_text += "\n_Placement:_\n"
                        for key, value in placement.items():
                            if "code" in key.lower():
                                continue
                            display_key = key.replace("_", " ").replace("enum", "").strip().title()
                            param_text += f"* {display_key}: `{value}`\n"

                    if attributes:
                        param_text += "\n_Attributes:_\n"
                        for key, value in attributes.items():
                            display_key = key.replace("_", " ").title()
                            param_text += f"* {display_key}: `{value}`\n"

                    summary_blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": param_text}
                    })

            # Add action buttons
            summary_blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Preview"},
                        "action_id": "preview_deployment",
                        "value": conversation_id
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Create PR"},
                        "style": "primary",
                        "action_id": "create_pr",
                        "value": conversation_id
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "cancel_deployment",
                        "value": conversation_id
                    }
                ]
            })

            # Send the message
            if say:
                result = await say(blocks=summary_blocks, thread_ts=thread_ts)
            else:
                result = await self.slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=summary_blocks
                )

            # Store the message_ts so we can invalidate buttons if user sends new input
            if result and result.get("ts"):
                await placement_cache.store_pending_deployment_message(
                    conversation_id=conversation_id,
                    channel_id=channel_id,
                    message_ts=result["ts"]
                )
                return result["ts"]

            return None

        except Exception as e:
            logger.error(f"Error showing deployment confirmation: {str(e)}", exc_info=True)
            error_text = "Something went wrong. Please try again."
            if say:
                await say(text=error_text, thread_ts=thread_ts)
            else:
                await self.slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=error_text
                )
            return None
