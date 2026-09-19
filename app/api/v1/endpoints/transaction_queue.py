"""
GitOps Queue API Endpoints

API endpoints for the deploy queue feature.
Allows adding items to queue, deploying all as single PR, and refreshing stale PRs.
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import (
    AuthenticationOnly, Authorization, AuthorizationFromBody, SecureRouter, mark_checked,
)
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.transaction_queue_service import (
    TransactionQueueService,
    validate_deployable_queue_items,
)
from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.schemas.transaction_queue_schemas import (
    AddToQueueRequest,
    TransactionQueueItemResponse,
    TransactionQueueItemWithTicketResponse,
    TransactionQueueListResponse,
    TransactionQueueDeployRequest,
    TransactionQueueDeployResponse,
    TransactionQueuePreviewResponse,
    TransactionQueuePRStatusResponse,
    TransactionQueuePRRefreshRequest,
    TransactionQueuePRRefreshResponse,
    TransactionQueuePRListResponse,
    TransactionQueuePRListItem,
    DeleteQueueItemResponse,
    SearchQueueRequest,
    SearchQueueResponse,
    SearchByTicketRequest,
    ConflictResolveRequest,
    ConflictResolveResponse,
    BulkApproveRequest,
    BulkApproveResponse,
)
from app.schemas.script_preview_schemas import ScriptPreviewResponse, ScriptPRCreateRequest, ScriptPRCreateResponse

router = APIRouter()

# Declared here, not further down, because carded routes appear throughout this
# module — ensure-settings-deploy-item sits near the top. Same
# /transaction-queue prefix as `router` (see api/v1/router.py); SecureRouter
# simply refuses to register a route that declares no access card.
secure_router = SecureRouter()


@router.post("/add-to-queue", response_model=TransactionQueueItemResponse, status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    request: AddToQueueRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Add any item type to the transaction queue.

    Supports all item types via polymorphic design (services, infrastructure, alerts, routes, pipelines).

    Frontend provides:
    - transaction_code: Code of the source entity
    - table_name: Source table enum (SERVICE_CONFIG, INFRASTRUCTURE, etc.)
    - config_snapshot: Configuration parameters from frontend
    - case_ref_code: Optional case reference
    - queue_code: Optional - if provided and status is APPROVED, updates existing; otherwise creates new

    Backend extracts from JWT:
    - user_code
    - tenant_code

    Backend enriches:
    - config_snapshot with resolved values (product_name, etc.)
    - display_name generated from config

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Request Body:
        - transaction_code: Source entity code
        - table_name: Source table (SERVICE_CONFIG, INFRASTRUCTURE, etc.)
        - config_snapshot: Configuration parameters
        - case_ref_code: Optional case reference
        - queue_code: Optional - updates existing if provided AND status is APPROVED; otherwise creates new

    Response:
        Created or updated queue item with all details

    Errors:
        - 400: Invalid configuration or queue_code mismatch
        - 401: Authentication required
        - 403: Access denied

    Example:
        POST /api/v1/gitops-queue/add-to-queue
        {
            "transaction_code": "infra-mst-abc123",
            "table_name": "INFRASTRUCTURE",
            "config_snapshot": {
                "identifier": "my-s3-bucket",
                "infra_type": "s3",
                "environment": "dev",
                "applications_mst_code": "app-xyz",
                "geo_loc_mst_code": "region-mumbai",
                "versioning": true
            },
            "case_ref_code": "case-s3-001"
        }
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        item = await service.add_item_to_queue(
            user_code=user.code,
            tenant_code=tenant.code,
            transaction_code=request.transaction_code,
            table_name=request.table_name,
            config_snapshot=request.config_snapshot,
            case_ref_code=request.case_ref_code,
            ticket_code=request.ticket_code,
            queue_code=request.queue_code
        )

        return TransactionQueueItemResponse.from_orm(item)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/search-by-transaction-code", response_model=SearchQueueResponse)
async def search_queue(
    request: SearchQueueRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Search for a DRAFT queue item by transaction code and table name.

    Searches the transaction_queue table for items matching the provided
    transaction_code and table_name with status=DRAFT. If multiple draft
    entries exist, returns the most recently created one. If no draft
    entry exists, returns null.

    Includes ticket details when available.

    Security:
        - JWT authentication required
        - Tenant isolation enforced
        - User isolation enforced

    Request Body:
        - transaction_code: Code of the source entity (e.g., service_config.code, infrastructure_mst.code)
        - table_name: Source table enum (SERVICE_CONFIG, INFRASTRUCTURE, ALERT_CONFIG, KONG_ROUTE, PIPELINE, SERVICE_CONFIG_DOCKERFILE)

    Response:
        - found: Boolean indicating if a matching draft item was found
        - item: The latest draft queue item with ticket details if found, null otherwise

    Example:
        POST /api/v1/transaction-queue/search-by-transaction-code
        {
            "transaction_code": "sc-dfe94fca-c9a2-4080-b5de-b300ec1945c9",
            "table_name": "SERVICE_CONFIG"
        }

    Response (found):
        {
            "found": true,
            "item": {
                "id": 666,
                "code": "queue-abc123",
                "transaction_code": "sc-dfe94fca-c9a2-4080-b5de-b300ec1945c9",
                "table_name": "SERVICE_CONFIG",
                "status": "draft",
                "ticket": {
                    "id": 123,
                    "code": "TKT-36033D09",
                    "ticket_number": "ticket-001",
                    ...
                },
                ...
            }
        }

    Response (not found):
        {
            "found": false,
            "item": null
        }
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    item = await service.search_draft_queue(
        transaction_code=request.transaction_code,
        table_name=request.table_name,
        tenant_code=tenant.code,
        user_code=user.code,
        case_ref_code=request.case_ref_code,
        include_failed=request.include_failed,
    )

    if item:
        return SearchQueueResponse(
            found=True,
            item=TransactionQueueItemWithTicketResponse.model_validate(item)
        )

    return SearchQueueResponse(
        found=False,
        item=None
    )


class EnsureSettingsDeployItemRequest(BaseModel):
    transaction_code: str = Field(..., description="service_config code to deploy")


class EnsureSettingsDeployItemResponse(BaseModel):
    item_id: Optional[int] = Field(
        None,
        description="APPROVED update_service queue item id ready to deploy; null if there is nothing to deploy or the service_config was not found."
    )
    no_changes: bool = Field(
        False,
        description="True when the live config already matches the last deployed state — the caller should skip the deploy (nothing to push)."
    )


@secure_router.post(
    "/ensure-settings-deploy-item",
    response_model=EnsureSettingsDeployItemResponse,
    summary="Find or create an APPROVED settings item this caller can deploy",
    # Uncarded until now, and it does not merely READ: it can create a queue
    # item and approve it (bulk_approve_by_ids below), so authentication alone
    # let anyone mint an APPROVED row for any service in their tenant. It is
    # also step 4 of the prod deploy, sitting between two carded create-pr
    # calls — the one link in that chain that checked nothing.
    #
    # can_deploy, not can_update: this exists only to make a deploy possible,
    # and the caller is about to ship the result.
    access=AuthorizationFromBody(
        permission="can_deploy",
        obj_type="service",
        field="transaction_code",
        deny_status=403,
    ),
)
async def ensure_settings_deploy_item(
    request: EnsureSettingsDeployItemRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Return an APPROVED settings queue item the CURRENT user can deploy.

    The settings diff is shared per service_config, so a user may see a change
    made by another user (or a failed deploy) that they don't own a queue item
    for. This reuses the caller's own DRAFT/APPROVED/FAILED item, or — when they
    have none — snapshots the current live config into a new item owned by the
    caller, so "Apply & Redeploy" always works regardless of who saved.
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)
    item_id, no_changes = await service.ensure_settings_deploy_item(
        transaction_code=request.transaction_code,
        tenant_code=tenant.code,
        user_code=user.code,
    )
    return EnsureSettingsDeployItemResponse(item_id=item_id, no_changes=no_changes)


@router.post("/search-by-ticket", response_model=SearchQueueResponse)
async def search_by_ticket(
    request: SearchByTicketRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Search for an APPROVED queue item by ticket code.

    Searches the transaction_queue table for items matching the provided
    ticket_code with status=APPROVED. If multiple approved entries exist,
    returns the most recently created one. If no approved entry exists,
    returns null.

    Security:
        - JWT authentication required
        - Tenant isolation enforced
        - User isolation enforced

    Request Body:
        - ticket_code: Ticket code to search for associated queue items

    Response:
        - found: Boolean indicating if a matching approved item was found
        - item: The latest approved queue item if found, null otherwise

    Example:
        POST /api/v1/transaction-queue/search-by-ticket
        {
            "ticket_code": "ticket-abc123"
        }

    Response (found):
        {
            "found": true,
            "item": {
                "id": 123,
                "code": "queue-xyz",
                "ticket_code": "ticket-abc123",
                "status": "approved",
                ...
            }
        }

    Response (not found):
        {
            "found": false,
            "item": null
        }
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    item = await service.search_by_ticket(
        ticket_code=request.ticket_code,
        tenant_code=tenant.code,
        user_code=user.code
    )

    if item:
        return SearchQueueResponse(
            found=True,
            item=TransactionQueueItemResponse.from_orm(item)
        )

    return SearchQueueResponse(
        found=False,
        item=None
    )


@router.get("", response_model=TransactionQueueListResponse)
async def get_queue(
    environment: Optional[str] = Query(None, description="Filter by environment"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Get current user's deploy queue items.

    Returns all non-deleted queue items for the authenticated user,
    including pending items and items with PRs raised.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Query Parameters:
        - environment: Optional filter by environment (dev, staging, prod)

    Response:
        - items: List of queue items
        - total: Total count of items
        - pending_count: Count of pending items
        - pr_raised_count: Count of items with PRs raised

    Example:
        GET /api/v1/gitops-queue
        GET /api/v1/gitops-queue?environment=dev
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    response = await service.get_user_queue(
        user_code=user.code,
        tenant_code=tenant.code
    )

    # Filter by environment if provided
    if environment:
        response.items = [
            item for item in response.items
            if item.environment == environment
        ]
        response.total = len(response.items)

    return response


@router.delete("/{queue_code}", response_model=DeleteQueueItemResponse)
async def delete_queue_item(
    queue_code: str,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Delete a queue item by queue code.

    Only items with status DRAFT or APPROVED can be deleted. After these statuses,
    entries become immutable and cannot be deleted.

    Security:
        - JWT authentication required
        - User can only delete their own queue items
        - Only DRAFT or APPROVED status items can be deleted

    Path Parameters:
        - queue_code: Queue item code (e.g., "queue-abc123def456")

    Response:
        Confirmation of deletion with queue code

    Errors:
        - 400: Item cannot be deleted (not DRAFT/APPROVED status or not owned by user)
        - 404: Queue item not found

    Example:
        DELETE /api/v1/transaction-queue/queue-abc123def456
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        deleted_id = await service.delete_queue_item(
            queue_code=queue_code,
            user_code=user.code
        )

        if not deleted_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Queue item with code '{queue_code}' not found"
            )

        return DeleteQueueItemResponse(
            id=deleted_id,
            status="deleted",
            message=f"Queue item '{queue_code}' deleted successfully"
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/{item_id}/preview", response_model=TransactionQueuePreviewResponse)
async def preview_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Preview HCL content for a queue item.

    Generates HCL from the stored config snapshot to show exactly
    what will be deployed.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Path Parameters:
        - item_id: Queue item ID to preview

    Response:
        - hcl_content: Generated HCL content
        - file_path: File path in repository
        - atlantis_project_name: Atlantis project name
        - service_name, environment, infra_type

    Errors:
        - 404: Item not found

    Example:
        GET /api/v1/gitops-queue/123/preview
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        preview = await service.preview_item(item_id)
        return preview

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/{item_id}/script-preview", response_model=ScriptPreviewResponse)
async def preview_script_content(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Preview generated script content for a queue item.

    Returns the actual Terraform/Terragrunt script content that will be generated,
    along with file locations and metadata. Shows previews for ALL branches
    (for service configs with multiple branches).

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Path Parameters:
        - item_id: Queue item ID to preview

    Response:
        - queue_id: Queue item ID
        - environment: Environment (dev/staging/prod)
        - infra_type: Infrastructure type (s3, sqs, ecs, etc.)
        - files: Array of file previews:
            - branch: Target branch name
            - file_path: Path to file in repository
            - operation: "create" or "update"
            - script_content: Generated script content (HCL)
            - metadata: Additional information (repo, owner, etc.)
        - metadata: Summary information

    Errors:
        - 404: Item not found or invalid configuration
        - 500: Script generation failed

    Example:
        GET /api/v1/gitops-queue/123/script-preview

    Response:
        {
            "queue_id": 123,
            "environment": "dev",
            "infra_type": "s3",
            "files": [
                {
                    "branch": "main",
                    "file_path": "environment/dev/buckets/my-bucket/terragrunt.hcl",
                    "operation": "create",
                    "script_content": "terragrunt {\\n  ...",
                    "metadata": {
                        "github_url": "https://github.com/aspora/vance-core-infrastructure",
                        "repo_type": "infrastructure",
                        "owner": "aspora",
                        "repo": "vance-core-infrastructure"
                    }
                }
            ],
            "metadata": {
                "tenant_code": "aspora",
                "total_files": 1,
                "successful_files": 1
            }
        }
    """
    user, tenant = current_user_tenant

    # ── who may look ─────────────────────────────────────────────────────────
    # The row is loaded directly rather than through get_selected_queues,
    # because that query filters to the CALLER's own rows — which silently
    # 404'd the preview for the two people the approval flow most needs to
    # show it to. The rule here is the inbox's visibility rule: the AUTHOR
    # (whoever created the draft), or anyone holding can_approve or can_deploy
    # on the service. The generated file is the most detailed view of a change
    # there is; it must not be the least guarded.
    from sqlalchemy import select as _select

    from app.db.models.transaction_queue_model import TransactionQueueModel as _TQ
    from app.services.approval_service import fga_ref as _fga_ref
    from app.core.authz import fga as _fga

    row = (
        await db.execute(
            _select(_TQ).where(
                _TQ.id == item_id,
                _TQ.tenant_code == tenant.code,
                _TQ.is_deleted.isnot(True),
            )
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Queue item {item_id} not found")

    if row.user_code != user.code:
        allowed = False
        try:
            approve_ok, deploy_ok = await _fga.batch_check([
                (f"user:{user.code}", "can_approve", _fga_ref(row)),
                (f"user:{user.code}", "can_deploy", _fga_ref(row)),
            ])
            allowed = approve_ok or deploy_ok
        except Exception:
            # fga_ref raises for row kinds the model does not map (infra) and
            # the check itself fails closed — either way, not allowed.
            allowed = False
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to preview this change — it "
                       "belongs to another user and you hold neither approve "
                       "nor deploy rights on its service.",
            )

    service = ScriptPRWorkflowService(db)

    try:
        # The AUTHOR's code, not the caller's: the service re-fetches the row
        # through the author-filtered query, and an approver's own code would
        # find nothing there.
        preview = await service.preview_by_queue_id(
            queue_id=item_id,
            user_code=row.user_code,
            tenant_code=tenant.code
        )

        # ── normalise per file, so the UI renders blind ──────────────────────
        # Two honesty rules learned by comparing previews against the PRs they
        # became:
        #   1. Some generators nest one level deeper, BY BRANCH
        #      ({key: {branch: {original_content, preview_content}}}) — read
        #      flat, those files showed "empty" while the real PR changed them.
        #   2. Generators regenerate EVERY file; the PR only contains the ones
        #      that differ from the repo. So each file is compared against the
        #      CURRENT repository content, and `changed` says whether it will
        #      actually appear in the PR — a preview that lists eight files
        #      when the PR will touch two is a fake.
        from app.handlers.gitops_handler import GitOpsHandler

        raw = (preview.get("script_gen_responses") or {}).get(row.id) or {}
        locs = (preview.get("file_location_responses") or {}).get(row.id)
        # ALL locations per key — one script_gen_key can cover several files
        # (k8s_chart_template maps configmap.yaml, deployment.yaml, ...), so a
        # first-one-wins map pinned another file's content to the wrong path.
        loc_by_key: dict = {}
        for f in (getattr(locs, "files", None) or []):
            key = getattr(f, "script_gen_key", None)
            if key:
                loc_by_key.setdefault(key, []).append(f)

        async def _current_content(file_loc) -> str | None:
            """The file as the repository holds it TODAY, or None unreadable."""
            if file_loc is None:
                return None
            try:
                parts = (file_loc.repo or "").split("/")
                owner = parts[0] if len(parts) > 1 else None
                repo_name = parts[1] if len(parts) > 1 else file_loc.repo
                res = await GitOpsHandler.get_content(
                    tenant=tenant.code, owner=owner, repo=repo_name,
                    file_path=file_loc.file_path,
                    branch=file_loc.base_branch or "main", db=db,
                )
                if isinstance(res, dict) and res.get("exists"):
                    return res.get("content")
                if isinstance(res, dict) and res.get("status") != "error":
                    return ""  # readable repo, file absent -> a NEW file
            except Exception:
                pass
            return None

        def _payload(entry) -> list[tuple[str | None, dict]]:
            """(branch, {original,preview,warning}) pairs, whatever the shape."""
            if isinstance(entry, str):
                return [(None, {"preview_content": entry})]
            if not isinstance(entry, dict):
                return []
            content_keys = {"original_content", "preview_content", "warning_message"}
            if content_keys & set(entry.keys()):
                return [(None, entry)]
            # branch-nested
            out = []
            for branch, payload in entry.items():
                if isinstance(payload, dict):
                    out.append((branch, payload))
                elif isinstance(payload, str):
                    out.append((branch, {"preview_content": payload}))
            return out

        _current_cache: dict = {}

        async def _current_cached(file_loc) -> str | None:
            if file_loc is None:
                return None
            ck = (getattr(file_loc, "repo", None), getattr(file_loc, "file_path", None))
            if ck not in _current_cache:
                _current_cache[ck] = await _current_content(file_loc)
            return _current_cache[ck]

        files = []
        for script_type, entry in raw.items():
            key_locs = loc_by_key.get(script_type) or []
            for branch, payload in _payload(entry):
                # Nested entries key by base_branch (docker/workflow) OR by
                # file_path (k8s templates in dry runs). A file_path match
                # picks the exact file; otherwise the key's first location.
                file_loc = next(
                    (l for l in key_locs if getattr(l, "file_path", None) == branch),
                    key_locs[0] if key_locs else None,
                )
                current = await _current_cached(file_loc)
                warning = payload.get("warning_message")
                # `original_content` is the string _upsert_staged_entry commits —
                # the bytes GitHub will diff. `preview_content` is, for several
                # generators (atlantis, workflow), a human-readable SUMMARY
                # ("Atlantis project entry: ..."), so preferring it here made
                # unchanged files look rewritten. Only fall back to it when the
                # generator recorded no original.
                after = payload.get("original_content") or payload.get("preview_content")
                # The docker generator reports its "cannot preview" case by
                # putting the warning TEXT into preview_content — that is a
                # message, not file content.
                if warning and after == warning:
                    after = None
                # git's rules, exactly — the deploy commits `after` byte for
                # byte and the PR diff is git's comparison against the base
                # branch, so the ONLY correct membership test is byte equality.
                # (An earlier version stripped whitespace first; git does not,
                # and a trailing-newline change is a real +1/-1 on GitHub.)
                #   current is None -> the repo could not be read: unknown
                #   current == ""   -> file absent on base: ADDED (all-green)
                #   after == current-> byte-identical: git drops it from the PR
                #   else            -> MODIFIED
                before = current if current else None
                if after is None:
                    status_val, changed = "unknown", None
                elif current is None:
                    status_val, changed = "unknown", None
                elif current == "":
                    status_val, changed = ("unchanged", False) if after == "" else ("added", True)
                elif after == current:
                    status_val, changed = "unchanged", False
                else:
                    status_val, changed = "modified", True
                files.append({
                    "path": getattr(file_loc, "file_path", None) or script_type,
                    "script_type": script_type,
                    "branch": branch,
                    "repo": getattr(file_loc, "repo", None),
                    "base_branch": getattr(file_loc, "base_branch", None),
                    "status": status_val,
                    "before": before,
                    "after": after,
                    "warning": warning,
                    "changed": changed,
                })
        preview["files"] = files
        return preview

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.error(f"Script preview failed for item {item_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Script preview failed: {str(e)}"
        )


@router.post("/create-pr", response_model=ScriptPRCreateResponse)
async def create_script_pr(
    request: ScriptPRCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Create PRs for queue items with generated scripts.

    Processes multiple queue items, generates scripts, creates feature branches,
    commits files to Git, and creates Pull Requests in GitHub.

    This is the main workflow endpoint that:
    1. Fetches queue items (by IDs or all pending)
    2. Determines file locations for each item
    3. Generates Terraform/Terragrunt scripts
    4. Creates feature branches in GitHub (one per repo/branch combination)
    5. Commits generated files to feature branches
    6. Creates Pull Requests
    7. Saves workflow mappings and details to database

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Request Body:
        - queue_ids: Optional list of specific queue IDs to process
        - all_pending_queues: If True, process all pending queues for the user

    Response:
        - feature_branches: Created feature branches (repo-branch -> feature_branch_name)
        - file_location_responses: File locations determined per queue_id
        - script_gen_responses: Generated scripts per queue_id
        - gitops_responses: Git commits and PRs per repo-branch
        - total_items_processed: Total queue items processed
        - total_feature_branches: Total feature branches created
        - total_prs_created: Total PRs created

    Errors:
        - 400: Invalid request (no queue_ids provided and all_pending_queues is False)
        - 404: No queue items found
        - 500: Script generation or Git operations failed

    Example:
        POST /api/v1/transaction-queue/create-pr
        {
            "queue_ids": [1, 2, 3],
            "all_pending_queues": false
        }

        POST /api/v1/transaction-queue/create-pr
        {
            "queue_ids": null,
            "all_pending_queues": true
        }
    """
    user, tenant = current_user_tenant
    return await _run_create_script_pr(request, user, tenant, db)


async def _run_create_script_pr(
    request: ScriptPRCreateRequest,
    user: UserMstModel,
    tenant: TenantsMstModel,
    db: AsyncSession,
    authorized_transaction_code: Optional[str] = None,
) -> ScriptPRCreateResponse:
    """Shared body of the legacy and FGA-carded create-pr routes.

    `authorized_transaction_code` is the object the route's card authorized
    against, when it had one — a service_configs code on /by-config, an
    infra_mst code on /by-infra. Both land in the queue row's transaction_code,
    which is why the guard compares against that one column rather than against
    a per-kind field. The card checks one object; the ids arrive separately.
    """
    # Approved, unmodified, and belonging to what was authorized. Raises rather
    # than shipping a smaller set: the service below FILTERS to APPROVED, so an
    # unapproved id used to be dropped and reported as "no queue items found".
    #
    # Only when ids were named. `all_pending_queues` has none to check — the
    # service resolves the caller's own APPROVED rows itself, so there is
    # nothing here to validate ahead of it.
    if request.queue_ids:
        await validate_deployable_queue_items(
            db,
            queue_ids=request.queue_ids,
            tenant_code=tenant.code,
            authorized_transaction_code=authorized_transaction_code,
        )

    service = ScriptPRWorkflowService(db)

    try:
        # create_with_run_track, not create: this is the entry point prod uses
        # instead of deploying, so it needs its own pipeline run-track timeline.
        # Every OTHER caller of create() stays as-is — deploy_activities'
        # run_script_pr_workflow in particular already runs inside a batch that
        # owns a run-track row, and a second one would compete with it.
        result = await service.create_with_run_track(
            user_code=user.code,
            tenant_code=tenant.code,
            queue_ids=request.queue_ids,
            all_pending_queues=request.all_pending_queues
        )

        # Add summary counts to response
        total_prs_created = len([v for v in result['gitops_responses'].values() if 'pr' in v])
        jenkins_results = result.get('jenkins_results', [])

        return ScriptPRCreateResponse(
            feature_branches=result['feature_branches'],
            file_location_responses=result['file_location_responses'],
            script_gen_responses=result['script_gen_responses'],
            gitops_responses=result['gitops_responses'],
            total_items_processed=len(result['file_location_responses']) + len(jenkins_results),
            total_feature_branches=len(result['feature_branches']),
            total_prs_created=total_prs_created,
            jenkins_results=jenkins_results,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.error(f"Script PR creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Script PR creation failed: {str(e)}"
        )


# ── Carded create-pr entry points ────────────────────────────────────────────
# Two routes because authorization is held against different object types.
# Service surfaces (settings save, gateway deploy) carry a service_configs.code
# and are FGA-checked; standalone infra resources (S3/SQS/Dynamo settings)
# carry an infrastructure_mst.code and stay AUTHENTICATION-ONLY for now —
# infra_mst is not in the phase-1 model yet. The queue items stay scoped to
# the requesting user + tenant inside create_with_run_track, exactly as on
# the legacy route.
# (declared at the top of this module — see beside `router`.)


@secure_router.post(
    "/by-config/{service_config_code}/create-pr",
    response_model=ScriptPRCreateResponse,
    access=Authorization(
        permission="can_deploy",
        obj_type="service",
        param="service_config_code",
        deny_status=403,
    ),
)
async def create_script_pr_by_config(
    service_config_code: str,
    payload: ScriptPRCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Create-pr entered from a service surface (settings save, gateway
    deploy). The card already checked can_deploy on service:<code>."""
    user, tenant = current_user_tenant

    config_repo = ServiceConfigRepository(db)
    row = await config_repo.get_by_code_and_tenant(service_config_code, tenant.code)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service configuration with code {service_config_code} not found"
        )

    # The card authorized can_deploy on this code alone; the queue ids arrive
    # separately in the body, so the guard is told what to hold them to.
    return await _run_create_script_pr(
        payload, user, tenant, db,
        authorized_transaction_code=service_config_code,
    )


@secure_router.post(
    "/by-infra/{infrastructure_mst_code}/create-pr",
    response_model=ScriptPRCreateResponse,
    access=AuthenticationOnly(
        reason="infra_mst is not in the phase-1 FGA model yet — tenant "
        "isolation is enforced in the handler; the FGA card follows when "
        "infra_mst enters the model"
    ),
)
async def create_script_pr_by_infra(
    infrastructure_mst_code: str,
    payload: ScriptPRCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Create-pr entered from a standalone infra resource's settings page
    (S3 / SQS / Dynamo)."""
    user, tenant = current_user_tenant

    infra_repo = InfrastructureMstRepository(db)
    row = await infra_repo.get_by_code(infrastructure_mst_code)
    # get_by_code is not tenant-filtered — enforce isolation here, and answer
    # 404 so a foreign code cannot be probed for existence.
    if not row or row.tenants_mst_code != tenant.code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Infrastructure with code {infrastructure_mst_code} not found"
        )

    # An infra_mst code lands in the SAME transaction_code column a service
    # code does, so the guard holds the queue ids to this resource exactly as
    # /by-config holds them to its service.
    return await _run_create_script_pr(
        payload, user, tenant, db,
        authorized_transaction_code=infrastructure_mst_code,
    )


@router.post("/pr/{pr_number}/resolve-conflict", response_model=ConflictResolveResponse)
async def resolve_conflict(
    pr_number: int,
    request: ConflictResolveRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Resolve conflicts for a stale PR by rebasing and regenerating files.

    Resets the feature branch to the latest base branch, re-runs the
    FileLocator → ScriptGen → GitOps pipeline, and commits atomically.
    Existing workflow records are updated with the new commit SHA.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Path Parameters:
        - pr_number: PR number to resolve conflicts for

    Request Body:
        - git_repository: GitHub repository in 'owner/repo' format

    Response:
        - status: success or error
        - pr_number: PR number
        - git_repository: Repository
        - feature_branch: Feature branch name
        - base_branch: Base branch name
        - commit_sha: New commit SHA
        - queue_count: Number of queue items regenerated

    Errors:
        - 400: Invalid request or PR is merged
        - 404: No workflow or queue items found for PR
        - 500: Conflict resolve operation failed

    Example:
        POST /api/v1/transaction-queue/pr/165/resolve-conflict
        {
            "git_repository": "Regobs/Devlift-Pipeline"
        }
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        result = await service.resolve_conflict(
            pr_number=pr_number,
            git_repository=request.git_repository,
            user_code=user.code,
            tenant_code=tenant.code
        )

        return ConflictResolveResponse(
            status="success",
            pr_number=result['pr_number'],
            git_repository=result['git_repository'],
            feature_branch=result['feature_branch'],
            base_branch=result['base_branch'],
            commit_sha=result.get('commit_sha'),
            queue_count=result.get('queue_count', 0)
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.error(f"Conflict resolve failed for PR #{pr_number}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conflict resolve failed: {str(e)}"
        )


@secure_router.post(
    "/deploy",
    response_model=TransactionQueueDeployResponse,
    summary="Deploy approved queue items",
    # Used by standalone infra deploys (no service_config_code exists for an
    # FGA check) — services go through /deployments/multiple-deploy. Tenant
    # isolation and approval checks are enforced in the handler.
    access=AuthenticationOnly(
        reason="infra_mst is not in the phase-1 FGA model yet — tenant "
        "isolation is enforced in the handler"
    ),
)
async def deploy_queue(
    request: TransactionQueueDeployRequest = None,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Deploy all pending queue items as a single PR.

    For tenants that use the Temporal/Atlantis flow (configured via TEMPORAL_DEPLOY_TENANTS),
    this starts a DeploymentWorkflow and returns immediately with status="queued" and a
    workflow_id for tracking. The workflow handles locking, PR creation, plan/apply, and merge.

    For all other tenants, the existing synchronous deploy_all path is used.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Request Body (optional):
        - environment: Only deploy items for this environment
        - item_ids: Only deploy specific item IDs

    Errors:
        - 400: No pending items to deploy
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    from app.core.config import settings

    user, tenant = current_user_tenant
    request = request or TransactionQueueDeployRequest()

    # No approval-seal validation here: this route's rows (infra / auto-deploy)
    # are born APPROVED and never carry a seal. The deploy services themselves
    # filter to the caller's tenant and APPROVED status.
    _logger.info(
        f"[DEPLOY] tenant={tenant.code} temporal_enabled={settings.temporal_enabled} "
        f"temporal_deploy_tenants_list={settings.temporal_deploy_tenants_list} "
        f"item_ids={request.item_ids} environment={request.environment}"
    )

    # ── Temporal/Atlantis flow ────────────────────────────────────────────────
    if settings.temporal_enabled:
        svc = TransactionQueueService(db)
        return await svc.deploy_temporal(
            user_code=user.code,
            tenant_code=tenant.code,
            environment=request.environment if request else None,
            item_ids=request.item_ids if request else None,
        )

    # ── Standard synchronous flow (temporal_enabled=False) ───────────────────
    _logger.info(f"[DEPLOY] Taking sync path — temporal_enabled={settings.temporal_enabled}")
    service = TransactionQueueService(db)
    response = await service.deploy_all(
        user_code=user.code,
        user_email=user.email_id,
        tenant_code=tenant.code,
        environment=request.environment,
        item_ids=request.item_ids,
    )

    if response.status == "error" and response.items_deployed == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=response.error or "Deployment failed",
        )

    return response


@router.get("/deploy-status/{workflow_id}")
async def get_deploy_status(
    workflow_id: str,
    _: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Query the current state of a Temporal DeploymentWorkflow.

    Returns the workflow step, PR number, locks_granted flag, and result.
    Only available when TEMPORAL_ENABLED=true.
    """
    from app.core.config import settings
    from app.temporal.client import get_temporal_client, workflow_query_http_error
    from app.temporal.workflows.deployment_workflow import DeploymentWorkflow

    if not settings.temporal_enabled:
        raise HTTPException(status_code=400, detail="Temporal is not enabled")

    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        state = await handle.query(DeploymentWorkflow.get_state)
        return {"workflow_id": workflow_id, **state}
    except Exception as e:
        # Same rule as the orchestrator's status route: only NOT_FOUND is a 404.
        raise workflow_query_http_error(e, workflow_id)


@router.post("/temporal-signal/{workflow_id}/{signal_name}")
async def send_temporal_signal(
    workflow_id: str,
    signal_name: str,
    _: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Send a signal to a running DeploymentWorkflow.

    Supported signals:
      plan_completed, plan_failed, apply_completed, apply_failed, pr_merged

    Called by the GitHub webhook handler when Atlantis posts a plan/apply comment,
    or triggered manually for testing.
    """
    from app.core.config import settings
    from app.temporal.client import get_temporal_client

    ALLOWED_SIGNALS = {
        "plan_completed", "plan_failed",
        "apply_completed", "apply_failed",
        "pr_merged",
    }
    if signal_name not in ALLOWED_SIGNALS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown signal '{signal_name}'. Allowed: {sorted(ALLOWED_SIGNALS)}",
        )

    if not settings.temporal_enabled:
        raise HTTPException(status_code=400, detail="Temporal is not enabled")

    client = await get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.signal(signal_name)
        return {"workflow_id": workflow_id, "signal": signal_name, "status": "sent"}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Workflow not found or signal failed: {e}")


@router.get("/prs", response_model=TransactionQueuePRListResponse)
async def list_queue_prs(
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    List PRs created from the deploy queue.

    Returns all PRs created by the user via the deploy queue feature,
    grouped with item counts and status.

    Syncs PR status from GitHub to ensure accurate state.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Response:
        - items: List of PRs with metadata
        - total: Total count of PRs

    Example:
        GET /api/v1/gitops-queue/prs
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    from app.repository.transaction_queue_repository import TransactionQueueRepository
    repo = TransactionQueueRepository(db)

    pr_list = await repo.get_prs_for_user(
        user_code=user.code,
        tenant_code=tenant.code
    )

    # Sync PR status from GitHub for PRs that are still marked as open
    items = []
    for pr in pr_list:
        pr_status = pr["status"]
        is_stale = False

        # For PRs marked as open in DB, check actual GitHub status
        if pr_status == "pr_raised":
            try:
                github_status = await service.sync_pr_status_from_github(
                    pr["pr_number"],
                    git_repository=pr.get("git_repository"),
                    pr_url=pr.get("pr_url")
                )
                if github_status:
                    pr_status = github_status.get("status", pr_status)
                    is_stale = github_status.get("is_stale", False)
            except Exception as e:
                # Log but don't fail - use cached status
                import logging
                logging.getLogger(__name__).warning(f"Failed to sync PR #{pr['pr_number']} status: {e}")

        items.append(
            TransactionQueuePRListItem(
                pr_number=pr["pr_number"],
                pr_url=pr["pr_url"],
                git_branch=pr["git_branch"],
                status=pr_status,
                items_count=pr["items_count"],
                environments=pr["environments"],
                infra_types=pr["infra_types"],
                items=[
                    TransactionQueueItemResponse.model_validate(item)
                    for item in pr.get("items", [])
                ],
                created_at=pr["created_at"],
                user_name=user.name,
                is_stale=is_stale
            )
        )

    return TransactionQueuePRListResponse(
        items=items,
        total=len(items)
    )


@router.get("/pr/{pr_number}/status", response_model=TransactionQueuePRStatusResponse)
async def get_pr_status(
    pr_number: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Check if a PR is stale (behind base branch).

    Compares the PR's feature branch with the base branch to determine
    if it needs to be refreshed due to conflicts.

    Security:
        - JWT authentication required

    Path Parameters:
        - pr_number: PR number to check

    Response:
        - is_stale: True if PR needs refresh
        - behind_by: Number of commits behind base
        - ahead_by: Number of commits ahead of base
        - status: Branch comparison status

    Errors:
        - 404: No queue items found for PR

    Example:
        GET /api/v1/gitops-queue/pr/123/status
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        status_response = await service.check_pr_status(pr_number)
        return status_response

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/pr/{pr_number}/refresh", response_model=TransactionQueuePRRefreshResponse)
async def refresh_pr(
    pr_number: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Refresh a stale PR by regenerating from snapshots.

    Resets the feature branch to the latest base branch and regenerates
    all HCL files from the stored config snapshots. This resolves
    conflicts without changing the intended configuration.

    The key insight: we regenerate from SNAPSHOTS, not current DB values.
    This ensures the deployed config matches what was originally queued.

    Security:
        - JWT authentication required

    Path Parameters:
        - pr_number: PR number to refresh

    Response:
        - status: success or error
        - commit_sha: New commit SHA
        - items_regenerated: Count of items regenerated

    Errors:
        - 404: No queue items found for PR
        - 500: Refresh operation failed

    Example:
        POST /api/v1/gitops-queue/pr/123/refresh
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    try:
        response = await service.refresh_pr(pr_number)

        if response.status == "error":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=response.error or "Refresh failed"
            )

        return response

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/pr/{pr_number}/merged", status_code=status.HTTP_200_OK)
async def mark_pr_merged(
    pr_number: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Mark all queue items for a PR as merged.

    Called when a PR is merged (can be triggered by webhook or manually).

    Security:
        - JWT authentication required

    Path Parameters:
        - pr_number: PR number

    Response:
        - updated_count: Number of items updated

    Example:
        POST /api/v1/gitops-queue/pr/123/merged
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    count = await service.update_pr_merged(pr_number)

    return {"updated_count": count, "status": "pr_merged"}


@router.post("/pr/{pr_number}/closed", status_code=status.HTTP_200_OK)
async def mark_pr_closed(
    pr_number: int,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Mark all queue items for a PR as closed (without merge).

    Called when a PR is closed without being merged.

    Security:
        - JWT authentication required

    Path Parameters:
        - pr_number: PR number

    Response:
        - updated_count: Number of items updated

    Example:
        POST /api/v1/gitops-queue/pr/123/closed
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    count = await service.update_pr_closed(pr_number)

    return {"updated_count": count, "status": "pr_closed"}


@router.post("/bulk-approve", response_model=BulkApproveResponse, status_code=status.HTTP_200_OK)
async def bulk_approve_queue_items(
    request: BulkApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user_tenant: tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
):
    """
    Bulk approve queue items by their IDs.

    Updates the status of multiple transaction queue items to APPROVED.
    Only items belonging to the authenticated user and tenant will be updated.

    Security:
        - JWT authentication required
        - User can only approve their own queue items
        - Tenant isolation enforced

    Request Body:
        - queue_ids: List of queue item IDs to approve

    Response:
        - status: Operation status (success or partial)
        - updated_count: Number of items successfully updated
        - requested_count: Number of items requested to update
        - message: Success or informational message

    Errors:
        - 400: Empty queue_ids list
        - 401: Authentication required

    Example:
        POST /api/v1/transaction-queue/bulk-approve
        {
            "queue_ids": [1, 2, 3, 4, 5]
        }

    Response:
        {
            "status": "success",
            "updated_count": 5,
            "requested_count": 5,
            "message": "Successfully approved 5 queue items"
        }
    """
    user, tenant = current_user_tenant
    service = TransactionQueueService(db)

    updated_count = await service.bulk_approve(
        queue_ids=request.queue_ids,
        user_code=user.code,
        tenant_code=tenant.code
    )

    requested_count = len(request.queue_ids)

    if updated_count == requested_count:
        return BulkApproveResponse(
            status="success",
            updated_count=updated_count,
            requested_count=requested_count,
            message=f"Successfully approved {updated_count} queue items"
        )
    else:
        return BulkApproveResponse(
            status="partial",
            updated_count=updated_count,
            requested_count=requested_count,
            message=f"Approved {updated_count} of {requested_count} requested items. Some items may not exist or belong to a different user/tenant."
        )
