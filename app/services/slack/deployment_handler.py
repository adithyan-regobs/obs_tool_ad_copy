"""Deployment Handler - Orchestrates preview and PR creation flows.

This handler encapsulates all deployment-related logic following the Single Responsibility Principle.
It provides clean separation between:
- Orchestration (public methods)
- Business logic (private helpers)
- State management (via placement_cache)
"""

import asyncio
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from fastapi import HTTPException

from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.services.infrastructure_creation_service import InfrastructureCreationService
from app.services.transaction_queue_service import TransactionQueueService
from app.services.slack.shared import InfrastructureRequestBuilder
from app.services.slack.shared.infrastructure_request_builder import resolve_kong_host_config_code
from app.services.slack import placement_cache
from app.core.enum import WorkflowSourceTableEnum
import logging

logger = logging.getLogger(__name__)


@dataclass
class DeploymentContext:
    """Context data for deployment operations."""
    conversation_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    ticket: Any
    turn_resource: str
    collected_parameters: Dict[str, Any]
    collected_placement_parameters: Dict[str, Any]
    original_blocks: Optional[List[Dict[str, Any]]] = None


@dataclass
class PreviewResult:
    """Result of preview generation."""
    success: bool
    preview_hcl: Optional[str] = None
    full_hcl: Optional[str] = None
    queue_item_id: Optional[int] = None
    infrastructure_code: Optional[str] = None
    identifier: Optional[str] = None
    error_message: Optional[str] = None
    error_status_code: Optional[int] = None


