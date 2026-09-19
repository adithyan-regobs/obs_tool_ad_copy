"""Slack Block Kit UI Builder

Builds interactive Slack messages using Block Kit components:
- Selection dropdowns for product/environment/geo_loc
- Code blocks for Terraform/HCL preview
- Approval buttons for deployment
- Error messages and help text
"""
from typing import List, Dict, Any, Optional
from app.core.config import settings
import json


class SlackBlockBuilder:
    """Builds Slack Block Kit UI components for infrastructure conversations."""

    @staticmethod
    def build_selection_prompt(
        selection_type: str,
        options: List[Dict[str, Any]],
        prompt_text: Optional[str] = None,
        conversation_id: Optional[str] = None
    ) -> List[Dict]:
        """Build dropdown/button selection prompt.

        Args:
            selection_type: Type of selection ("product", "resource_group", "service", "environment", "geo_loc")
            options: List of options with 'code' and 'name' fields
            prompt_text: Optional custom prompt text
            conversation_id: Optional conversation/ticket code to embed in button values

        Returns:
            List of Slack Block Kit blocks
        """
        if not prompt_text:
            prompt_text = f"Please select a {selection_type.replace('_', ' ')}:"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": prompt_text
                }
            }
        ]

        # If <= 5 options, use buttons; otherwise use dropdown
        if len(options) <= 5:
            # Button layout
            buttons = []
            for option in options:
                # Encode conversation_id and option code in button value (format: "conversation_id|option_code")
                button_value = f"{conversation_id}|{option['code']}" if conversation_id else option["code"]

                buttons.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": option["name"][:75]  # Slack limit
                    },
                    "value": button_value,
                    "action_id": f"select_{selection_type}_{option['code']}"
                })

            blocks.append({
                "type": "actions",
                "elements": buttons
            })
        else:
            # Dropdown layout
            dropdown_options = [
                {
                    "text": {
                        "type": "plain_text",
                        "text": option["name"][:75]
                    },
                    "value": f"{conversation_id}|{option['code']}" if conversation_id else option["code"]
                }
                for option in options
            ]

            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "static_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": f"Choose {selection_type.replace('_', ' ')}"
                        },
                        "options": dropdown_options,
                        "action_id": f"select_{selection_type}"
                    }
                ]
            })

        return blocks

    @staticmethod
    def build_code_preview(
        code: str,
        service_type: str,
        ai_message: Optional[str] = None,
        show_approval: bool = True,
        chat_info_code: Optional[str] = None
    ) -> Dict:
        """Build code preview with approval buttons.

        Args:
            code: Terraform/HCL code to display
            service_type: Type of service (s3, sqs, gateway, dynamodb)
            ai_message: Optional AI-generated explanation
            show_approval: Whether to show approval buttons
            chat_info_code: Chat session code for tracking approval

        Returns:
            Dict with 'blocks' and optional 'file' for large code
        """
        blocks = []

        # AI explanation message - always show first
        if ai_message:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ai_message
                }
            })

        # Code display logic
        code_length = len(code)
        code_block_limit = settings.slack_code_block_limit

        if code_length < code_block_limit:
            # Inline code block
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```hcl\n{code}\n```"
                }
            })
        else:
            # Code too large - will be sent as file attachment
            # Add a divider to separate from AI message
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"📎 Configuration file ({code_length} chars) will be uploaded below."
                    }
                ]
            })

        # Approval buttons
        if show_approval:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Approve & Deploy"
                        },
                        "style": "primary",
                        "value": chat_info_code or "unknown",
                        "action_id": "approve_deployment"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "❌ Cancel"
                        },
                        "style": "danger",
                        "value": chat_info_code or "unknown",
                        "action_id": "cancel_deployment"
                    }
                ]
            })

        response = {"blocks": blocks}

        # If code is too large, prepare file content
        if code_length >= code_block_limit:
            response["file"] = {
                "content": code,
                "filename": f"{service_type}_config.hcl",
                "filetype": "hcl"
            }

        return response

    @staticmethod
    def build_error_message(error_text: str) -> List[Dict]:
        """Build error message block.

        Args:
            error_text: Error message to display

        Returns:
            List of Slack Block Kit blocks
        """
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"❌ *Error*\n\n{error_text}"
                }
            }
        ]

    @staticmethod
    def build_success_message(
        pr_url: Optional[str] = None,
        message: str = "Deployment successful!"
    ) -> List[Dict]:
        """Build success message with optional PR link.

        Args:
            pr_url: Optional GitHub PR URL
            message: Success message text

        Returns:
            List of Slack Block Kit blocks
        """
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *{message}*"
                }
            }
        ]

        if pr_url:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📝 <{pr_url}|View Pull Request>"
                }
            })

        return blocks

    @staticmethod
    def build_onboarding_message(slack_email: str) -> List[Dict]:
        """Build onboarding message for users not found in system.

        Args:
            slack_email: User's email from Slack

        Returns:
            List of Slack Block Kit blocks
        """
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "👋 *Welcome to Sage!*\n\nYou're not set up in Devlift yet."
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Please ask your admin to add you with email:\n`{slack_email}`"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Once added, you'll be able to create infrastructure from Slack!"
                    }
                ]
            }
        ]

    @staticmethod
    def build_help_message() -> List[Dict]:
        """Build help/getting started message.

        Returns:
            List of Slack Block Kit blocks
        """
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚀 Sage Help"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*What I can do:*\n\n"
                           "• Create S3 buckets\n"
                           "• Create SQS queues\n"
                           "• Create DynamoDB tables\n"
                           "• Add Kong Gateway routes\n"
                           "• View deployment history\n"
                           "• View chat history"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*How to use:*\n\n"
                           "Just chat with me naturally! For example:\n\n"
                           "• \"Create an S3 bucket called user-uploads\"\n"
                           "• \"I need an SQS queue for notifications\"\n"
                           "• \"Add a GET /users route to payment-service\""
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Slash commands:*\n\n"
                           "• `/infra deploy history` - View recent deployments\n"
                           "• `/infra chat history` - View conversation history\n"
                           "• `/infra help` - Show this help message"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Tip: I'll guide you through the process step by step!"
                    }
                ]
            }
        ]

    @staticmethod
    def build_deployment_in_progress() -> List[Dict]:
        """Build 'deployment in progress' message.

        Returns:
            List of Slack Block Kit blocks
        """
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "⏳ *Deploying infrastructure...*\n\nThis may take a few moments."
                }
            }
        ]

    @staticmethod
    def build_deployment_history(deployments: List[Dict[str, Any]]) -> List[Dict]:
        """Build deployment history list.

        Args:
            deployments: List of deployment records

        Returns:
            List of Slack Block Kit blocks
        """
        if not deployments:
            return [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "📋 *Deployment History*\n\nNo deployments found."
                    }
                }
            ]

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📋 Recent Deployments"
                }
            }
        ]

        for deployment in deployments[:10]:  # Limit to 10 recent
            pr_url = deployment.get("pr_url")
            service = deployment.get("service", "Unknown")
            environment = deployment.get("environment", "Unknown")
            status = deployment.get("status", "Unknown")
            created_at = deployment.get("created_at", "Unknown")

            status_emoji = "✅" if status == "success" else "❌"

            text = f"{status_emoji} *{service}* - {environment}\n"
            text += f"Status: {status}\n"
            text += f"Created: {created_at}"

            if pr_url:
                text += f"\n<{pr_url}|View PR>"

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text
                }
            })

        return blocks

    @staticmethod
    def build_deployment_success(
        pr_url: Optional[str] = None,
        commit_sha: Optional[str] = None,
        service_type: Optional[str] = None
    ) -> List[Dict]:
        """Build deployment success message with PR details.

        Args:
            pr_url: GitHub Pull Request URL
            commit_sha: Git commit SHA
            service_type: Type of service deployed

        Returns:
            List of Slack Block Kit blocks
        """
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Deployment Successful!*\n\n{service_type or 'Infrastructure'} has been deployed."
                }
            }
        ]

        if pr_url:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📝 <{pr_url}|View Pull Request>"
                }
            })

        if commit_sha:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Commit: `{commit_sha[:7]}`"
                    }
                ]
            })

        return blocks

    @staticmethod
    def build_deployment_cancelled() -> List[Dict]:
        """Build message when user cancels deployment.

        Returns:
            List of Slack Block Kit blocks
        """
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "❌ *Deployment cancelled* by user.\n\nYou can modify your request and try again anytime."
                }
            }
        ]