class DeploymentHandler:
    """Handles deployment preview and PR creation orchestration."""

    def __init__(
        self,
        db: AsyncSession,
        slack_client: AsyncWebClient,
        tenant_code: str
    ):
        self.db = db
        self.slack_client = slack_client
        self.tenant_code = tenant_code
        self.queue_service = TransactionQueueService(db)
        self.pr_workflow_service = ScriptPRWorkflowService(db)
        self.infrastructure_service = InfrastructureCreationService(db)
        self.request_builder = InfrastructureRequestBuilder(tenant_code)

    # === Public Orchestration Methods ===

    async def handle_preview(self, context: DeploymentContext) -> PreviewResult:
        """Orchestrate preview generation flow.

        Steps:
        1. Create infrastructure and queue records
        2. Generate preview via pr_workflow_service
        3. Store preview state for reuse by Create PR
        4. Display results in Slack (inline + file attachment)
        5. Update original message to show "preview generated"

        Note: The "generating preview" status is already shown by interaction_handler
        before calling this method, so we don't duplicate it here.
        """
        try:
            # Step 1: Create infrastructure and queue records
            queue_item_id, queue_code, infrastructure_code, config_snapshot = await self._create_deployment_records(context)

            # Step 2: Generate preview
            preview_hcl, full_hcl = await self._generate_preview(
                queue_item_id=queue_item_id,
                user_code=context.ticket.user_mst_code
            )

            # Step 3: Store preview state for reuse (including queue_code)
            await self._store_preview_state(context.conversation_id, queue_item_id, queue_code, infrastructure_code)

            # Step 4: Display results in Slack
            identifier = config_snapshot.get("identifier", "terragrunt")
            await self._display_preview(context, preview_hcl, full_hcl, identifier)

            # Step 5: Update original message to show preview generated
            await self._update_message_after_preview(context)

            return PreviewResult(
                success=True,
                preview_hcl=preview_hcl,
                full_hcl=full_hcl,
                queue_item_id=queue_item_id,
                infrastructure_code=infrastructure_code,
                identifier=identifier
            )

        except HTTPException as e:
            logger.error(f"Preview failed: {str(e)}", exc_info=True)
            return PreviewResult(
                success=False,
                error_message=str(e.detail or e),
                error_status_code=e.status_code
            )
        except Exception as e:
            logger.error(f"Preview failed: {str(e)}", exc_info=True)
            return PreviewResult(success=False, error_message=str(e))

    async def handle_create_pr_after_preview(
        self,
        context: DeploymentContext,
        queue_item_id: int,
        queue_code: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Create PR using existing queue item from preview.

        Updates the infrastructure record and queue item config_snapshot
        with the latest parameters from context before creating the PR.
        This handles the case where the user changed parameters via chat
        after previewing but before clicking Create PR.

        Args:
            context: Deployment context (fresh from LangGraph checkpoint)
            queue_item_id: ID of queue item created during preview
            queue_code: Code of the queue item (stored in preview state)

        Returns:
            Tuple of (success, pr_url, queue_code_or_error)
        """
        try:
            # Update deployment records with latest parameters from context.
            # _create_deployment_records detects existing preview_state and
            # updates the infrastructure_mst + queue item instead of creating new ones.
            updated_queue_item_id, updated_queue_code, _, _ = await self._create_deployment_records(context)

            pr_result = await self.pr_workflow_service.create(
                user_code=context.ticket.user_mst_code,
                tenant_code=self.tenant_code,
                queue_ids=[updated_queue_item_id],
                all_pending_queues=False
            )

            pr_url = self._extract_pr_url(pr_result)

            # Clear preview state
            await placement_cache.clear_preview_state(context.conversation_id)

            return True, pr_url, updated_queue_code

        except HTTPException as e:
            logger.error(f"PR creation failed: {str(e)}", exc_info=True)
            return False, None, {
                "message": str(e.detail or e),
                "status_code": e.status_code
            }
        except Exception as e:
            logger.error(f"PR creation failed: {str(e)}", exc_info=True)
            return False, None, str(e)

    # === Private Helper Methods ===

    async def _create_deployment_records(
        self,
        context: DeploymentContext
    ) -> Tuple[int, str, Optional[str], Dict]:
        """Create infrastructure_mst and queue records.

        Returns:
            Tuple of (queue_item_id, queue_code, infrastructure_code, config_snapshot)
        """
        case_type = context.collected_placement_parameters.get("case_type_ref_code", "")

        # Create infrastructure/kong route config record
        infrastructure_code = None
        if case_type == "add_route" or context.turn_resource == "kong_gateway":
            # Kong routes go to kong_route_configs table
            infrastructure_code = await self._create_kong_route_config(context)
        elif case_type in ("create_database", "database_creation", "user_management", "database_user_management", "mysql_user_management", "postgresql_user_management") or "database" in context.turn_resource:
            # Database creation and user management skip infrastructure_mst - go directly to queue
            logger.info(f"Database flow: skipping infrastructure_mst record, case_type={case_type}, turn_resource={context.turn_resource}")
            infrastructure_code = None
        else:
            infrastructure_code = await self._create_infrastructure_record(context)

        # Build config snapshot
        config_snapshot = self.request_builder.build_config_snapshot(
            turn_resource=context.turn_resource,
            collected_parameters=context.collected_parameters,
            collected_placement_parameters=context.collected_placement_parameters
        )

        # Add to queue
        queue_item = await self._add_to_queue(context, infrastructure_code, config_snapshot)

        return queue_item.id, queue_item.code, infrastructure_code, config_snapshot

    async def _create_infrastructure_record(self, context: DeploymentContext) -> str:
        """Create or update infrastructure_mst record and return its code.

        If an infrastructure_mst_code exists in the preview state cache (from a previous
        preview in this conversation), it will update that record instead of creating new.
        """
        # Check if we have an existing infrastructure_mst_code from previous preview
        preview_state = await placement_cache.get_preview_state(context.conversation_id)
        existing_infrastructure_mst_code = preview_state.get("infrastructure_code") if preview_state else None

        if existing_infrastructure_mst_code:
            logger.info(f"Found existing infrastructure_mst_code={existing_infrastructure_mst_code}, will UPDATE")

        infrastructure_request = self.request_builder.build_infrastructure_request(
            turn_resource=context.turn_resource,
            collected_parameters=context.collected_parameters,
            collected_placement_parameters=context.collected_placement_parameters,
            infrastructure_mst_code=existing_infrastructure_mst_code
        )
        response = await self.infrastructure_service.create_resource(
            tenant_code=self.tenant_code,
            request=infrastructure_request,
            user_email=context.ticket.user_mst_code
        )
        return response.code

    async def _create_kong_route_config(self, context: DeploymentContext) -> str:
        """Create or update kong_route_configs record and return its code.

        If an infrastructure_mst_code exists in the preview state cache (from a previous
        preview in this conversation), it will update that record instead of creating new.
        """
        # Check if we have an existing infrastructure_mst_code from previous preview
        preview_state = await placement_cache.get_preview_state(context.conversation_id)
        existing_infrastructure_mst_code = preview_state.get("infrastructure_code") if preview_state else None

        if existing_infrastructure_mst_code:
            logger.info(f"Found existing kong route code={existing_infrastructure_mst_code}, will UPDATE")

        kong_request = self.request_builder.build_infrastructure_request(
            turn_resource="kong_gateway",
            collected_parameters=context.collected_parameters,
            collected_placement_parameters=context.collected_placement_parameters,
            infrastructure_mst_code=existing_infrastructure_mst_code
        )
        logger.info(f"Creating/updating Kong route config: {kong_request}")
        response = await self.infrastructure_service.create_resource(
            tenant_code=self.tenant_code,
            request=kong_request,
            user_email=context.ticket.user_mst_code
        )
        logger.info(f"Created/updated Kong route config: {response.code}")
        return response.code

    async def _add_to_queue(
        self,
        context: DeploymentContext,
        infrastructure_code: Optional[str],
        config_snapshot: Dict
    ):
        """Add item to transaction queue."""
        case_type = context.collected_placement_parameters.get("case_type_ref_code", "")
        if case_type == "add_route" or context.turn_resource == "kong_gateway":
            # Kong rows are keyed on the kong HOST service's config
            # (SERVICE_CONFIG + case_ref_code "add_route" — Ajmal's
            # KongGatewayTab pattern); the KRC route record stays as
            # inventory only. No legacy KONG_ROUTE keying is ever written.
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
        else:
            table_name = WorkflowSourceTableEnum.INFRASTRUCTURE

        # Determine case_ref_code - use explicit case_ref_code from config (not case_type_ref_code which is a category)
        # case_type_ref_code (e.g. "database") is a category; case_ref_code (e.g. "user_management") is the FK into case_ref table
        case_ref_code = context.collected_placement_parameters.get("case_ref_code", "")
        if not case_ref_code and "database" in context.turn_resource:
            if "database_user" in context.turn_resource:
                case_ref_code = "user_management"
            else:
                case_ref_code = "database_creation"
        if not case_ref_code:
            case_ref_code = case_type

        # Check for existing queue item from previous preview to update instead of creating new
        preview_state = await placement_cache.get_preview_state(context.conversation_id)
        existing_queue_code = preview_state.get("queue_code") if preview_state else None

        queue_item = await self.queue_service.add_item_to_queue(
            user_code=context.ticket.user_mst_code,
            tenant_code=self.tenant_code,
            transaction_code=infrastructure_code,
            table_name=table_name,
            config_snapshot=config_snapshot,
            case_ref_code=case_ref_code,
            ticket_code=context.ticket.code,
            queue_code=existing_queue_code
        )

        # Commit to ensure queue item is persisted for later PR creation
        await self.db.commit()
        logger.info(f"Queue item {queue_item.id} committed to database")

        return queue_item

    async def _generate_preview(
        self,
        queue_item_id: int,
        user_code: str
    ) -> Tuple[str, str]:
        """Generate preview and extract both HCLs.

        Returns:
            Tuple of (preview_hcl, full_hcl)
        """
        preview_result = await self.pr_workflow_service.preview_by_queue_id(
            queue_id=queue_item_id,
            user_code=user_code,
            tenant_code=self.tenant_code
        )

        script_gen = preview_result.get("script_gen_responses", {}).get(queue_item_id, {})
        first_key = next(iter(script_gen.keys()), None)

        if not first_key:
            raise ValueError("No script generated")

        return (
            script_gen[first_key].get("preview_content", ""),
            script_gen[first_key].get("original_content", "")
        )

    async def _store_preview_state(
        self,
        conversation_id: str,
        queue_item_id: int,
        queue_code: str,
        infrastructure_code: Optional[str]
    ):
        """Store preview state in cache for reuse by Create PR."""
        await placement_cache.store_preview_state(
            conversation_id=conversation_id,
            queue_item_id=queue_item_id,
            queue_code=queue_code,
            infrastructure_code=infrastructure_code
        )

    async def _display_preview(
        self,
        context: DeploymentContext,
        preview_hcl: str,
        full_hcl: str,
        identifier: str
    ):
        """Display preview HCL inline, full HCL as file, and buttons below.

        Handles Slack API errors gracefully to ensure the user can still proceed
        even if some display operations fail.

        The file upload has a 5-second timeout - if it takes longer, the upload
        is cancelled and the Create PR button is shown regardless.
        """
        file_upload_failed = False

        # Post preview inline
        try:
            await self.slack_client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Preview HCL:*\n```\n{preview_hcl}\n```"}
                }],
                text="Preview HCL"
            )
        except SlackApiError as e:
            logger.warning(f"Failed to post preview message: {e.response.get('error', str(e))}")
            # Continue - we can still try to upload file and show buttons

        # Upload full HCL as file with 5-second timeout
        # If upload takes longer than 5 seconds, cancel it and show the Create PR button
        try:
            await asyncio.wait_for(
                self.slack_client.files_upload_v2(
                    channel=context.channel_id,
                    thread_ts=context.thread_ts,
                    content=full_hcl,
                    filename=f"{identifier}_terragrunt.hcl",
                    title="Full Terragrunt HCL"
                ),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning("File upload cancelled after 5 second timeout, proceeding to show Create PR button")
            file_upload_failed = True
        except SlackApiError as e:
            error_code = e.response.get('error', 'unknown_error')
            logger.warning(f"Failed to upload HCL file: {error_code}")
            file_upload_failed = True
            # Continue - file upload is not critical, we can still show buttons

        # Small delay to ensure file upload completes before posting buttons
        # Slack file uploads are processed asynchronously
        if not file_upload_failed:
            await asyncio.sleep(0.5)

        # Build button message text
        button_text = "*Ready to create Pull Request?*"
        # Post new message with Create PR and Cancel buttons AFTER the file
        try:
            button_result = await self.slack_client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": button_text}
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Create PR"},
                                "style": "primary",
                                "action_id": "create_pr",
                                "value": context.conversation_id
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel"},
                                "style": "danger",
                                "action_id": "cancel_deployment",
                                "value": context.conversation_id
                            }
                        ]
                    }
                ],
                text="Ready to create Pull Request?"
            )

            # Store the new button message for future invalidation
            if button_result and button_result.get("ts"):
                await placement_cache.store_pending_deployment_message(
                    conversation_id=context.conversation_id,
                    channel_id=context.channel_id,
                    message_ts=button_result["ts"]
                )
        except SlackApiError as e:
            error_code = e.response.get('error', 'unknown_error')
            logger.error(f"Failed to post Create PR buttons: {error_code}")
            # This is more critical - raise to indicate partial failure
            raise ValueError(f"Failed to display Create PR buttons: {error_code}")

    async def _update_message_to_generating(self, context: DeploymentContext):
        """Update original message to show generating status (remove buttons)."""
        try:
            result = await self.slack_client.conversations_history(
                channel=context.channel_id,
                latest=context.message_ts,
                limit=1,
                inclusive=True
            )

            original_blocks = result.get("messages", [{}])[0].get("blocks", [])
            # Remove only actions block and status messages (not config)
            # Status messages start with emoji like ⏳ or ✅
            new_blocks = [
                b for b in original_blocks
                if b.get("type") != "actions" and not (
                    b.get("type") == "section" and
                    b.get("text", {}).get("text", "").startswith(("⏳", "✅"))
                )
            ]

            # Add generating status
            new_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "⏳ *Generating preview...*"}
            })

            await self.slack_client.chat_update(
                channel=context.channel_id,
                ts=context.message_ts,
                blocks=new_blocks,
                text="Generating preview..."
            )
        except Exception as e:
            logger.warning(f"Could not update message to generating: {e}")

    async def _update_message_after_preview(self, context: DeploymentContext):
        """Update original message to show preview generated status."""
        try:
            # Use provided original_blocks (preserves config) or fetch if not available
            if context.original_blocks:
                original_blocks = context.original_blocks
            else:
                result = await self.slack_client.conversations_history(
                    channel=context.channel_id,
                    latest=context.message_ts,
                    limit=1,
                    inclusive=True
                )
                original_blocks = result.get("messages", [{}])[0].get("blocks", [])

            # Remove only actions block and context blocks (status messages)
            # Keep section blocks that contain config
            new_blocks = [
                b for b in original_blocks
                if b.get("type") != "actions" and b.get("type") != "context"
            ]

            # Add preview generated status as context block
            new_blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "✅ Preview generated"
                }]
            })

            await self.slack_client.chat_update(
                channel=context.channel_id,
                ts=context.message_ts,
                blocks=new_blocks,
                text="Preview generated"
            )
        except Exception as e:
            logger.warning(f"Could not update message after preview: {e}")

    def _extract_pr_url(self, pr_result: Dict) -> Optional[str]:
        """Extract PR URL from workflow result."""
        for response in pr_result.get("gitops_responses", {}).values():
            if "pr" in response:
                return response["pr"].get("pr_url") or response["pr"].get("html_url")
        return None
