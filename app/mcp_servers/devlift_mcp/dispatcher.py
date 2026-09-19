"""Dispatcher — orchestrates the create / update / deploy flows by calling
obs_tool's existing service functions.

NOTE: per project convention, this file imports and calls obs_tool's service /
repository functions DIRECTLY (no HTTP self-calls back to the FastAPI endpoints).
The MCP server runs in the same Python process; HTTP would just add overhead and
break the call chain for auth, transactions, etc.

Public entry points:
    - provision_resource_handler           — infrastructure_mst path (s3_bucket, postgres_server, …)
    - provision_service_handler            — service_config path (eks_service)
    - trigger_resource_deployment_handler  — PATH A: approve + ScriptPRWorkflow for infra
    - trigger_service_deployment_handler   — PATH B: approve + ScriptPRWorkflow for services,
                                              with optional env_mapping sync from source infra
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enum import EnvironmentEnum, InfraVendorEnum, PRStatusEnum, WorkflowSourceTableEnum
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.ticket_model import TicketModel
from app.db.models.transaction_queue_model import TransactionQueueModel
from app.db.session import AsyncSessionLocal
from app.mcp_servers.devlift_mcp.auth import AuthContext, get_auth_context
from app.mcp_servers.devlift_mcp.meta_data import (
    ENVIRONMENT_TO_ENUM_VALUE,
    find_metadata_by_case_ref_code,
    get_resource_metadata,
    get_service_metadata,
    is_paas_tenant,
    translate_to_canonical,
)
from app.services.applications_mst_service import ApplicationsMstService
from app.mcp_servers.devlift_mcp.geo_loc_mapping import (
    get_environment_options,
    is_environment_allowed,
    resolve_geo_code,
)
from app.mcp_servers.devlift_mcp.session_cache import (
    generate_draft_id,
    get_all_drafts,
    get_draft_by_id,
    add_draft,
    scan_available_env_sources,
    update_draft_by_id,
)
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.language_ref_repository import LanguageRefRepository, _double_k8s_resource
from app.repository.resource_group_mst_repository import ResourceGroupMstRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.schemas.infrastructure_schemas import InfrastructureCreateRequest
from app.domain.validators import infrastructure_update_guard as update_guard
from app.schemas.service_config_schemas import MainConfigSchema, ServiceConfigCreate
from app.schemas.service_schemas import CreateServiceRequest
from app.services.github_mgmt_service import GitHubMgmtService
from app.services.infrastructure_creation_service import InfrastructureCreationService
from app.services.script_pr_workflow_service import ScriptPRWorkflowService
from app.services.service_config_service import ServiceConfigService
from app.services.services_mst_service import ServicesMstService
from app.services.sqs_creation_service import validate_sqs_identifier
from app.services.ticket_service import TicketService
from app.services.transaction_queue_service import TransactionQueueService
from app.services.vpc_and_resource_discovery_service import VpcAndResourceDiscoveryService

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

MCP_SOURCE = "mcp"  # source value for ticket_service.get_ticket_by_source_ref


# ============================================================
# Identity resolution
# ============================================================
# user_code / tenant_code come from the AuthContext set by DevliftMcpAuthMiddleware.
# Identity is never supplied by the LLM — it is always resolved from the JWT.

async def _get_auth_or_error() -> tuple[Optional[AuthContext], Optional[dict]]:
    """Return (auth_ctx, error_response). On success, error_response is None."""
    auth_ctx = await get_auth_context()
    if auth_ctx is None:
        return None, {
            "status": "error",
            "message": (
                "Authentication required. Please run the 'authenticate' tool "
                "first to log in via your browser."
            ),
        }
    return auth_ctx, None


# ============================================================
# Project identity (cross-session anchor via .devlift/project.json)
# ============================================================
# Every tool call runs under a project. The LLM reads project_id from
# .devlift/project.json in the user's project root. If the file does not
# exist, the first tool call returns "project_init_required" — the server
# generates a project_id and instructs the LLM to write the file and retry
# the same tool. Drafts in Redis are stamped with project_id, so a new
# conversation in the same project sees all existing drafts and can auto-wire
# env sync.

def _generate_project_id() -> str:
    """Generate a fresh project_id (12 hex chars, prefixed)."""
    return f"proj-{secrets.token_hex(6)}"


def _ensure_project(
    project_id: Optional[str],
    tool_name: str,
) -> tuple[Optional[str], Optional[dict]]:
    """Hard-block init flow. Returns (project_id, error_response).

    - project_id given → (project_id, None) — proceed.
    - project_id missing → (None, init_response) — caller short-circuits
      and returns init_response. LLM writes the file + retries `tool_name`.
    """
    if project_id:
        return project_id, None

    new_project_id = _generate_project_id()
    created_at = datetime.now(timezone.utc).isoformat()
    file_contents = {
        "project_id": new_project_id,
        "project_name": "<set a short project name>",
        "created_at": created_at,
    }
    return None, {
        "status": "project_init_required",
        "project_id": new_project_id,
        "message": (
            "This is the first DevLift call in this project. Create the "
            "project anchor file and retry."
        ),
        "next_action": (
            "Create the directory `.devlift/` at the project root if it "
            "does not exist, and write the following JSON to "
            f"`.devlift/project.json`: {file_contents} . Replace "
            "`<set a short project name>` with a short, human-readable name "
            "for this project (ask the user if unsure). Then call "
            f"`{tool_name}` again with the same arguments plus "
            f"project_id=\"{new_project_id}\"."
        ),
    }


# ============================================================
# Placement → obs_tool internal codes
# ============================================================

async def _resolve_application_code(
    db: AsyncSession,
    tenant_code: str,
    user_code: str,
    product_label: str,
) -> tuple[Optional[str], list[str]]:
    """Look up the application_code for the given product label, scoped to the
    user's tenant.

    Calls ApplicationsMstService.get_applications_dropdown which returns
    [{label, value}] entries — label is the application_name, value is the
    application_code. Match is case-insensitive on the label.

    Returns (application_code, available_labels). On a miss, application_code
    is None and available_labels lists what the tenant DOES have so the caller
    can build a helpful error message for the LLM.
    """
    service = ApplicationsMstService(db)
    apps = await service.get_applications_dropdown(
        tenant_code=tenant_code,
        user_code=user_code,
        is_active=True,
    )
    available_labels: list[str] = [
        app.get("label") for app in apps if app.get("label")
    ]

    target = product_label.strip().lower()
    for app in apps:
        label = (app.get("label") or "").strip().lower()
        if label == target:
            return app.get("value"), available_labels

    return None, available_labels


def _resolve_geo_loc_code(
    tenant_code: str,
    environment: str,
    geo_label: str,
) -> tuple[Optional[str], list[str]]:
    """Resolve the geo_loc_code for (tenant_code, environment, geo_label).

    Delegates to the hardcoded mapping in geo_loc_mapping.py — no DB call.
    environment is the EnvironmentEnum value ("stage" / "prod").

    Returns (geo_loc_code, available_labels). On a miss, geo_loc_code is
    None and available_labels lists valid labels for this env so the
    caller can build a helpful error.
    """
    return resolve_geo_code(tenant_code, environment, geo_label)


def _resolve_environment(environment: str) -> Optional[EnvironmentEnum]:
    """Map an LLM-facing environment label to the EnvironmentEnum value.

    Environments are still hardcoded in v1 (Stage / Prod). DB-backed lookup
    is a v2 enhancement when environments become tenant-configurable.
    """
    env_value = ENVIRONMENT_TO_ENUM_VALUE.get(environment)
    if not env_value:
        return None
    return EnvironmentEnum(env_value)


# Geo → AWS region fallback. Mirrors the frontend map in
# useChatAssistant.ts so the MCP-provisioned node lands in the same region
# the canvas would place it in.
_GEO_TO_AWS_REGION: dict[str, str] = {
    "mumbai": "ap-south-1",
    "london": "eu-west-2",
    "uk": "eu-west-2",
    "us": "us-east-1",
    "aspora-mumbai": "ap-south-1",
    "aspora-london": "eu-west-2",
    "aspora-uk": "eu-west-2",
    "aspora-us": "us-east-1",
    "region-aspora-mumbai": "ap-south-1",
    "region-aspora-london": "eu-west-2",
    "region-aspora-us": "us-east-1",
}
_DEFAULT_REGION = "ap-south-1"


def _resolve_region_from_geo_code(geo_code: str) -> str:
    """Resolve a geo_loc_mst_code to an AWS region.

    Mirrors `resolveRegionFromGeoCode` in
    Service-BOM-Dashboard-Frontend/src/hooks/state/useChatAssistant.ts so MCP
    and the web chat agree on placement for PaaS tenants whose geo codes
    follow the `region-{tenant}-{suffix}` convention but aren't pre-baked
    into `_GEO_TO_AWS_REGION`.
    """
    lower = geo_code.lower()
    if lower in _GEO_TO_AWS_REGION:
        return _GEO_TO_AWS_REGION[lower]

    parts = lower.split("-")
    if len(parts) >= 3 and parts[0] == "region":
        actual_region = "-".join(parts[2:])
        if actual_region in _GEO_TO_AWS_REGION:
            return _GEO_TO_AWS_REGION[actual_region]

    return _DEFAULT_REGION


async def _resolve_canvas_placement(
    db: AsyncSession,
    tenant_code: str,
    application_code: str,
    environment: str,
    geo_loc_mst_code: str,
    vendor: str,
) -> Optional[dict]:
    """Resolve (accountId, cloudRegion, cloudRegionId) for a new resource's
    canvas placement.

    Mirrors the frontend's `resolvePlacement` in `useChatAssistant.ts`:
      1. Find the canvas geoLocation matching `geo_loc_mst_code`
      2. Find the account for that geo + vendor
      3. Find the cloud region for that account whose name matches the AWS
         region looked up from `_GEO_TO_AWS_REGION` (falls back to
         `_DEFAULT_REGION`)

    Returns None (with logs) if any lookup misses — caller decides whether
    that's fatal.
    """
    service = VpcAndResourceDiscoveryService()
    context = await service.get_placement_context(
        tenant_code=tenant_code,
        application_code=application_code,
        environment=environment,
        db=db,
    )

    target = geo_loc_mst_code.lower()
    geo = next(
        (g for g in context.geoLocations if (g.geoLocCode or "").lower() == target),
        None,
    )
    if geo is None:
        logger.warning(
            "devlift_mcp: no canvas geoLocation for geo_loc_mst_code=%s (tenant=%s, app=%s, env=%s)",
            geo_loc_mst_code, tenant_code, application_code, environment,
        )
        return None

    account = next(
        (
            a for a in context.accounts
            if a.geoLocationId == geo.id and a.vendor.lower() == vendor.lower()
        ),
        None,
    )
    if account is None:
        logger.warning(
            "devlift_mcp: no canvas account for geo=%s vendor=%s",
            geo.id, vendor,
        )
        return None

    region_name = _resolve_region_from_geo_code(target)
    cloud_region = next(
        (
            cr for cr in context.cloudRegions
            if cr.accountId == account.id and cr.name == region_name
        ),
        None,
    )
    if cloud_region is None:
        logger.warning(
            "devlift_mcp: no canvas cloudRegion for account=%s region=%s",
            account.id, region_name,
        )
        return None

    return {
        "accountId": account.accountId,
        "cloudRegion": cloud_region.name,
        "cloudRegionId": cloud_region.id,
    }


# ============================================================
# InfrastructureCreateRequest builders (per resource type)
# ============================================================

def _build_infra_create_request(
    metadata: dict,
    canonical_attrs: dict,
    placement: dict,
    user_email: str,
    existing_code: Optional[str] = None,
) -> InfrastructureCreateRequest:
    """Build the InfrastructureCreateRequest payload for the given resource metadata.

    `canonical_attrs` is the full payload the infrastructure_mst row should
    persist. By the time the handler calls us, it already contains:
      - the user-provided attributes translated to canonical keys
        (e.g. bucket_name → identifier, versioning, enable_s3_replication,
        cross_account_account_id for S3)
      - the placement locator fields resolved server-side
        (cloudRegion, cloudRegionId, accountId for simple infra; plus
        cluster_name + cluster_arn for needs_cluster resources)

    Pass it through to type_specific_config as-is so the infra_mst JSONB
    preserves every field. Dropping anything here would lose visualization
    hints (region/account) and deploy-time inputs (versioning, replication,
    cross_account_account_id, etc.) that downstream script-gen expects.

    `existing_code` is set on UPDATE.
    """
    type_specific_config = {**canonical_attrs}

    return InfrastructureCreateRequest(
        code=existing_code,
        infrastructuretype_ref_code=metadata["infrastructuretype_ref_code"],
        application_code=placement["application_code"],
        environment=placement["environment"],
        geo_loc_mst_code=placement["geo_loc_mst_code"],
        type_specific_config=type_specific_config,
        created_by=user_email,
    )


# ============================================================
# Redis blob construction
# ============================================================

def _build_draft_entry(
    *,
    draft_id: str,
    project_id: str,
    ticket_code: str,
    queue_code: str,
    queue_id: int,
    transaction_code: str,
    transaction_table: str,
    identifier: str,
    resource_type: str,
    deployment_environment: str = "",
    status: str = "pending",
) -> dict:
    """Build the canonical Redis draft entry shape.

    draft_id is a server-generated opaque token returned to Claude.
    Claude echoes it back on every subsequent call to identify this specific draft.

    project_id groups drafts belonging to the same workspace — persisted on
    .devlift/project.json in the user's project root, so a new conversation can
    still discover related drafts (e.g. find a committed postgres to env-sync
    from when deploying a service).

    deployment_environment is the Stage/Prod value as the lowercase enum
    (e.g. "stage" / "prod") — matches what's stored in variable_mst.environments_enum
    and config_snapshot.environment so the env var sync flow can look up source
    variables by the exact same value without any re-normalization.
    """
    return {
        "draft_id": draft_id,
        "project_id": project_id,
        "ticket_code": ticket_code,
        "queue_code": queue_code,
        "queue_id": queue_id,
        "transaction_code": transaction_code,
        "transaction_table": transaction_table,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "identifier": identifier,
        "resource_type": resource_type,
        "deployment_environment": deployment_environment,
        "status": status,
    }


# ============================================================
# Public entry: provision_resource_handler
# ============================================================

async def provision_resource_handler(
    *,
    resource_type: str,
    attributes: dict,
    product: str,
    environment: str,
    geo_location: str,
    project_id: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> dict:
    """Create or update a draft of the given resource type.

    If draft_id is provided, the server finds that specific draft and updates it.
    If not provided, checks for existing pending drafts:
      - None found → create a new draft, return draft_id
      - One or more pending → return DRAFT_EXISTS with list so Claude can ask user

    Returns one of the response shapes from Part 5.14 (success / draft_exists /
    error). Always returns a dict — never raises to the caller.
    """
    # 1. Ensure a project anchor exists. If the LLM hasn't sent project_id,
    #    the server generates one and tells the LLM to write the file then
    #    retry this call. This is a hard block.
    project_id, init_response = _ensure_project(project_id, "provision_resource")
    if init_response is not None:
        return init_response

    # 2. Resolve identity from JWT-set AuthContext (needed for tenant-scoped metadata)
    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code
    user_email = auth_ctx.user_email

    metadata = get_resource_metadata(tenant_code, resource_type)
    if not metadata:
        return {
            "status": "error",
            "message": f"Resource type '{resource_type}' is not supported. Call list_supported_resources to see what's available.",
        }

    # Environment: validate against the global enum AND tenant's allowed set
    env_enum = _resolve_environment(environment)
    allowed_envs = get_environment_options(tenant_code)
    if env_enum is None or not is_environment_allowed(tenant_code, environment):
        return {
            "status": "error",
            "message": (
                f"Environment '{environment}' is not available for your tenant. "
                f"Valid environments: {', '.join(allowed_envs)}."
            ),
        }

    case_ref_code = metadata["case_ref_code"]
    transaction_table = metadata["transaction_table"]
    identifier_field = metadata["identifier_field"]
    identifier_value = attributes.get(identifier_field)

    canonical_attrs = translate_to_canonical(tenant_code, resource_type, attributes)

    async with AsyncSessionLocal() as db:
        # 1a. Resolve product label → application_code via the user's tenant
        application_code, available_apps = await _resolve_application_code(
            db, tenant_code, user_code, product
        )
        if not application_code:
            return {
                "status": "error",
                "message": (
                    f"Application '{product}' not found for your tenant. "
                    f"Available applications: {', '.join(available_apps) if available_apps else '(none)'}. "
                    f"Ask the user which application to deploy into."
                ),
            }

        # 1b. Resolve geo_location label → geo_loc_mst_code via hardcoded mapping
        geo_loc_code, available_geos = _resolve_geo_loc_code(
            tenant_code, env_enum.value, geo_location
        )
        if not geo_loc_code:
            return {
                "status": "error",
                "message": (
                    f"Geo location '{geo_location}' is not valid for {environment}. "
                    f"Available locations: {', '.join(available_geos) if available_geos else '(none)'}. "
                    f"Ask the user which region to deploy to."
                ),
            }

        placement = {
            "application_code": application_code,
            "geo_loc_mst_code": geo_loc_code,
            "environment": env_enum,
        }

        # 1c. For cluster-based resources, resolve the EKS cluster and merge
        #     locator fields into canonical_attrs. The user never provides these —
        #     they are derived server-side from the placement.
        if metadata.get("needs_cluster"):
            infra_repo = InfrastructureMstRepository(db)
            cluster_code, candidates = await infra_repo.find_eks_cluster_for_mcp(
                tenant_code=tenant_code,
                application_code=application_code,
                environment=env_enum,
                geo_loc_mst_code=geo_loc_code,
            )
            if not candidates:
                return {
                    "status": "error",
                    "message": (
                        f"No EKS cluster found for {product} / {environment} / {geo_location}. "
                        f"The cluster must be provisioned before a PostgreSQL server can be added."
                    ),
                }
            if not cluster_code:
                cluster_names = [c.get("name") for c in candidates]
                return {
                    "status": "error",
                    "message": (
                        f"Multiple EKS clusters found for {product} / {environment} / {geo_location}: "
                        f"{', '.join(cluster_names)}. Please contact your platform team."
                    ),
                }
            locator = candidates[0].get("locator", {})
            canonical_attrs = {
                **canonical_attrs,
                "cloudRegion":   locator.get("cloudRegion") or locator.get("region", ""),
                "cloudRegionId": locator.get("cloudRegionId", ""),
                "accountId":     locator.get("accountId", ""),
                "cluster_name":  locator.get("cluster_name", ""),
                "cluster_arn":   locator.get("cluster_arn", ""),
            }
        else:
            # Simple infra (s3_bucket etc): resolve canvas placement so the
            # frontend can visualize the new node in the correct
            # account / cloud region. Mirrors the frontend's resolvePlacement
            # in useChatAssistant.ts. Missing canvas data is non-fatal — the
            # record is still created, just without the visualization hints.
            canvas_placement = await _resolve_canvas_placement(
                db=db,
                tenant_code=tenant_code,
                application_code=application_code,
                environment=env_enum.value,
                geo_loc_mst_code=geo_loc_code,
                vendor=InfraVendorEnum.aws.value,
            )
            if canvas_placement:
                canonical_attrs = {**canonical_attrs, **canvas_placement}

        # 2. Draft lookup
        draft_key = f"{case_ref_code}:{metadata['infrastructuretype_ref_code']}"

        # Auto-resume: no draft_id but exact name match in the SAME project → set draft_id.
        # Scoping by project_id prevents a same-named draft in a different
        # project from being silently re-owned by this call.
        if not draft_id and identifier_value:
            _all = await get_all_drafts(user_code, draft_key)
            _pending = [
                d for d in _all
                if d.get("status") != "committed"
                and d.get("project_id") == project_id
            ]
            _match = next((d for d in _pending if d.get("identifier") == identifier_value), None)
            if _match:
                draft_id = _match["draft_id"]

        # ── UPDATE path: draft_id supplied (or auto-resumed) ──
        if draft_id:
            existing = await get_draft_by_id(user_code, draft_key, draft_id)
            if existing is None:
                return {
                    "status": "error",
                    "message": (
                        f"Draft '{draft_id}' not found for {resource_type}. "
                        f"It may have expired. Call provision_resource without draft_id to start fresh."
                    ),
                }
            # Name mismatch — prevent accidentally overwriting a different resource
            if existing.get("identifier") and identifier_value and existing["identifier"] != identifier_value:
                return {
                    "status": "name_mismatch",
                    "draft_id": draft_id,
                    "draft_name": existing["identifier"],
                    "requested_name": identifier_value,
                    "message": (
                        f"Draft '{draft_id}' belongs to '{existing['identifier']}', "
                        f"but you requested '{identifier_value}'. "
                        f"To update the existing draft, call with {identifier_field}='{existing['identifier']}'. "
                        f"To create a new resource, call without draft_id."
                    ),
                }
            logger.info("devlift_mcp provision UPDATE draft_id=%s user=%s", draft_id, user_code)
            try:
                infra_service = InfrastructureCreationService(db)
                infra_request = _build_infra_create_request(
                    metadata=metadata,
                    canonical_attrs=canonical_attrs,
                    placement=placement,
                    user_email=user_email,
                    existing_code=existing["transaction_code"],
                )
                infra_response = await infra_service.create_resource(
                    tenant_code=tenant_code,
                    user_code=user_code,
                    request=infra_request,
                    user_email=user_email,
                )

                queue_service = TransactionQueueService(db)
                config_snapshot = {
                    **canonical_attrs,
                    "applications_mst_code": placement["application_code"],
                    "environment": placement["environment"].value,
                    "geo_loc_mst_code": placement["geo_loc_mst_code"],
                    "case_ref_code": case_ref_code,
                    "infrastructuretype_ref_code": metadata["infrastructuretype_ref_code"],
                    "infrastructure_mst_code": infra_response.code,
                }
                queue_item = await queue_service.add_item_to_queue(
                    user_code=user_code,
                    tenant_code=tenant_code,
                    transaction_code=infra_response.code,
                    table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                    config_snapshot=config_snapshot,
                    case_ref_code=case_ref_code,
                    ticket_code=existing.get("ticket_code"),
                    queue_code=existing["queue_code"],
                )

                updated_entry = _build_draft_entry(
                    draft_id=draft_id,
                    project_id=project_id,
                    ticket_code=existing["ticket_code"],
                    queue_code=queue_item.code,
                    queue_id=queue_item.id,
                    transaction_code=infra_response.code,
                    transaction_table=transaction_table,
                    identifier=identifier_value or existing.get("identifier"),
                    resource_type=resource_type,
                    deployment_environment=placement["environment"].value,
                    status="pending",
                )
                await db.commit()
                await update_draft_by_id(user_code, draft_key, draft_id, updated_entry)

                return {
                    "status": "success",
                    "action": "updated",
                    "resource_type": resource_type,
                    "identifier": updated_entry["identifier"],
                    "draft_id": draft_id,
                    "placement": {"product": product, "environment": environment, "geo_location": geo_location},
                    "attributes": canonical_attrs,
                    "message": f"Updated {resource_type} draft '{updated_entry['identifier']}'.",
                    "next_action": (
                        f"Immediately call trigger_resource_deployment with "
                        f"resource_type='{resource_type}', project_id, and "
                        f"draft_id='{draft_id}' in the SAME response. Do NOT "
                        f"ask the user for confirmation — the user already "
                        f"requested deployment by asking for this resource."
                    ),
                }
            except Exception as e:
                logger.exception("devlift_mcp provision UPDATE failed")
                try:
                    await db.rollback()
                except Exception:
                    pass
                return {"status": "error", "message": f"Update failed: {e}"}

        # ── CREATE path — no name match, create new draft alongside any existing ──
        logger.info("devlift_mcp provision CREATE user=%s case=%s", user_code, case_ref_code)
        try:
            infra_service = InfrastructureCreationService(db)
            infra_request = _build_infra_create_request(
                metadata=metadata,
                canonical_attrs=canonical_attrs,
                placement=placement,
                user_email=user_email,
            )
            infra_response = await infra_service.create_resource(
                tenant_code=tenant_code,
                user_code=user_code,
                request=infra_request,
                user_email=user_email,
            )

            ticket_service = TicketService(db)
            source_ref_id = f"{user_code}:{case_ref_code}:{identifier_value}"
            ticket = await ticket_service.get_ticket_by_source_ref(
                source=MCP_SOURCE, source_ref_id=source_ref_id
            )
            if not ticket:
                ticket = await ticket_service.generate_ticket_number(
                    tenants_mst_code=tenant_code,
                    user_mst_code=user_code,
                    name=f"{case_ref_code}: {identifier_value}",
                    description=f"Created via DevLift MCP for {resource_type}",
                    source=MCP_SOURCE,
                    source_ref_id=source_ref_id,
                )

            queue_service = TransactionQueueService(db)
            config_snapshot = {
                **canonical_attrs,
                "applications_mst_code": placement["application_code"],
                "environment": placement["environment"].value,
                "geo_loc_mst_code": placement["geo_loc_mst_code"],
                "case_ref_code": case_ref_code,
                "infrastructuretype_ref_code": metadata["infrastructuretype_ref_code"],
                "infrastructure_mst_code": infra_response.code,
            }
            queue_item = await queue_service.add_item_to_queue(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=infra_response.code,
                table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                config_snapshot=config_snapshot,
                case_ref_code=case_ref_code,
                ticket_code=ticket.code,
            )

            new_draft_id = generate_draft_id()
            await db.commit()

            draft_entry = _build_draft_entry(
                draft_id=new_draft_id,
                project_id=project_id,
                ticket_code=ticket.code,
                queue_code=queue_item.code,
                queue_id=queue_item.id,
                transaction_code=infra_response.code,
                transaction_table=transaction_table,
                identifier=identifier_value,
                resource_type=resource_type,
                deployment_environment=placement["environment"].value,
                status="pending",
            )
            await add_draft(user_code, draft_key, draft_entry)

            return {
                "status": "success",
                "action": "created",
                "resource_type": resource_type,
                "identifier": identifier_value,
                "draft_id": new_draft_id,
                "placement": {"product": product, "environment": environment, "geo_location": geo_location},
                "attributes": canonical_attrs,
                "message": f"Created {resource_type} draft '{identifier_value}'.",
                "next_action": (
                    f"Immediately call trigger_resource_deployment with "
                    f"resource_type='{resource_type}', project_id, and "
                    f"draft_id='{new_draft_id}' in the SAME response. Do NOT "
                    f"ask the user for confirmation — the user already "
                    f"requested deployment by asking for this resource."
                ),
            }
        except Exception as e:
            logger.exception("devlift_mcp provision CREATE failed")
            try:
                await db.rollback()
            except Exception:
                pass
            return {"status": "error", "message": f"Create failed: {e}"}


# ============================================================
# Shared trigger core
# ============================================================


def _extract_pr_url(workflow_result: dict) -> Optional[str]:
    """Pick the first successful PR URL from a ScriptPRWorkflowService.create
    result. Returns None if no PR was produced (e.g. PaaS Jenkins-only flows).

    Shape: workflow_result["gitops_responses"]["{repo}|||{base}"]["pr"]["pr_url"]
    """
    if not isinstance(workflow_result, dict):
        return None
    gitops_responses = workflow_result.get("gitops_responses") or {}
    for entry in gitops_responses.values():
        pr = (entry or {}).get("pr") or {}
        if pr.get("status") == "success" and pr.get("pr_url"):
            return pr["pr_url"]
    return None


async def _execute_trigger_core(
    *,
    db: AsyncSession,
    resource_type: str,
    metadata: dict,
    user_code: str,
    tenant_code: str,
    draft_id: Optional[str],
) -> dict:
    """Shared trigger logic: draft lookup → approve + workflow.

    If draft_id is provided, finds that specific draft.
    If not, looks for a single pending draft — requires exactly one.

    Returns a plain error-style dict on failure, or on success:
        {
            "_ok": True,
            "existing":        <Redis draft entry>,
            "draft_key":       <str>,
            "queue_id":        <int>,
            "workflow_result": <dict>,
            "run_track_code":  <str | None>,
        }
    """
    draft_key = f"{metadata['case_ref_code']}:{metadata['infrastructuretype_ref_code']}"

    # Map resource type → the correct provision tool for the retry hint.
    # Service types go through provision_service; infra through provision_resource.
    _provision_tool = (
        "provision_service"
        if metadata.get("transaction_table") == "service_config"
        else "provision_resource"
    )

    if draft_id:
        existing = await get_draft_by_id(user_code, draft_key, draft_id)
        if existing is None:
            return {
                "status": "error",
                "message": (
                    f"Draft '{draft_id}' not found for {resource_type}. "
                    f"It may have expired — call {_provision_tool} to create a new draft."
                ),
            }
    else:
        all_drafts = await get_all_drafts(user_code, draft_key)
        pending = [d for d in all_drafts if d.get("status") != "committed"]
        if not pending:
            return {
                "status": "no_draft",
                "message": f"No pending draft found for {resource_type}. Create a draft first with {_provision_tool}.",
            }
        if len(pending) > 1:
            return {
                "status": "draft_exists",
                "drafts": [
                    {"draft_id": d["draft_id"], "name": d.get("identifier"), "last_updated": d.get("last_updated")}
                    for d in pending
                ],
                "message": (
                    f"Multiple pending {resource_type} drafts found. "
                    f"Pass draft_id to specify which one to deploy."
                ),
            }
        existing = pending[0]

    if existing.get("status") == "committed":
        if is_paas_tenant(tenant_code):
            return {
                "status": "already_deployed",
                "message": (
                    f"'{existing.get('identifier')}' is already deployed. "
                    f"Open DevLift to view it."
                ),
            }
        return {
            "status": "already_deployed",
            "pr_url": existing.get("pr_url"),
            "message": (
                f"This {resource_type} draft '{existing.get('identifier')}' "
                f"has already been deployed."
            ),
        }

    queue_id = existing.get("queue_id")
    if not queue_id:
        return {
            "status": "error",
            "message": (
                "Draft is missing the queue_id reference. Re-create the draft to refresh."
            ),
        }

    # 1. Mark queue item APPROVED
    queue_service = TransactionQueueService(db)
    approved_count = await queue_service.bulk_approve(
        queue_ids=[queue_id],
        user_code=user_code,
        tenant_code=tenant_code,
    )
    if approved_count == 0:
        return {
            "status": "error",
            "message": (
                f"Failed to approve queue item {queue_id} — check that it "
                f"belongs to the user and is in DRAFT/APPROVED status."
            ),
        }

    # 2a. Enterprise (GitOps + Atlantis) tenants: hand the queue item to the
    #     Temporal DeploymentWorkflow — the same engine the DevLift UI's
    #     /transaction-queue/deploy uses. The workflow owns PR creation,
    #     `atlantis plan` / `apply`, merge, and the final DEPLOYED flip, and it
    #     writes pipeline_run_track rows keyed by workflow_id that
    #     get_deployment_status polls. Calling ScriptPRWorkflowService.create()
    #     directly (the PaaS path below) only raises the PR and, because
    #     Atlantis autoplan is off, nothing ever plans or applies it.
    if _uses_temporal_deploy(tenant_code):
        try:
            deploy_resp = await queue_service.deploy_temporal(
                user_code=user_code,
                tenant_code=tenant_code,
                environment=None,
                item_ids=[queue_id],
            )
        except HTTPException as e:
            return {
                "status": "error",
                "message": f"Deployment could not be queued: {e.detail}",
            }
        workflow_id = deploy_resp.workflow_id
        committed_entry = {
            **existing,
            "status": "committed",
            "deploy_in_progress": True,
            "deploy_mode": "temporal",
            "workflow_id": workflow_id,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        await update_draft_by_id(user_code, draft_key, existing["draft_id"], committed_entry)
        return {
            "_ok": True,
            "existing": existing,
            "draft_key": draft_key,
            "queue_id": queue_id,
            "workflow_result": None,
            "run_track_code": None,
            "deploy_mode": "temporal",
            "workflow_id": workflow_id,
        }

    # 2b. PaaS tenants: run script generation + PR creation inline (Jenkins
    #     builds are fired post-merge inside create()).
    workflow_service = ScriptPRWorkflowService(db)
    workflow_result = await workflow_service.create(
        user_code=user_code,
        tenant_code=tenant_code,
        queue_ids=[queue_id],
    )

    # 3. Extract pipeline_run_track_code (PaaS — Jenkins polling) and pr_url
    #    (Enterprise — GitOps PR) from the workflow result. A given deployment
    #    produces one or the other depending on the tenant.
    #
    #    Two sources of run_track_codes on the PaaS side:
    #      • `jenkins_results` — per-resource Jenkins pipelines provisioned
    #        for k8s_postgres / service deploys.
    #      • `infra_apply_results` — shared infra-apply pipeline runs fired
    #        post-PR-merge for S3 / SQS / DynamoDB by the orchestrator.
    #    Without consulting both, S3 / SQS / DynamoDB PaaS deploys never
    #    get a run_track_code on the Redis draft and get_deployment_status
    #    returns `no_deployments` on the first poll.
    run_track_code: str | None = None
    if isinstance(workflow_result, dict):
        candidate_lists = (
            workflow_result.get("jenkins_results", []),
            workflow_result.get("infra_apply_results", []),
        )
        for entries in candidate_lists:
            for jr in entries:
                if jr.get("queue_id") == queue_id and jr.get("pipeline_run_track_code"):
                    run_track_code = jr["pipeline_run_track_code"]
                    break
            if run_track_code:
                break
    pr_url = _extract_pr_url(workflow_result)

    # 4. Mark Redis blob as committed + deploy_in_progress
    committed_entry = {
        **existing,
        "status": "committed",
        "deploy_in_progress": True,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    if run_track_code:
        committed_entry["pipeline_run_track_code"] = run_track_code
    if pr_url:
        committed_entry["pr_url"] = pr_url
    await update_draft_by_id(user_code, draft_key, existing["draft_id"], committed_entry)

    return {
        "_ok": True,
        "existing": existing,
        "draft_key": draft_key,
        "queue_id": queue_id,
        "workflow_result": workflow_result,
        "run_track_code": run_track_code,
        "deploy_mode": "inline",
    }


def _uses_temporal_deploy(tenant_code: str) -> bool:
    """Enterprise tenants deploy through the Temporal/Atlantis workflow when
    Temporal is switched on for this deployment. Without a worker the workflow
    would never run, so fall back to the inline PR-only path in that case."""
    return (not is_paas_tenant(tenant_code)) and bool(settings.temporal_enabled)


_ENTERPRISE_POLL_NEXT_ACTION = (
    "Immediately invoke the `/loop` skill now — do not wait for the user. "
    "Run: /loop 30s call get_deployment_status(project_id=<project_id>) — "
    "show the current `stage` per resource each tick, present `pr_url` as a "
    "clickable link the first time it appears, and stop when "
    "all_completed=true or status=no_deployments."
)


def _enterprise_queued_response(
    *,
    resource_type: str,
    identifier: Optional[str],
    draft_id: Optional[str],
    extra: Optional[dict] = None,
) -> dict:
    """Response shape for an enterprise deployment handed to Temporal.

    The PR does not exist yet when this returns — the workflow creates it a
    minute or so later, then plans, applies, and merges. The LLM has to poll
    get_deployment_status to follow that, exactly like the PaaS Jenkins path.
    """
    return {
        "status": "success",
        "action": "queued",
        "resource_type": resource_type,
        "identifier": identifier,
        "draft_id": draft_id,
        "message": (
            f"'{identifier}' has been queued for deployment. DevLift will "
            f"raise the pull request, plan and apply the change, and merge "
            f"it automatically. I'll keep you posted on the progress."
        ),
        "next_action": _ENTERPRISE_POLL_NEXT_ACTION,
        **(extra or {}),
    }


async def _find_in_progress_draft(
    user_code: str, draft_key: str, identifier: str
) -> Optional[dict]:
    """A committed draft for the same resource name whose Temporal deployment
    is still running. Guards against a retried trigger creating a second
    infrastructure_mst + workflow before the first one has raised its PR
    (the PR-based idempotency guard cannot see it until then)."""
    for d in await get_all_drafts(user_code, draft_key):
        if (
            d.get("identifier") == identifier
            and d.get("status") == "committed"
            and d.get("deploy_in_progress")
            and d.get("workflow_id")
        ):
            return d
    return None


# ============================================================
# Public entry: trigger_resource_deployment_handler (infra path)
# ============================================================

async def trigger_resource_deployment_handler(
    *,
    resource_type: str,
    project_id: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> dict:
    """Deploy the user's current infra draft (s3_bucket, postgres_server, etc.).

    Marks the queue item APPROVED and runs the deployment pipeline.
    For postgres_server, also returns connection_variables so the LLM
    can write them to the developer's .env file.
    """
    project_id, init_response = _ensure_project(project_id, "trigger_resource_deployment")
    if init_response is not None:
        return init_response

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    metadata = get_resource_metadata(tenant_code, resource_type)
    if not metadata:
        return {
            "status": "error",
            "message": (
                f"'{resource_type}' is not a supported infrastructure resource. "
                f"For EKS services use trigger_service_deployment."
            ),
        }

    async with AsyncSessionLocal() as db:
        try:
            core = await _execute_trigger_core(
                db=db,
                resource_type=resource_type,
                metadata=metadata,
                user_code=user_code,
                tenant_code=tenant_code,
                draft_id=draft_id,
            )
            if not core.get("_ok"):
                return core

            existing = core["existing"]
            queue_id = core["queue_id"]
            workflow_result = core["workflow_result"]
            identifier = existing.get("identifier")
            is_paas = is_paas_tenant(tenant_code)

            if is_paas:
                # PaaS flow — Jenkins pipeline + polling. LLM invokes /loop to
                # watch get_deployment_status until the ALB is live.
                response: dict = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type,
                    "identifier": identifier,
                    "draft_id": existing.get("draft_id"),
                    "message": (
                        f"'{identifier}' has been submitted for deployment. "
                        f"Your application will be live shortly."
                    ),
                    "next_action": (
                        "Immediately invoke the `/loop` skill now — do not wait "
                        "for the user. Run: /loop 15s call get_deployment_status "
                        "— show per-resource progress each tick, stop when "
                        "all_completed=true or status=no_deployments."
                    ),
                }

                # For postgres_server: extract connection variables from the jenkins result
                # so the LLM can write them to the developer's .env file.
                if resource_type == "postgres_server" and isinstance(workflow_result, dict):
                    jenkins_entry = next(
                        (r for r in workflow_result.get("jenkins_results", [])
                         if r.get("queue_id") == queue_id),
                        None,
                    )
                    if jenkins_entry:
                        snap = jenkins_entry.get("config_snapshot") or {}
                        # Derive helm release name the same way k8s_helm_script_gen_component does:
                        #   identifier = config_snapshot.get("identifier") or server_name
                        #   release_name = identifier.lower().replace(" ", "-")
                        src_identifier = snap.get("identifier") or snap.get("server_name", identifier or "")
                        release_name = src_identifier.lower().replace(" ", "-")
                        ns_tenant = snap.get("tenant_code") or tenant_code
                        namespace = f"{ns_tenant}-ns"
                        connection_variables = {
                            "POSTGRES_HOST":     f"{release_name}-postgresql.{namespace}.svc.cluster.local",
                            "POSTGRES_PORT":     str(snap.get("port") or 5432),
                            "POSTGRES_USER":     snap.get("username") or "postgres",
                            "POSTGRES_DATABASE": snap.get("database") or snap.get("auth.database") or "devlift_db",
                            "POSTGRES_PASSWORD": snap.get("postgresPassword") or "Dvl!ftPg#S3cur3@2025",
                        }
                        response["connection_variables"] = connection_variables

                        # Persist just the KEYS (not values) on the committed
                        # Redis draft so a later provision_service call in the
                        # same project — potentially in a new conversation —
                        # can discover this resource as an env-sync source.
                        draft_key = core["draft_key"]
                        draft_id_str = existing.get("draft_id")
                        current = await get_draft_by_id(user_code, draft_key, draft_id_str)
                        if current is not None:
                            patched = {
                                **current,
                                "canonical_env_keys": list(connection_variables.keys()),
                                "env_synced": False,
                            }
                            await update_draft_by_id(
                                user_code, draft_key, draft_id_str, patched,
                            )
            elif core.get("deploy_mode") == "temporal":
                response = _enterprise_queued_response(
                    resource_type=resource_type,
                    identifier=identifier,
                    draft_id=existing.get("draft_id"),
                )
            else:
                # Enterprise fallback (Temporal disabled) — PR only, no
                # polling, no connection variables surfaced via MCP.
                pr_url = _extract_pr_url(workflow_result)
                if pr_url:
                    message = (
                        f"A pull request has been raised at {pr_url}. "
                        f"Your team will manage the deployment of "
                        f"'{identifier}' from there."
                    )
                else:
                    message = (
                        f"'{identifier}' has been queued — a pull request will "
                        f"appear shortly in your repository. Your team will "
                        f"manage the deployment from there."
                    )
                response = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type,
                    "identifier": identifier,
                    "draft_id": existing.get("draft_id"),
                    "message": message,
                    **({"pr_url": pr_url} if pr_url else {}),
                }

            return response
        except Exception as e:
            logger.exception("devlift_mcp trigger_resource_deployment failed")
            return {"status": "error", "message": f"Deployment failed: {e}"}


# ============================================================
# Public entry: trigger_service_deployment_handler (service_config path)
# ============================================================

async def _sync_envs_by_mapping(
    *,
    db: AsyncSession,
    user_code: str,
    tenant_code: str,
    project_id: str,
    service_metadata: dict,
    source_case_ref_code: str,
    source_draft_id: Optional[str] = None,
    env_mapping: dict[str, str],
) -> dict:
    """Sync environment variables from a source infra resource into a service.

    This side only resolves drafts/metadata; the actual work — validating the
    mapping against the source's variable_mst rows, reading the source
    consolidated AWS secret, and bulk-creating references into the service —
    runs inside devlift-secret-config-manager via
    POST /internal/variables/sync-env-refs (variable-mst-isolation-spec).
    obs_tool touches neither variable_mst nor Secrets Manager here.

    Returns {"status": "ok", "synced": [...]} or {"status": "error", ...}.
    """
    import httpx
    from app.core.enum import EnvironmentEnum, WorkflowSourceTableEnum
    from app.integrations.secret_config_client import SecretConfigClient

    # Resolve source metadata by resource_type name OR raw case_ref_code
    source_metadata = (
        get_resource_metadata(tenant_code, source_case_ref_code)
        or get_service_metadata(tenant_code, source_case_ref_code)
        or find_metadata_by_case_ref_code(tenant_code, source_case_ref_code)
    )
    if source_metadata is None:
        return {
            "status": "error",
            "message": (
                f"Unknown source_case_ref_code '{source_case_ref_code}'. "
                f"Pass a valid resource_type (e.g. 'postgres_server')."
            ),
        }

    source_draft_key = f"{source_metadata['case_ref_code']}:{source_metadata['infrastructuretype_ref_code']}"
    if source_draft_id:
        source_draft = await get_draft_by_id(user_code, source_draft_key, source_draft_id)
        # Reject cross-project sync — the draft must belong to the same
        # project the service is being deployed under.
        if source_draft is not None and source_draft.get("project_id") != project_id:
            return {
                "status": "error",
                "message": (
                    f"source_draft_id '{source_draft_id}' belongs to a different "
                    f"project and cannot be synced into this service. Pick a "
                    f"source from the current project's available_env_sources."
                ),
            }
    else:
        # No source_draft_id — pick the first committed draft for this resource
        # type within THIS project. Committed = already deployed, so its
        # connection variables exist in AWS Secrets Manager.
        all_source = await get_all_drafts(user_code, source_draft_key)
        project_scoped = [d for d in all_source if d.get("project_id") == project_id]
        committed = [d for d in project_scoped if d.get("status") == "committed"]
        source_draft = committed[0] if committed else (project_scoped[0] if project_scoped else None)
    if source_draft is None:
        return {
            "status": "error",
            "message": (
                f"No draft found for source '{source_case_ref_code}'. "
                f"Provision and deploy that resource before syncing its envs."
            ),
        }

    source_tx_code = source_draft.get("transaction_code")
    if not source_tx_code:
        return {
            "status": "error",
            "message": (
                f"Source draft '{source_case_ref_code}' has no transaction_code — "
                f"it may not have been fully provisioned yet."
            ),
        }

    # Service draft — must exist and have transaction_code + application_code.
    # Pick the most recent pending draft for the service type.
    service_draft_key = f"{service_metadata['case_ref_code']}:{service_metadata['infrastructuretype_ref_code']}"
    all_service = await get_all_drafts(user_code, service_draft_key)
    service_draft = next((d for d in reversed(all_service) if d.get("status") == "pending"), None)
    if service_draft is None or not service_draft.get("transaction_code"):
        return {
            "status": "error",
            "message": "Service draft not found or has no transaction_code. Re-provision the service.",
        }
    service_tx_code = service_draft["transaction_code"]
    service_name = service_draft.get("identifier") or ""

    service_application_code = service_draft.get("application_code")
    if not service_application_code:
        return {
            "status": "error",
            "message": (
                "Service draft is missing application_code — re-run provision_service "
                "to refresh the draft."
            ),
        }

    env_value = service_draft.get("deployment_environment")
    if not env_value:
        return {
            "status": "error",
            "message": (
                "Service draft is missing deployment_environment — re-run provision_service "
                "to refresh the draft."
            ),
        }
    try:
        env_enum = EnvironmentEnum(env_value)
    except ValueError:
        return {"status": "error", "message": f"Invalid deployment_environment '{env_value}' for env sync."}

    source_table = (
        WorkflowSourceTableEnum.INFRASTRUCTURE
        if source_metadata.get("transaction_table") == "infrastructure_mst"
        else WorkflowSourceTableEnum.SERVICE_CONFIG
    )

    # Resolve + sync entirely inside devlift-secret-config-manager — obs_tool
    # touches neither variable_mst nor AWS Secrets Manager for variables
    # (variable-mst-isolation-spec). Business errors come back as
    # status="error" with the exact message strings this function always
    # returned, so MCP output is unchanged.
    secret_config_client = SecretConfigClient()
    try:
        sync_result = await secret_config_client.sync_env_refs(
            source_table_name=source_table,
            source_transaction_code=source_tx_code,
            environment=env_value,
            tenant_code=tenant_code,
            application_code=service_application_code,
            service_transaction_code=service_tx_code,
            resource_name=service_name,
            source_label=source_case_ref_code,
            env_mapping=env_mapping,
        )
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail")
        except Exception:
            detail = e.response.text[:300]
        return {
            "status": "error",
            "message": f"Failed to sync variables into service: {detail}",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to sync variables into service: {e}",
        }

    if sync_result.get("status") != "ok":
        return {"status": "error", "message": sync_result.get("message")}

    synced = sync_result.get("synced") or []
    if not synced:
        return {"status": "ok", "synced": []}

    # Mark the source draft as synced so it isn't offered again for this
    # project (see scan_available_env_sources). Best-effort — if the write
    # fails, the sync already succeeded so we don't bubble it up.
    try:
        source_draft_for_update = await get_draft_by_id(
            user_code, source_draft_key, source_draft["draft_id"],
        )
        if source_draft_for_update is not None:
            await update_draft_by_id(
                user_code,
                source_draft_key,
                source_draft["draft_id"],
                {**source_draft_for_update, "env_synced": True},
            )
    except Exception:
        logger.warning(
            "devlift_mcp _sync_envs_by_mapping: failed to mark env_synced=True "
            "on source draft %s (non-fatal)",
            source_draft.get("draft_id"),
        )

    return {"status": "ok", "synced": synced}


async def trigger_service_deployment_handler(
    *,
    resource_type: str,
    is_code_committed: bool = False,
    project_id: Optional[str] = None,
    draft_id: Optional[str] = None,
    sync_envs: bool = False,
    source_case_ref_code: Optional[str] = None,
    source_draft_id: Optional[str] = None,
    env_mapping: Optional[dict[str, str]] = None,
) -> dict:
    """Deploy the user's current EKS service draft.

    The caller MUST verify the working tree is clean and pushed to the remote
    BEFORE setting is_code_committed=True. The build pipeline clones from git,
    so any unpushed code is missing from the deployed image.

    When sync_envs=True, creates service variable_mst rows that REFERENCE the
    source infra resource's variables (via referenced_variable_id). The caller
    provides env_mapping = {target_env_var_name: source_variable_key} so the
    service's env vars match whatever names the developer's code actually uses.
    """
    project_id, init_response = _ensure_project(project_id, "trigger_service_deployment")
    if init_response is not None:
        return init_response

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    metadata = get_service_metadata(tenant_code, resource_type)
    if not metadata:
        return {
            "status": "error",
            "message": (
                f"'{resource_type}' is not a supported service type. "
                f"For infrastructure resources use trigger_resource_deployment."
            ),
        }

    # Server-side guard — refuse to deploy unverified code.
    if not is_code_committed:
        return {
            "status": "code_not_committed",
            "message": (
                "is_code_committed must be true. The build clones from git — "
                "unpushed code = broken deploy. Verify with: "
                "`git status --porcelain` (must be empty) and "
                "`git rev-list origin/<branch>..HEAD --count` (must be 0). "
                "If either fails, run `git add -A && git commit -m \"prepare for deployment\" && git push origin <branch>`, "
                "then call trigger_service_deployment again with is_code_committed=true."
            ),
        }

    if sync_envs:
        if not source_case_ref_code:
            return {
                "status": "error",
                "message": (
                    "sync_envs=true requires source_case_ref_code — pass the resource_type "
                    "of the infra resource whose variables should be injected (e.g. 'postgres_server')."
                ),
            }
        if not env_mapping or not isinstance(env_mapping, dict):
            return {
                "status": "error",
                "message": (
                    "sync_envs=true requires env_mapping — a dict mapping each target env "
                    "var name (as used in the service's code) to its source variable key "
                    "(e.g. {'DB_HOST': 'POSTGRES_HOST'}). Read the service code to discover "
                    "which env var names it reads, then map each to the canonical source keys."
                ),
            }

    async with AsyncSessionLocal() as db:
        try:
            sync_result: dict | None = None

            if sync_envs and source_case_ref_code and env_mapping:
                sync_result = await _sync_envs_by_mapping(
                    db=db,
                    user_code=user_code,
                    tenant_code=tenant_code,
                    project_id=project_id,
                    service_metadata=metadata,
                    source_case_ref_code=source_case_ref_code,
                    source_draft_id=source_draft_id,
                    env_mapping=env_mapping,
                )
                if sync_result.get("status") == "error":
                    return sync_result

            core = await _execute_trigger_core(
                db=db,
                resource_type=resource_type,
                metadata=metadata,
                user_code=user_code,
                tenant_code=tenant_code,
                draft_id=draft_id,
            )
            if not core.get("_ok"):
                return core

            existing = core["existing"]
            workflow_result = core["workflow_result"]
            identifier = existing.get("identifier")
            is_paas = is_paas_tenant(tenant_code)

            if is_paas:
                response: dict = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type,
                    "identifier": identifier,
                    "draft_id": existing.get("draft_id"),
                    "message": (
                        f"'{identifier}' has been submitted for deployment. "
                        f"Your application will be live shortly."
                    ),
                    "next_action": (
                        "Immediately invoke the `/loop` skill now — do not wait "
                        "for the user. Run: /loop 15s call get_deployment_status "
                        "— show per-resource progress each tick, stop when "
                        "all_completed=true or status=no_deployments."
                    ),
                }
            elif core.get("deploy_mode") == "temporal":
                response = _enterprise_queued_response(
                    resource_type=resource_type,
                    identifier=identifier,
                    draft_id=existing.get("draft_id"),
                )
            else:
                # Enterprise fallback (Temporal disabled) — PR only, no polling.
                pr_url = _extract_pr_url(workflow_result)
                if pr_url:
                    message = (
                        f"A pull request has been raised at {pr_url}. "
                        f"Your team will manage the deployment of "
                        f"'{identifier}' from there."
                    )
                else:
                    message = (
                        f"'{identifier}' has been queued — a pull request will "
                        f"appear shortly in your repository. Your team will "
                        f"manage the deployment from there."
                    )
                response = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type,
                    "identifier": identifier,
                    "draft_id": existing.get("draft_id"),
                    "message": message,
                    **({"pr_url": pr_url} if pr_url else {}),
                }

            if sync_result is not None:
                response["env_sync"] = {
                    "source": source_case_ref_code,
                    "synced": sync_result.get("synced", []),
                }
            return response
        except Exception as e:
            logger.exception("devlift_mcp trigger_service_deployment failed")
            return {"status": "error", "message": f"Deployment failed: {e}"}


# ============================================================
# Public entry: provision_service_handler (service_config path)
# ============================================================

async def provision_service_handler(
    *,
    resource_type: str,
    attributes: dict,
    product: str,
    environment: str,
    geo_location: str,
    project_id: Optional[str] = None,
    draft_id: Optional[str] = None,
) -> dict:
    """Create or update a service_config draft for an EKS service.

    Flow:
      1. Ensure a project anchor exists (hard-block init if missing)
      2. Resolve identity from JWT AuthContext
      3. Resolve placement (application_code, geo_loc_code, env_enum)
      4. Look up services_mst by service_name (or service_name-service).
         If not found, auto-create via ServicesMstService.create_service using
         the first resource_group for the application; service_type defaults to API.
      5. Resolve EKS cluster (infrastructure_mst_code). On UPDATE reuse the
         cluster_code stored in the existing Redis draft.
      6. Upsert service_config row (draft save — no GitHub trigger)
      7. Create ticket + queue entry (transaction_code = service_config_code)
      8. Store Redis draft blob (includes services_mst_code + cluster_code)
    """
    # 1. Ensure a project anchor exists before doing anything else.
    project_id, init_response = _ensure_project(project_id, "provision_service")
    if init_response is not None:
        return init_response

    # 2. Resolve identity from JWT-set AuthContext (needed for tenant-scoped metadata)
    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code
    user_email = auth_ctx.user_email

    metadata = get_service_metadata(tenant_code, resource_type)
    if not metadata:
        return {
            "status": "error",
            "message": f"Service type '{resource_type}' is not supported. Call list_supported_resources to see what's available.",
        }

    env_enum = _resolve_environment(environment)
    allowed_envs = get_environment_options(tenant_code)
    if env_enum is None or not is_environment_allowed(tenant_code, environment):
        return {
            "status": "error",
            "message": (
                f"Environment '{environment}' is not available for your tenant. "
                f"Valid environments: {', '.join(allowed_envs)}."
            ),
        }

    case_ref_code = metadata["case_ref_code"]
    service_name = attributes.get("service_name", "").strip()

    # Validate the fields that will hard-fail at deploy time if missing
    missing = []
    if not service_name:
        missing.append("service_name")
    if not attributes.get("repository", "").strip():
        missing.append("repository")
    if not attributes.get("branches"):
        missing.append("branches")
    if missing:
        return {
            "status": "incomplete",
            "missing_required_fields": missing,
            "message": f"Cannot provision yet — please provide: {', '.join(missing)}.",
        }

    async with AsyncSessionLocal() as db:
        # 1. Resolve placement
        application_code, available_apps = await _resolve_application_code(db, tenant_code, user_code, product)
        if not application_code:
            return {
                "status": "error",
                "message": (
                    f"Application '{product}' not found for your tenant. "
                    f"Available applications: {', '.join(available_apps) if available_apps else '(none)'}."
                ),
            }

        geo_loc_code, available_geos = _resolve_geo_loc_code(tenant_code, env_enum.value, geo_location)
        if not geo_loc_code:
            return {
                "status": "error",
                "message": (
                    f"Geo location '{geo_location}' is not valid for {environment}. "
                    f"Available locations: {', '.join(available_geos) if available_geos else '(none)'}."
                ),
            }

        # 2. Draft lookup — draft_key matches what trigger_service_deployment uses.
        draft_key = f"{case_ref_code}:{metadata['infrastructuretype_ref_code']}"

        # Auto-resume: no draft_id but exact name match in the SAME project → set draft_id.
        if not draft_id:
            _all = await get_all_drafts(user_code, draft_key)
            _pending = [
                d for d in _all
                if d.get("status") != "committed"
                and d.get("project_id") == project_id
            ]
            _match = next((d for d in _pending if d.get("identifier") == service_name), None)
            if _match:
                draft_id = _match["draft_id"]

        if draft_id:
            existing = await get_draft_by_id(user_code, draft_key, draft_id)
            if existing is None:
                return {
                    "status": "error",
                    "message": (
                        f"Draft '{draft_id}' not found for {resource_type}. "
                        f"It may have expired. Call provision_service without draft_id to start fresh."
                    ),
                }
            # Name mismatch — prevent accidentally overwriting a different service
            if existing.get("identifier") and existing["identifier"] != service_name:
                return {
                    "status": "name_mismatch",
                    "draft_id": draft_id,
                    "draft_name": existing["identifier"],
                    "requested_name": service_name,
                    "message": (
                        f"Draft '{draft_id}' belongs to '{existing['identifier']}', "
                        f"but you requested '{service_name}'. "
                        f"To update the existing draft, call with service_name='{existing['identifier']}'. "
                        f"To create a new service, call without draft_id."
                    ),
                }
            is_update = True
        else:
            # No name match → CREATE new draft alongside any existing
            existing = None
            is_update = False

        # 3. Resolve services_mst_code
        #    On UPDATE reuse the stored services_mst_code so we don't re-query.
        if is_update and existing:
            services_mst_code = existing["services_mst_code"]
            cluster_code = existing["cluster_code"]
            infra_repo = InfrastructureMstRepository(db)
            cluster_row = await infra_repo.get_by_code(cluster_code)
            cluster_locator: dict = (cluster_row.locator or {}) if cluster_row else {}
        else:
            # CREATE path — look up (or auto-create) the service_mst row
            services_repo = ServicesMstRepository(db)
            service_row = await services_repo.find_by_name_for_mcp(
                tenant_code=tenant_code,
                service_name=service_name,
            )

            if service_row is None:
                # Auto-create: resolve first resource_group for this application
                rg_repo = ResourceGroupMstRepository(db)
                resource_group = await rg_repo.get_first_by_application(
                    tenant_code=tenant_code,
                    application_code=application_code,
                )
                if resource_group is None:
                    return {
                        "status": "error",
                        "message": (
                            f"No resource group found for application '{product}'. "
                            f"Ask the DevOps team to set up at least one resource group first."
                        ),
                    }

                from app.core.enum import ServiceTypeEnum
                services_mst_service = ServicesMstService(db)
                create_result = await services_mst_service.create_service(
                    tenant_code=tenant_code,
                    data=CreateServiceRequest(
                        application_code=application_code,
                        resource_group_code=resource_group.code,
                        service_name=service_name,
                        service_type=ServiceTypeEnum.API,
                        is_active=True,
                        is_public_facing=False,
                    ),
                )
                services_mst_code = create_result["service_code"]
                logger.info(
                    "devlift_mcp provision_service: auto-created service_mst code=%s name=%s",
                    services_mst_code, service_name,
                )
            else:
                services_mst_code = service_row.code
                logger.info(
                    "devlift_mcp provision_service: found existing service_mst code=%s name=%s",
                    services_mst_code, service_row.name,
                )

            # Resolve EKS cluster for this placement
            infra_repo = InfrastructureMstRepository(db)
            cluster_code, candidates = await infra_repo.find_eks_cluster_for_mcp(
                tenant_code=tenant_code,
                application_code=application_code,
                environment=env_enum,
                geo_loc_mst_code=geo_loc_code,
            )
            if cluster_code is None:
                if not candidates:
                    return {
                        "status": "error",
                        "message": (
                            f"No EKS cluster found for {product}/{environment}/{geo_location}. "
                            f"Ask the DevOps team to register an EKS cluster for this placement."
                        ),
                    }
                names = [c["name"] for c in candidates]
                return {
                    "status": "error",
                    "message": (
                        f"Multiple EKS clusters found for {product}/{environment}/{geo_location}: "
                        f"{', '.join(names)}. Cannot auto-select — contact DevOps to clarify."
                    ),
                }
            cluster_locator: dict = candidates[0].get("locator", {})

        # 4. Validate repository + branches against GitHub
        repository: str = attributes.get("repository", "")
        branches: list = attributes.get("branches") or []
        if repository:
            try:
                github_svc = GitHubMgmtService(db)

                # Check repo exists in the tenant's GitHub installations
                repos_result = await github_svc.get_repositories_for_tenant(tenant_code)
                available_repos = [r["full_name"] for r in repos_result.get("repositories", [])]
                if available_repos and repository not in available_repos:
                    return {
                        "status": "error",
                        "message": (
                            f"Repository '{repository}' not found in your GitHub installations. "
                            f"Pass 'owner/repo' format (not a full URL).\n"
                            f"Available repos:\n"
                            + "\n".join(f"{i}. {r}" for i, r in enumerate(available_repos, 1))
                        ),
                    }

                # Check each branch exists in the repo
                if branches:
                    parts = repository.split("/", 1)
                    if len(parts) == 2:
                        owner, repo_name = parts
                        branches_result = await github_svc.get_repository_branches(owner, repo_name)
                        available_branches = [b["name"] for b in branches_result.get("branches", [])]
                        if available_branches:
                            invalid = [b for b in branches if b not in available_branches]
                            if invalid:
                                return {
                                    "status": "error",
                                    "message": (
                                        f"Branch(es) {invalid} not found in '{repository}'.\n"
                                        f"Available branches:\n"
                                        + "\n".join(
                                            f"{i}. {b}" for i, b in enumerate(available_branches[:20], 1)
                                        )
                                    ),
                                }
            except Exception as e:
                logger.warning("devlift_mcp: GitHub validation failed, proceeding anyway: %s", e)

        # 5. Upsert service_config (draft save — no GitHub trigger)
        try:
            # Resolve language_ref_code from human-readable language name + version.
            lang_repo = LanguageRefRepository(db)
            lang_row = await lang_repo.find_for_mcp(
                language_name=attributes.get("language", ""),
                language_version=attributes.get("language_version"),
            )
            resolved_language_ref_code = lang_row.code if lang_row else attributes.get("language")

            cpu_requested = attributes.get("cpu")
            memory_requested = attributes.get("memory")

            config = MainConfigSchema(
                repository=attributes.get("repository"),
                branches=attributes.get("branches"),
                namespace=tenant_code,
                port=str(attributes["port"]) if attributes.get("port") else None,
                health=attributes.get("health"),
                cpu_requested=cpu_requested,
                cpu_limit=_double_k8s_resource(cpu_requested) if cpu_requested else None,
                memory_requested=memory_requested,
                memory_limit=_double_k8s_resource(memory_requested) if memory_requested else None,
                replica_count=str(attributes["replica_count"]) if attributes.get("replica_count") is not None else None,
                generate_dockerfile=False,
                dockerfile_path=attributes.get("dockerfile_path"),
                service_path="/*",
                alb_schema="internet-facing",
                secrets_enabled=False,
                ebs_enabled=False,
                # Cluster context from infrastructure_mst.locator
                cluster_name=cluster_locator.get("cluster_name"),
                cluster_arn=cluster_locator.get("cluster_arn"),
                region=cluster_locator.get("cloudRegion") or cluster_locator.get("region"),
                cloud_region_id=cluster_locator.get("cloudRegionId"),
                subnet_ids=cluster_locator.get("subnetIds"),
            )

            service_config_data = ServiceConfigCreate(
                services_mst_code=services_mst_code,
                infrastructuretype_ref_code=metadata["infrastructuretype_ref_code"],
                infra_vendor_enum=InfraVendorEnum.aws,
                infrastructure_mst_code=cluster_code,
                environment=env_enum,
                geo_loc_mst_code=geo_loc_code,
                language_ref_code=resolved_language_ref_code,
                config=config,
                sync_to_github=False,
            )

            svc_config_service = ServiceConfigService(db)
            service_config_response = await svc_config_service.upsert_service_config(
                tenant_code=tenant_code,
                service_config_data=service_config_data,
                user_email=user_email,
                user_code=user_code,
            )
            service_config_code = service_config_response.code

            # 5. Create ticket + queue entry
            ticket_service = TicketService(db)
            source_ref_id = f"{user_code}:{case_ref_code}:{services_mst_code}"
            ticket = await ticket_service.get_ticket_by_source_ref(
                source=MCP_SOURCE, source_ref_id=source_ref_id
            )
            if not ticket:
                ticket = await ticket_service.generate_ticket_number(
                    tenants_mst_code=tenant_code,
                    user_mst_code=user_code,
                    name=f"{case_ref_code}: {service_name}",
                    description=f"Created via DevLift MCP for {resource_type}",
                    source=MCP_SOURCE,
                    source_ref_id=source_ref_id,
                )

            existing_queue_code = existing.get("queue_code") if is_update and existing else None

            queue_service = TransactionQueueService(db)
            config_snapshot = {
                # User-provided service attributes (canonical names)
                "repository": attributes.get("repository"),
                "branches": attributes.get("branches"),
                "generate_dockerfile": attributes.get("generate_dockerfile", False),
                "dockerfile_path": attributes.get("dockerfile_path") or "Dockerfile",
                "go_config_path": attributes.get("go_config_path", ""),
                "go_use_aws_secrets": attributes.get("go_use_aws_secrets", False),
                "namespace": tenant_code,
                "cpu_requested": cpu_requested,
                "cpu_limit": _double_k8s_resource(cpu_requested) if cpu_requested else None,
                "memory_requested": memory_requested,
                "memory_limit": _double_k8s_resource(memory_requested) if memory_requested else None,
                "port": str(attributes["port"]) if attributes.get("port") else None,
                "health": attributes.get("health"),
                "hpa": {"enabled": False},
                "replica_count": str(attributes["replica_count"]) if attributes.get("replica_count") is not None else "1",
                "ebs_enabled": False,
                "alb_schema": "internet-facing",
                "service_path": "/*",
                "secrets_enabled": False,
                "ci_provider": "jenkins",
                # Resolved language fields
                "language_ref_code": resolved_language_ref_code,
                "language_name": lang_row.name if lang_row else attributes.get("language"),
                "language_version": lang_row.version if lang_row else attributes.get("language_version"),
                # Placement & identifiers
                "service_name": service_name,
                "services_mst_code": services_mst_code,
                "infrastructure_mst_code": cluster_code,
                "applications_mst_code": application_code,
                "product_name": product,
                "environment": env_enum.value,
                "geo_loc_mst_code": geo_loc_code,
                "infrastructuretype_ref_code": metadata["infrastructuretype_ref_code"],
            }
            queue_item = await queue_service.add_item_to_queue(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=service_config_code,
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                config_snapshot=config_snapshot,
                case_ref_code=case_ref_code,
                ticket_code=ticket.code,
                queue_code=existing_queue_code,
            )

            await db.commit()

            # 6. Store Redis draft — includes services_mst_code + cluster_code
            #    so subsequent UPDATE calls can reuse them without re-resolving.
            new_draft_id = draft_id or generate_draft_id()
            draft_entry = {
                **_build_draft_entry(
                    draft_id=new_draft_id,
                    project_id=project_id,
                    ticket_code=ticket.code,
                    queue_code=queue_item.code,
                    queue_id=queue_item.id,
                    transaction_code=service_config_code,
                    transaction_table="service_config",
                    identifier=service_name,
                    resource_type=resource_type,
                    deployment_environment=env_enum.value,
                    status="pending",
                ),
                "services_mst_code": services_mst_code,
                "cluster_code": cluster_code,
                "application_code": application_code,
            }
            if is_update:
                await update_draft_by_id(user_code, draft_key, new_draft_id, draft_entry)
            else:
                await add_draft(user_code, draft_key, draft_entry)

            action = "updated" if is_update else "created"

            # Advertise any committed, not-yet-synced infra drafts in this
            # project as candidate env-sync sources. Lets the LLM wire
            # postgres / redis / etc. vars into this service even when they
            # were provisioned in an earlier conversation.
            source_drafts = await scan_available_env_sources(user_code, project_id)
            available_env_sources = [
                {
                    "resource_type": d["resource_type"],
                    "identifier": d.get("identifier"),
                    "draft_id": d["draft_id"],
                    "canonical_keys": d["canonical_env_keys"],
                }
                for d in source_drafts
            ]

            return {
                "status": "success",
                "action": action,
                "resource_type": resource_type,
                "identifier": service_name,
                "placement": {
                    "product": product,
                    "environment": environment,
                    "geo_location": geo_location,
                },
                "attributes": attributes,
                "draft_id": new_draft_id,
                "available_env_sources": available_env_sources,
                "message": (
                    f"{action.capitalize()} EKS service config draft for '{service_name}'. "
                    f"Call trigger_service_deployment when you're ready to ship."
                ),
            }
        except Exception as e:
            logger.exception("devlift_mcp provision_service failed")
            try:
                await db.rollback()
            except Exception:
                pass
            return {"status": "error", "message": f"Provision service failed: {e}"}


# ============================================================
# Public entry: provision_and_trigger_from_ticket_handler
# ============================================================
# Chatbot-driven path. The LLM has chatted with the chatbot via the `chat`
# tool until isReady=true; the resolved attribute_parameters and
# placement_parameters were cached at that moment. This handler now reads
# that cache, creates the infrastructure_mst row + queue + draft, then
# runs _execute_trigger_core to actually deploy. Combines what the legacy
# provision_resource + trigger_resource_deployment flow did in two calls.

def _drop_unset_attributes(attrs: dict) -> dict:
    """Remove keys the user never filled in.

    The chatbot resolves every skipped optional field to null. Persisting
    those nulls puts literal "null" into the resource's locator (the UI
    renders it as text) and, on the next settings save, the script
    generators read an explicit null as "clear this line". A UI-created
    resource simply omits the key, so do the same here. False, 0 and empty
    lists are real answers and are kept.
    """
    return {
        k: v
        for k, v in (attrs or {}).items()
        if v is not None and not (isinstance(v, str) and v.strip() == "")
    }


def _derive_resource_type_from_form_id(form_id: str) -> str:
    """`s3_bucket_creation_form` → `s3_bucket`. Best-effort suffix strip."""
    base = form_id or ""
    for suffix in ("_creation_form", "_creation", "_form"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


async def provision_and_trigger_from_ticket_handler(
    *,
    ticket_code: str,
    project_id: Optional[str] = None,
) -> dict:
    """End-to-end provision + trigger driven by a chatbot ticket.

    Reads the resolved attribute_parameters and placement_parameters that
    `chat_impl` cached at isReady=true, builds the InfrastructureCreateRequest
    directly (no metadata lookup, no label-to-code resolution — the chatbot
    already did that), creates the queue item + Redis draft, then approves
    and runs the deployment pipeline.
    """
    project_id, init_response = _ensure_project(project_id, "trigger_resource_deployment")
    if init_response is not None:
        return init_response

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code
    user_email = auth_ctx.user_email

    from app.mcp_servers.devlift_mcp.chatbot_client import get_cached_chatbot_result
    from app.mcp_servers.devlift_mcp.chatbot_gateway_translator import build_gateway_group_delta

    cached = await get_cached_chatbot_result(user_code, ticket_code)
    if not cached:
        return {
            "status": "error",
            "reason": "fields_incomplete",
            "message": (
                "The provisioning form for this conversation isn't fully "
                "collected yet. Continue the chat to fill in the remaining "
                "details, then I'll deploy."
            ),
            "next_action": {
                "type": "continue_chat",
                "ticket_code": ticket_code,
                "instruction": (
                    f"Call chat(message=<user reply>, ticket_code='{ticket_code}') "
                    f"and loop until the response carries isReady: true. Do not "
                    f"retry trigger_resource_deployment until then."
                ),
            },
        }

    if cached.get("kind") == "service" or (
        cached.get("create_service") and cached.get("service_config")
    ):
        return {
            "status": "error",
            "reason": "wrong_tool",
            "message": (
                "This conversation configured a service, not a resource. "
                "Services are saved as a review draft, not deployed directly."
            ),
            "next_action": {
                "type": "call_create_service_and_save_draft",
                "ticket_code": ticket_code,
                "instruction": (
                    f"Call create_service_and_save_draft(ticket_code='{ticket_code}', "
                    f"project_id='{project_id}') instead. Do not retry "
                    f"trigger_resource_deployment for this ticket."
                ),
            },
        }

    attribute_parameters: dict = cached.get("attribute_parameters") or {}
    placement_parameters: dict = cached.get("placement_parameters") or {}
    form_id: str = cached.get("form_id") or ""

    # The kong form sends the usual two blocks plus a `gateway_group` card. Only
    # the card needs code (paths compiled and wrapped, `secured` turned into the
    # JWT plugin); the rest is already mapped by the form. Building `groups` here
    # is also what routes the request through the v2 generator — the file locator
    # keys off exactly that.
    if cached.get("gateway_group"):
        try:
            delta_group = build_gateway_group_delta(
                cached["gateway_group"], attribute_parameters.get("api_name") or "",
            )
        except ValueError as exc:
            return {
                "status": "error",
                "reason": "fields_incomplete",
                "message": str(exc),
                "next_action": {
                    "type": "continue_chat",
                    "ticket_code": ticket_code,
                    "instruction": (
                        f"Call chat(message=<user reply>, ticket_code='{ticket_code}') "
                        f"to finish the missing fields, then retry."
                    ),
                },
            }
        if delta_group is not None:
            attribute_parameters = {**attribute_parameters, "groups": [delta_group]}
            logger.info(
                "devlift_mcp from_ticket: built gateway group '%s · %s' with %d path(s) "
                "for service=%s",
                delta_group["route_group_key"], delta_group["http_method"],
                len(delta_group["paths"]), attribute_parameters.get("api_name"),
            )

    if not attribute_parameters or not placement_parameters:
        return {
            "status": "error",
            "reason": "fields_incomplete",
            "message": (
                "The provisioning form for this conversation isn't fully "
                "collected yet. Continue the chat to fill in the remaining "
                "details, then I'll deploy."
            ),
            "next_action": {
                "type": "continue_chat",
                "ticket_code": ticket_code,
                "instruction": (
                    f"Call chat(message=<user reply>, ticket_code='{ticket_code}') "
                    f"and loop until isReady: true."
                ),
            },
        }

    case_ref_code = placement_parameters.get("case_ref_code")
    infrastructuretype_ref_code = placement_parameters.get("infrastructuretype_ref_code")
    transaction_table = placement_parameters.get("transaction_table") or "infrastructure_mst"
    application_code = placement_parameters.get("applications_mst_code")
    geo_loc_mst_code = placement_parameters.get("geo_loc_mst_code")
    environment_enum_value = placement_parameters.get("environment_enum")

    missing_keys = [
        name for name, val in [
            ("case_ref_code", case_ref_code),
            ("infrastructuretype_ref_code", infrastructuretype_ref_code),
            ("applications_mst_code", application_code),
            ("geo_loc_mst_code", geo_loc_mst_code),
            ("environment_enum", environment_enum_value),
        ] if not val
    ]
    if missing_keys:
        return {
            "status": "error",
            "message": (
                f"Chatbot placement_parameters missing required keys: "
                f"{', '.join(missing_keys)}."
            ),
        }

    try:
        environment = EnvironmentEnum(environment_enum_value)
    except ValueError:
        return {
            "status": "error",
            "message": f"Unknown environment_enum '{environment_enum_value}' from chatbot.",
        }

    resource_type = _derive_resource_type_from_form_id(form_id)
    # Each chatbot form's result_template maps the user-typed primary name
    # onto a slightly different key — S3 / SQS / DynamoDB use `identifier`,
    # the database form uses `database_name`, kong uses `api_name`/`route`.
    # Try the well-known keys in priority order so future forms slot in
    # without breaking this trigger path.
    _PRIMARY_NAME_KEYS = (
        "identifier",
        "name",
        "database_name",
        "bucket_name",
        "queue_name",
        "table_name",
        "api_name",
        "route",
        "cluster_name",
        "instance_name",
    )
    identifier_value = next(
        (
            attribute_parameters[k]
            for k in _PRIMARY_NAME_KEYS
            if attribute_parameters.get(k)
        ),
        None,
    )
    if not identifier_value:
        return {
            "status": "error",
            "message": (
                "Chatbot attribute_parameters did not include a recognizable "
                "primary-name key. Expected one of: "
                f"{', '.join(_PRIMARY_NAME_KEYS)}. "
                f"Got keys: {list(attribute_parameters.keys())}."
            ),
        }

    # SQS names must not carry the '.fifo' suffix — terraform-aws-modules/sqs
    # appends it itself when fifo_queue=true, so 'orders' ships as 'orders.fifo'
    # but 'orders.fifo' ships as 'orders.fifo-01.fifo'. The UI already blocks
    # this (lib/validation/sqsQueueName.ts); this is the same rule for the
    # chatbot/MCP path, which otherwise has no name check at all.
    if infrastructuretype_ref_code == "sqs_infrastructuretype_ref":
        try:
            validate_sqs_identifier(str(identifier_value))
        except ValueError as exc:
            return {
                "status": "error",
                "reason": "invalid_field",
                "message": str(exc),
                "next_action": {
                    "type": "continue_chat",
                    "ticket_code": ticket_code,
                    "instruction": (
                        f"Call chat(message=<corrected queue name>, "
                        f"ticket_code='{ticket_code}') to fix the queue name, "
                        f"then retry."
                    ),
                },
            }

    # Idempotency guard — if this exact resource (same user + case +
    # primary name) has already been triggered and is sitting in an
    # open PR, return that PR instead of creating a duplicate.
    #
    # Required because MCP clients (e.g. Claude Code) cap tool calls
    # around ~60s but the trigger path takes ~80s, so the client times
    # out and the LLM retries. Without this guard, every retry creates
    # a fresh infrastructure_mst + workflow + PR. Service-level
    # duplicate checks skip databases / kong / etc. (see
    # InfrastructureCreationService._check_infrastructure_duplicate),
    # so this is the only net here for those types.
    source_ref_id = f"{user_code}:{case_ref_code}:{identifier_value}"

    # Temporal deployments raise their PR asynchronously, so the PR-based
    # guard below is blind during the first minute or so. The Redis draft
    # already knows a workflow is running for this name — answer from it.
    in_progress = await _find_in_progress_draft(
        user_code, f"{case_ref_code}:{infrastructuretype_ref_code}", identifier_value
    )
    if in_progress:
        logger.info(
            "devlift_mcp from_ticket: deployment already running for "
            "source_ref_id=%s workflow_id=%s",
            source_ref_id,
            in_progress.get("workflow_id"),
        )
        return _enterprise_queued_response(
            resource_type=in_progress.get("resource_type") or form_id,
            identifier=identifier_value,
            draft_id=in_progress.get("draft_id"),
            extra={"ticket_code": ticket_code, "action": "already_queued"},
        )

    async with AsyncSessionLocal() as db:
        # Join ticket → transaction_queue → gitops_workflow_detail in one
        # query. TicketModel doesn't carry transaction_code directly; the
        # link is via queue items.
        existing_wf_stmt = (
            select(GitopsWorkflowDetailModel)
            .join(
                TransactionQueueModel,
                TransactionQueueModel.transaction_code
                == GitopsWorkflowDetailModel.transaction_code,
            )
            .join(
                TicketModel,
                TicketModel.code == TransactionQueueModel.ticket_code,
            )
            .where(
                TicketModel.source == MCP_SOURCE,
                TicketModel.source_ref_id == source_ref_id,
                GitopsWorkflowDetailModel.tenant_mst_code == tenant_code,
                GitopsWorkflowDetailModel.user_mst_code == user_code,
                GitopsWorkflowDetailModel.is_deleted == False,
                GitopsWorkflowDetailModel.pr_status == PRStatusEnum.PR_OPEN,
                GitopsWorkflowDetailModel.pr_url.isnot(None),
            )
            .order_by(GitopsWorkflowDetailModel.created_at.desc())
            .limit(1)
        )
        existing_wf = (
            await db.execute(existing_wf_stmt)
        ).scalar_one_or_none()
        if existing_wf:
            logger.info(
                "devlift_mcp from_ticket: returning existing PR for "
                "source_ref_id=%s pr=%s",
                source_ref_id,
                existing_wf.pr_url,
            )
            if is_paas_tenant(tenant_code):
                return {
                    "status": "ok",
                    "action": "already_deployed",
                    "identifier": identifier_value,
                    "message": (
                        f"**{identifier_value}** has been deployed. "
                        f"Open DevLift to view it."
                    ),
                    "next_action": {
                        "type": "present_completion",
                        "instruction": (
                            "Tell the user the resource is deployed and "
                            "to open DevLift to view it. Do not surface "
                            "any PR link. Do not retry "
                            "trigger_resource_deployment."
                        ),
                    },
                }
            return {
                "status": "ok",
                "message": (
                    f"A pull request for **{identifier_value}** has "
                    f"already been raised. Surface the link to the user."
                ),
                "pr_url": existing_wf.pr_url,
                "pr_number": existing_wf.pr_number,
                "identifier": identifier_value,
                "git_repository": existing_wf.git_repository,
                "next_action": {
                    "type": "present_pr_links",
                    "instruction": (
                        "Present `pr_url` to the user as a clickable "
                        "markdown link labelled with `identifier`. Do "
                        "not retry trigger_resource_deployment."
                    ),
                },
            }

    canonical_attrs = _drop_unset_attributes(attribute_parameters)

    async with AsyncSessionLocal() as db:
        # Canvas placement enrichment (visualization hints — non-fatal)
        try:
            canvas_placement = await _resolve_canvas_placement(
                db=db,
                tenant_code=tenant_code,
                application_code=application_code,
                environment=environment.value,
                geo_loc_mst_code=geo_loc_mst_code,
                vendor=InfraVendorEnum.aws.value,
            )
            if canvas_placement:
                canonical_attrs = {**canonical_attrs, **canvas_placement}
        except Exception:
            logger.exception("devlift_mcp from_ticket: canvas placement lookup failed (non-fatal)")

        try:
            infra_service = InfrastructureCreationService(db)
            # Kong routes need service_mst_code on the top-level request, but
            # the chatbot's kong form delivers it inside attribute_parameters.
            # Lift it out (and any other top-level fields a form may surface
            # this way) — leave a copy in type_specific_config so downstream
            # readers that look there still work.
            service_mst_code = canonical_attrs.get("service_mst_code")
            infra_request = InfrastructureCreateRequest(
                code=None,
                infrastructuretype_ref_code=infrastructuretype_ref_code,
                application_code=application_code,
                environment=environment,
                geo_loc_mst_code=geo_loc_mst_code,
                service_mst_code=service_mst_code,
                type_specific_config=canonical_attrs,
                created_by=user_email,
            )
            infra_response = await infra_service.create_resource(
                tenant_code=tenant_code,
                user_code=user_code,
                request=infra_request,
                user_email=user_email,
            )

            ticket_service = TicketService(db)
            source_ref_id = f"{user_code}:{case_ref_code}:{identifier_value}"
            ticket = await ticket_service.get_ticket_by_source_ref(
                source=MCP_SOURCE, source_ref_id=source_ref_id
            )
            if not ticket:
                ticket = await ticket_service.generate_ticket_number(
                    tenants_mst_code=tenant_code,
                    user_mst_code=user_code,
                    name=f"{case_ref_code}: {identifier_value}",
                    description=f"Created via DevLift MCP (chatbot) for {resource_type or form_id}",
                    source=MCP_SOURCE,
                    source_ref_id=source_ref_id,
                )

            queue_service = TransactionQueueService(db)
            config_snapshot = {
                **canonical_attrs,
                "applications_mst_code": application_code,
                "environment": environment.value,
                "geo_loc_mst_code": geo_loc_mst_code,
                "case_ref_code": case_ref_code,
                "infrastructuretype_ref_code": infrastructuretype_ref_code,
                "infrastructure_mst_code": infra_response.code,
            }
            queue_item = await queue_service.add_item_to_queue(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=infra_response.code,
                table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                config_snapshot=config_snapshot,
                case_ref_code=case_ref_code,
                ticket_code=ticket.code,
            )

            new_draft_id = generate_draft_id()
            await db.commit()

            draft_key = f"{case_ref_code}:{infrastructuretype_ref_code}"
            draft_entry = _build_draft_entry(
                draft_id=new_draft_id,
                project_id=project_id,
                ticket_code=ticket.code,
                queue_code=queue_item.code,
                queue_id=queue_item.id,
                transaction_code=infra_response.code,
                transaction_table=transaction_table,
                identifier=identifier_value,
                resource_type=resource_type or form_id,
                deployment_environment=environment.value,
                status="pending",
            )
            await add_draft(user_code, draft_key, draft_entry)
        except Exception as e:
            logger.exception("devlift_mcp from_ticket: create draft failed")
            try:
                await db.rollback()
            except Exception:
                pass
            return {"status": "error", "message": f"Create from ticket failed: {e}"}

        # Now run the same trigger pipeline the legacy path uses.
        try:
            pseudo_metadata = {
                "case_ref_code": case_ref_code,
                "infrastructuretype_ref_code": infrastructuretype_ref_code,
                "transaction_table": transaction_table,
            }
            core = await _execute_trigger_core(
                db=db,
                resource_type=resource_type or form_id,
                metadata=pseudo_metadata,
                user_code=user_code,
                tenant_code=tenant_code,
                draft_id=new_draft_id,
            )
            if not core.get("_ok"):
                return core

            existing = core["existing"]
            workflow_result = core["workflow_result"]
            identifier = existing.get("identifier")
            is_paas = is_paas_tenant(tenant_code)

            if is_paas:
                response: dict = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type or form_id,
                    "identifier": identifier,
                    "ticket_code": ticket_code,
                    "draft_id": existing.get("draft_id"),
                    "message": (
                        f"'{identifier}' has been submitted for deployment. "
                        f"Your application will be live shortly."
                    ),
                    "next_action": (
                        "Immediately invoke the `/loop` skill now — do not wait "
                        "for the user. Run: /loop 15s call get_deployment_status "
                        "— show per-resource progress each tick, stop when "
                        "all_completed=true or status=no_deployments."
                    ),
                }
            elif core.get("deploy_mode") == "temporal":
                response = _enterprise_queued_response(
                    resource_type=resource_type or form_id,
                    identifier=identifier,
                    draft_id=existing.get("draft_id"),
                    extra={"ticket_code": ticket_code},
                )
            else:
                pr_url = _extract_pr_url(workflow_result)
                if pr_url:
                    message = (
                        f"A pull request has been raised at {pr_url}. "
                        f"Your team will manage the deployment of "
                        f"'{identifier}' from there."
                    )
                else:
                    message = (
                        f"'{identifier}' has been queued — a pull request will "
                        f"appear shortly in your repository. Your team will "
                        f"manage the deployment from there."
                    )
                response = {
                    "status": "success",
                    "action": "deployed",
                    "resource_type": resource_type or form_id,
                    "identifier": identifier,
                    "ticket_code": ticket_code,
                    "draft_id": existing.get("draft_id"),
                    "message": message,
                    **({"pr_url": pr_url} if pr_url else {}),
                }
            return response
        except Exception as e:
            logger.exception("devlift_mcp from_ticket: trigger failed")
            return {"status": "error", "message": f"Deployment failed: {e}"}


async def _resolve_canvas_eks_cluster(
    *,
    db: AsyncSession,
    tenant_code: str,
    application_code: str,
    environment: str,
    cluster_code: str,
    locator: dict,
) -> dict:
    """Cluster placement as the web's "Create & Add" sees it.

    The web fills cluster_name / cluster_arn / region / cloud_region_id /
    subnet_ids / vpc_id from the canvas EKS node, i.e. the eksClusters of
    `VpcAndResourceDiscoveryService.get_placement_context` — for tenants on
    the static VPC dataset that list differs from the infrastructure_mst row's
    locator (different subnet set, sometimes no vpcId). Prefer the canvas entry
    that matches this cluster; fall back to the locator field by field. Never
    fatal: a canvas miss just means the locator is used, as before.
    """
    merged = dict(locator or {})
    try:
        # get_canvas_data, not get_placement_context: only the full canvas
        # carries eksClusters (and it is what the web's canvas renders from).
        context = await VpcAndResourceDiscoveryService().get_canvas_data(
            tenant_code=tenant_code,
            application_code=application_code,
            environment=environment,
            db=db,
        )
        want_code = cluster_code
        want_arn = (merged.get("cluster_arn") or "").lower()
        want_name = (merged.get("cluster_name") or "").lower()
        match = None
        for c in getattr(context, "eksClusters", None) or []:
            if getattr(c, "infrastructureMstCode", None) == want_code:
                match = c
                break
            if want_arn and (getattr(c, "clusterArn", "") or "").lower() == want_arn:
                match = c
                break
            if want_name and (getattr(c, "clusterName", "") or "").lower() == want_name:
                match = c
                break
        if match is None:
            logger.info(
                "devlift_mcp create_service: no canvas EKS cluster for %s — using the row locator",
                cluster_code,
            )
            return merged
        for key, attr in (
            ("cluster_name", "clusterName"),
            ("cluster_arn", "clusterArn"),
            ("cloudRegion", "cloudRegion"),
            ("cloudRegionId", "cloudRegionId"),
            ("subnetIds", "subnetIds"),
            ("vpcId", "vpcId"),
        ):
            value = getattr(match, attr, None)
            if value not in (None, "", []):
                merged[key] = value
        return merged
    except Exception:
        logger.exception("devlift_mcp create_service: canvas EKS lookup failed (non-fatal)")
        return merged


# ============================================================
# Public entry: create_service_and_save_draft_handler (EKS services)
# ============================================================
# Services do not take the self-approving resource path. They follow the
# review lane (draft -> submit -> approve -> deploy) whose permission checks
# live in obs_tool's route access cards, so this handler CALLS obs_tool's own
# REST routes with the caller's identity instead of the Python services —
# every card runs exactly as it does for the web. The resource path above
# stays in-process; nothing there is carded.
#
# Mirrors the web: "Create & Add" (create-service + baseline service-config,
# which also maps the new config under its resource group in OpenFGA) followed
# by the Settings tab's Save (a DRAFT queue row). Idempotent: whatever already
# exists is looked up and reused, so a retry after a client timeout never
# creates a duplicate.

_SERVICE_DRAFT_KEY = "update_service:eks_infrastructuretype_ref"


def _section_action(section: dict, preface: str = "") -> dict:
    """One dialog for a group of fields (the chatbot's `section`): the user
    answers up to 4 fields at once and the answers go back structured, so
    the chatbot fills them without its extraction LLM. Shared by the `chat`
    tool and the tools that open a pre-filled session."""
    fields = section.get("fields") or []
    ids = ", ".join(f.get("field_id", "?") for f in fields)

    # A dropdown's options ARE the allowed set. A number or text field's are
    # `suggested_values` — convenience, never validated against, with the real
    # limit in that field's `hint`. Only `type` tells them apart, and the
    # difference decides a rule: "do not invent values not in this list" is
    # right for a repository and wrong for CPU, where it turned three
    # suggestions into a hard floor. Asked for 0.25 cores, the model reported
    # "minimum 0.5" — a limit no code anywhere enforces. The form accepts 0.1,
    # obs_tool's validator wants only > 0, and the dashboard has no floor
    # either; 0.5 was simply the lowest button on screen.
    open_fields = [
        f for f in fields
        if f.get("type") in ("number", "text") and (f.get("options") or [])
    ]
    open_clause = ""
    if open_fields:
        named = ", ".join(
            f"{f.get('field_id')} ({f.get('hint')})" if f.get("hint")
            else str(f.get("field_id"))
            for f in open_fields
        )
        open_clause = (
            f" These take FREE input: {named}. For them the choices are common "
            f"values, NOT the permitted set — what IS permitted is the `hint` "
            f"beside each (a range for a number, a format for text). Offer the "
            f"choices, say what the hint allows in the question, and treat "
            f"Other as a real option, so someone who needs a value the buttons "
            f"do not show gets it instead of being told the nearest button is "
            f"a limit. Never claim a limit the hint does not give. "
            f"This applies ONLY to these fields; for a dropdown or array the "
            f"options really are the allowed set."
        )

    # An entry whose `options` is an EMPTY list takes free input (Kong's paths,
    # a service's other_paths). The picker still needs rows, so the model
    # improvised: it lifted the example paths out of the question text and
    # wrote a line of prose under each saying it was only an example. The user
    # then read the same two paths twice and the same sentence twice, for a
    # field they were always going to type into. `examples` carries those
    # shapes as data instead, and they belong on screen ONCE — in the rows.
    #
    # An example ALSO does the `hint`'s job better than the hint does: two
    # paths show the anchors and the capture group at a glance, where the
    # sentence spelling out the same rule is a third thing to read before
    # answering. So the hint is dropped from a question that has examples —
    # not lost, just moved to where it lands: a value that breaks the rule is
    # refused with the rule attached (validation.py: "<label> must be <the
    # regex_description>"), and `_kong_path_suggestions` adds Kong's spelling
    # of what they typed. Correcting one path beats every user reading a
    # warning about a mistake most of them were not going to make.
    free_fields = [
        f for f in fields if isinstance(f.get("options"), list) and not f["options"]
    ]
    free_clause = ""
    if free_fields:
        named = ", ".join(str(f.get("field_id")) for f in free_fields)
        free_clause = (
            f" These have no choices of their own and are typed: {named}. "
            f"Where one carries `examples`, those ARE its rows — verbatim, in "
            f"order, as BARE labels with no description under them. They are "
            f"shapes, NOT the values the field is limited to, so pass them "
            f"through untouched and leave them OUT of the question text. For "
            f"such a field the question is the `label` and the `description` "
            f"and STOPS there: drop its `hint`, state no format rule and warn "
            f"about no spelling, because the rows already show the shape and "
            f"a value that breaks the rule comes straight back refused, with "
            f"the rule and a corrected version attached. Invent no extra row. "
            f"Where a field has no `examples`, offer only values you genuinely "
            f"hold and say where each came from, and its `hint` stays. Either "
            f"way the real answer normally arrives through Other, and may "
            f"carry several values at once, comma separated."
        )

    return {
        "type": "ask_section",
        "title": section.get("title"),
        "fields": fields,
        "instruction": (
            f"{preface}Render ONE AskUserQuestion titled '{section.get('title')}' with one "
            f"question per entry of `fields`, in order ({ids}): header = a short "
            f"form of `label` (max 12 characters); question = `label`, then "
            f"`description` when present — it says what the field is FOR and is "
            f"the whole point of the question, so carry it over rather than "
            f"summarising it away — then `hint` when present, which says what "
            f"the field ACCEPTS. A field often has both and they are not "
            f"interchangeable; the choices = the first 4 entries of `options` using "
            f"each option's `text` as the label, with a `description` ONLY "
            f"where it adds what the label does not ('Yes' -> 'Create the Argo "
            f"CD application' helps; 'config.yaml' under 'config.yaml' is the "
            f"same string twice — omit it, and never pass the option's `value` "
            f"as the description, which for most options IS the label) — "
            f"when `options` has more than 4 entries, name the remaining ones in "
            f"the question text (the user types one via Other); multiSelect = "
            f"`multi`. Do not invent choices.{open_clause}{free_clause} "
            f"Then call chat(ticket_code=<same>, "
            f"message='<one line: field=value, ...>', answers={{field_id: the "
            f"chosen option's `value`, or the typed text; a multi-select as a "
            f"list}}, skip=[every field with `required` false that the user "
            f"chose Skip for or left unanswered]). A required field the user "
            f"gave nothing for is simply left out of `answers` (it is asked "
            f"again). ASK nothing else this turn — but if you already HOLD "
            f"values for fields outside this section (copying another "
            f"service's configuration, or the user named several at once), add "
            f"them to the same `answers` call: supplying what you know is not "
            f"asking, and `missing_fields` says what is still open. Keep a "
            f"field whose options depend on another (branches on repository, "
            f"version on language) for the next call."
        ),
    }


def _continue_chat_response(ticket_code: str, tool_name: str) -> dict:
    return {
        "status": "error",
        "reason": "fields_incomplete",
        "message": (
            "The service configuration for this conversation isn't fully "
            "collected yet. Continue the chat to fill in the remaining "
            "details, then I'll save it."
        ),
        "next_action": {
            "type": "continue_chat",
            "ticket_code": ticket_code,
            "instruction": (
                f"Call chat(message=<user reply>, ticket_code='{ticket_code}') "
                f"and loop until the response carries isReady: true and "
                f"next_action.type == '{tool_name}'. Do not retry {tool_name} "
                f"until then."
            ),
        },
    }


def _obs_tool_api_error(step: str, exc: Exception, service_name: str) -> dict:
    """Translate an obs_tool REST refusal into the tool's error contract."""
    from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError

    if isinstance(exc, ObsToolAPIError):
        if exc.status_code in (401, 403):
            return {
                "status": "error",
                "reason": "permission_denied",
                "message": (
                    f"You don't have permission to {step} for '{service_name}'. "
                    f"{exc.detail_text}"
                ).strip(),
            }
        if exc.status_code == 409:
            return {"status": "error", "reason": "conflict", "message": exc.detail_text}
        if exc.status_code in (400, 422):
            return {
                "status": "error",
                "reason": "invalid_configuration",
                "message": f"DevLift rejected the configuration while trying to {step}: {exc.detail_text}",
            }
        return {
            "status": "error",
            "message": f"DevLift returned {exc.status_code} while trying to {step}: {exc.detail_text}",
        }
    # Transport-level failures (timeouts, connection refused) often carry an
    # empty str(); name the exception type so the message is never blank.
    reason = str(exc).strip() or type(exc).__name__
    return {"status": "error", "message": f"Failed to {step}: {reason}"}


def _config_code_from_409(exc: Exception) -> Optional[str]:
    """`create_service_config` reports an existing row two ways: a dict detail
    carrying `config_code` (service layer) or a sentence naming the code
    (endpoint layer). Pull the code out of either."""
    import re

    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail.get("config_code"):
        return str(detail["config_code"])
    text = getattr(exc, "detail_text", None) or str(exc)
    match = re.search(r"\b(sc-[A-Za-z0-9._-]+)\b", text)
    return match.group(1) if match else None


async def _template_summary(user_code: str, ticket_code: Optional[str], cached: dict) -> Optional[dict]:
    """What the language template gave this service, and what the user changed.

    Compared as strings because the template stores what the form takes and the
    chatbot gives back what it stored — "0.25" and 0.25 are the same answer.
    Returns None when no template was applied, which is every edit and every
    session for a language that has none.
    """
    from app.mcp_servers.devlift_mcp.chatbot_client import get_template_applied

    if not ticket_code:
        return None
    try:
        applied = await get_template_applied(user_code, ticket_code)
    except Exception:
        logger.exception("devlift_mcp create_service: template summary lookup failed")
        return None
    # `applied` false covers the template being offered and declined; empty
    # `answers` covers an edit or clone, which claims the slot without ever
    # having a template. Either way there is nothing to report.
    if not applied or not applied.get("applied") or not applied.get("answers"):
        return None

    template = applied.get("answers") or {}
    # What the fields settled at once the template landed, in the chatbot's own
    # representation. Diffing against what was SENT instead reported every
    # dropdown whose label differs from its value — create_ecr, auth_mode — as
    # a change the user had made, because the chatbot had canonicalised
    # "true" to "Yes" on the way in.
    baseline = applied.get("baseline") or template
    overrides = set(applied.get("overrides") or [])
    final = cached.get("collected_data") or {}

    def _same(a, b) -> bool:
        if isinstance(a, list) or isinstance(b, list):
            return [str(x) for x in (a or [])] == [str(x) for x in (b or [])]
        return str(a).strip().lower() == str(b).strip().lower()

    kept, changed = [], {}
    for field_id, template_value in template.items():
        if field_id not in final:
            continue
        if field_id in overrides:
            # Asked for at apply time: the baseline already holds the user's
            # value, so only the template's own value shows what differed.
            changed[field_id] = {"template": template_value, "chosen": final[field_id]}
        elif _same(baseline.get(field_id, template_value), final[field_id]):
            kept.append(field_id)
        else:
            changed[field_id] = {
                "template": baseline.get(field_id, template_value),
                "chosen": final[field_id],
            }
    return {
        "language": applied.get("language"),
        "kept": sorted(kept),
        "kept_count": len(kept),
        "changed": changed,
    }


def _needs_cluster_response(service_name, environment, geo_loc_mst_code, candidates) -> dict:
    """Several clusters could serve this placement and nothing settles it, so
    the user picks. A question, never a guess and never a dead end."""
    return {
        "status": "needs_cluster",
        "reason": "ambiguous_cluster",
        "service_name": service_name,
        "environment": environment.value,
        "geo_loc_mst_code": geo_loc_mst_code,
        "clusters": [{"name": c["name"], "code": c["code"]} for c in candidates],
        "message": (
            f"{len(candidates)} EKS clusters serve {environment.value} in this "
            f"region. Which one should **{service_name}** run on?"
        ),
        "next_action": {
            "type": "choose",
            "options": [{"label": c["name"], "value": c["code"]} for c in candidates],
            "instruction": (
                "Surface `message` and present the options (AskUserQuestion "
                "when 4 or fewer, else a NUMBERED list the user can answer by "
                "number or name). Use each "
                "option's `label` as the choice text and NEVER show its "
                "`value`. Then call create_service_and_save_draft again with "
                "the same ticket_code and the chosen value as cluster_code. "
                "Do not pick for the user."
            ),
        },
    }


async def create_service_and_save_draft_handler(
    *,
    ticket_code: Optional[str],
    project_id: Optional[str] = None,
    cluster_code: Optional[str] = None,
) -> dict:
    """Create an EKS service (if new) and save its configuration as a draft.

    Reads the `create_service` + `service_config` result the `chat` tool
    cached at isReady=true, then:
      1. POST /services/create-service            (skipped when the service exists)
      2. POST /service-configs                    (baseline; skipped when it exists)
      3. POST /transaction/service-settings/{sc}  (the user's values, as a DRAFT)
    Nothing is submitted, approved or deployed.
    """
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt
    from app.mcp_servers.devlift_mcp.chatbot_client import get_cached_chatbot_result, get_edit_origin
    from app.repository.service_config_repository import ServiceConfigRepository

    tool_name = "create_service_and_save_draft"

    project_id, init_response = _ensure_project(project_id, tool_name)
    if init_response is not None:
        return init_response

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    if not ticket_code:
        return {
            "status": "error",
            "reason": "fields_incomplete",
            "message": "ticket_code is required — it comes from the chat session.",
        }

    cached = await get_cached_chatbot_result(user_code, ticket_code)
    if not cached:
        return _continue_chat_response(ticket_code, tool_name)

    if cached.get("kind") == "gateway" or cached.get("gateway_group"):
        # The kong_route_form: routes for an EXISTING service. Same tool, the
        # gateway half of the same draft (web: the second Save request).
        return await _save_gateway_draft_from_ticket(
            auth_ctx=auth_ctx, ticket_code=ticket_code, project_id=project_id, cached=cached
        )

    is_service = cached.get("kind") == "service" or (
        cached.get("create_service") and cached.get("service_config")
    )
    if not is_service:
        return {
            "status": "error",
            "reason": "wrong_tool",
            "message": (
                "This conversation configured a resource, not a service. "
                "Use trigger_resource_deployment for it."
            ),
            "next_action": {
                "type": "call_trigger_resource_deployment",
                "ticket_code": ticket_code,
                "instruction": (
                    f"Call trigger_resource_deployment(ticket_code='{ticket_code}', "
                    f"project_id='{project_id}') instead."
                ),
            },
        }

    # ── 1. What the chatbot resolved ────────────────────────────────────
    try:
        create_payload = sp.build_create_service_payload(cached)
        sc_block = sp.service_config_block(cached)
        environment = EnvironmentEnum(sp._require(sc_block, "environment", "service_config"))
        geo_loc_mst_code = sp._require(sc_block, "geo_loc_mst_code", "service_config")
    except sp.ServicePayloadError as e:
        return {"status": "error", "reason": "fields_incomplete", "message": str(e)}
    except ValueError as e:
        return {"status": "error", "message": f"Unknown environment from chatbot: {e}"}

    service_name: str = create_payload["service_name"]
    application_code: str = create_payload["application_code"]
    service_type: str = create_payload["service_type"]
    alb_selection = sp.alb_selection_for(service_type)

    # ── Placement is the configuration's identity, not one of its settings ──
    # An edit session opened on one row; the target row below is resolved from
    # the ANSWERS. Change the environment or the region in those answers and
    # the save lands somewhere else entirely — a new configuration in the
    # target placement, or the draft written onto a DIFFERENT existing one —
    # and both read back as a successful edit. There is nothing to "move":
    # running the service elsewhere is a second configuration, deliberately
    # created. So refuse, name what changed, and say what an edit CAN change.
    origin = await get_edit_origin(auth_ctx.user_code, ticket_code) if ticket_code else None
    if origin:
        moved = []
        if origin.get("environment") and str(origin["environment"]).lower() != environment.value.lower():
            moved.append(f"environment ({origin['environment']} → {environment.value})")
        if origin.get("geo_loc_mst_code") and origin["geo_loc_mst_code"] != geo_loc_mst_code:
            moved.append(f"region ({origin.get('geo_name') or origin['geo_loc_mst_code']})")
        if origin.get("application_code") and origin["application_code"] != application_code:
            moved.append(f"product ({origin.get('product_name') or origin['application_code']})")
        proposed_rg = create_payload.get("resource_group_code")
        if origin.get("resource_group_code") and proposed_rg and origin["resource_group_code"] != proposed_rg:
            moved.append(f"resource group ({origin.get('resource_group_name') or origin['resource_group_code']})")
        # The service is looked up BY NAME below, so a rename misses it, the
        # row comes back None and the handler builds a whole new service — a
        # bigger surprise than a second configuration. (Cloning renames on
        # purpose, but a clone session writes no origin, so it never lands
        # here.) The name is also the namespace and the ECR repository, which
        # is why it cannot be edited even in principle.
        if origin.get("service_name") and origin["service_name"].strip().lower() != service_name.strip().lower():
            moved.append(f"name ({origin['service_name']} → {service_name})")
        # API vs Worker decides `alb_selection`, which is part of the lookup
        # key a few lines down — so changing it resolves to a different row
        # exactly the way a changed environment does.
        if origin.get("service_type") and str(origin["service_type"]).strip().lower() != service_type.strip().lower():
            moved.append(f"service type ({origin['service_type']} → {service_type})")
        if cluster_code and origin.get("infrastructure_mst_code") and cluster_code != origin["infrastructure_mst_code"]:
            moved.append("cluster")
        if moved:
            where = " / ".join(
                p for p in (origin.get("environment"), origin.get("geo_name"), origin.get("product_name")) if p
            )
            return {
                "status": "error",
                "reason": "placement_change_not_supported",
                "service_name": origin.get("service_name") or service_name,
                "changed": moved,
                "message": (
                    f"**{origin.get('service_name') or service_name}** is configured in "
                    f"{where or 'its current placement'}, and that cannot be changed by "
                    f"editing it. Its name, service type, product, environment, region, "
                    f"resource group and cluster are what identify the service and its "
                    f"configuration — they are not settings on it. "
                    f"Nothing was changed.\n\n"
                    f"An edit can change its settings: repository and branches, language, "
                    f"port, health and service path, CPU and memory, replicas and "
                    f"autoscaling, ALB schema, Dockerfile options and IAM policies. Tell "
                    f"me which of those you want and I will do it.\n\n"
                    f"To run this service in another environment or region, it needs a "
                    f"second configuration created there instead. A different name or "
                    f"service type means a different service, which has to be created "
                    f"as one — copying this one's settings is the usual way."
                ),
                "next_action": {
                    "type": "placement_locked",
                    "instruction": (
                        "Surface `message` verbatim. Do NOT retry the save, do NOT call "
                        "chat again to re-answer the placement, and do NOT offer to move "
                        "the service. Ask which setting the user wants to change, then "
                        "start a fresh edit_service_configuration for it. If they want it "
                        "in another environment or region, say that is a new configuration "
                        "and wait for them to confirm before starting one."
                    ),
                },
            }

    # The chatbot resolves `language_ref_code` from the chosen version's option
    # `value`, so it is only a real code when those options were fetched. When
    # the fetch came back empty the lookup silently falls through to the raw
    # LABEL — "1.23" instead of "GO_1_23" — and nothing downstream notices:
    # the draft saves, submit and approve pass, and the value finally fails the
    # service_configs foreign key inside a Temporal activity, after the pull
    # request is raised. Check it here, where the answer is still a message.
    # A real code always carries its language ("GO_1_24", "PYTHON_3_10",
    # "JAVA-MAVEN_17"); the fallthrough value is the bare version label and has
    # no letter in it at all. That shape test needs no query and cannot be
    # fooled by the one failure this has: a well-formed code that happens not
    # to exist still meets the foreign key, which is the right backstop for it.
    proposed_language = (sc_block or {}).get("language_ref_code")
    if not sp._is_blank(proposed_language):
        if not any(ch.isalpha() for ch in str(proposed_language)):
            logger.warning(
                "devlift_mcp %s: chatbot returned language_ref_code=%r, which is "
                "a bare version rather than a language code — its version "
                "options were probably empty (ticket=%s)",
                tool_name, proposed_language, ticket_code,
            )
            return {
                "status": "error",
                "reason": "unknown_language",
                "message": (
                    f"The language came back as `{proposed_language}`, which is "
                    f"not one DevLift knows — the version list was probably "
                    f"unavailable when it was chosen. Nothing was saved. Say "
                    f"the language and version again (for example 'Go 1.24') "
                    f"and I'll retry."
                ),
                "next_action": {
                    "type": "present_completion",
                    "instruction": (
                        "Tell the user the language did not resolve and nothing "
                        "was saved. Ask them to name the language and version "
                        "again in the SAME chat ticket, then call this tool "
                        "again. Do not submit or deploy anything."
                    ),
                },
            }

    # ── 2. Read-only lookups: existing service, EKS cluster, existing config ──
    async with AsyncSessionLocal() as db:
        service_row = await ServicesMstRepository(db).find_by_name_for_mcp(
            tenant_code=tenant_code,
            service_name=service_name,
        )

        # A service that already has a configuration here already runs on a
        # cluster, and that recorded cluster is the answer — asking again
        # would invite the user to move a live service by accident. Looked up
        # WITHOUT the cluster filter on purpose: the cluster is what we are
        # trying to learn, so it cannot also be part of the key.
        existing_config = None
        configs_on_several_clusters = False
        if service_row is not None:
            config_repo = ServiceConfigRepository(db)
            placement = (
                tenant_code,
                service_row.code,
                environment.value,
                geo_loc_mst_code,
                alb_selection,
                InfraVendorEnum.aws.value,
                sp.EKS_INFRA_TYPE,
            )
            try:
                existing_config = await config_repo.get_by_tenant_service_env_geo_loc(*placement)
            except MultipleResultsFound:
                # The row's unique key ends in the cluster, so one service may
                # legitimately hold a configuration on two clusters of the same
                # placement. Without a cluster the lookup cannot say which, and
                # the repository raises rather than pick.
                configs_on_several_clusters = True
                if cluster_code:
                    # The caller named one: that completes the unique key.
                    existing_config = await config_repo.get_by_tenant_service_env_geo_loc(
                        *placement, cluster_code
                    )
        existing_cluster = getattr(existing_config, "infrastructure_mst_code", None)

        # Named apart from the `cluster_code` PARAMETER, which carries the
        # user's choice from a previous `needs_cluster` turn.
        single_code, candidates = await InfrastructureMstRepository(db).find_eks_cluster_for_mcp(
            tenant_code=tenant_code,
            application_code=application_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
        )
        # `find_eks_cluster_for_mcp` answers with a code only when exactly one
        # cluster matches the placement. The web never faces this: the user has
        # already clicked a cluster on the canvas. Here the form asks for no
        # cluster, so one match is inferred and several become a question for
        # the user — never a guess, and never a dead end.
        # A cluster the caller named is checked against the placement FIRST,
        # whether or not the service already exists — otherwise a stale or
        # invented code would slip through unvalidated on the edit path.
        chosen_match = None
        if cluster_code:
            chosen_match = next((c for c in candidates if c["code"] == cluster_code), None)
            if chosen_match is None:
                names = ", ".join(c["name"] for c in candidates) or "none"
                return {
                    "status": "error",
                    "reason": "unknown_cluster",
                    "message": (
                        f"That cluster is not one of the clusters available for "
                        f"{environment.value} / {geo_loc_mst_code}. Available: {names}."
                    ),
                }

        if existing_cluster:
            # An edit / re-save. The cluster is settled; keep the live one even
            # if it has since left the placement's candidate list (deactivated,
            # renamed), because the service is running on it either way.
            if cluster_code and cluster_code != existing_cluster:
                # Say so rather than quietly ignore it: "move X to cluster Y"
                # must not read as success when nothing moved.
                current = next(
                    (c["name"] for c in candidates if c["code"] == existing_cluster), "its current cluster"
                )
                return {
                    "status": "error",
                    "reason": "cluster_change_not_supported",
                    "service_name": service_name,
                    "message": (
                        f"**{service_name}** already runs on {current} in "
                        f"{environment.value}, and this tool cannot move a running "
                        f"service to {chosen_match['name']}. Its settings were left "
                        f"unchanged. Moving a service between clusters is a DevOps "
                        f"operation — ask them if that is really what you want."
                    ),
                }
            cluster_code = existing_cluster
            selected = next(
                (c for c in candidates if c["code"] == existing_cluster),
                {"code": existing_cluster, "name": existing_cluster, "locator": {}},
            )
        elif cluster_code:
            selected = chosen_match
        elif configs_on_several_clusters:
            # The service holds configurations on more than one cluster here and
            # named none, so there is nothing to infer — ask, same as below.
            return _needs_cluster_response(service_name, environment, geo_loc_mst_code, candidates)
        elif single_code:
            cluster_code, selected = single_code, candidates[0]
        elif not candidates:
            return {
                "status": "error",
                "reason": "no_cluster",
                "message": (
                    f"No EKS cluster is registered for this product in "
                    f"{environment.value} / {geo_loc_mst_code}. Ask the DevOps "
                    f"team to register one before creating '{service_name}'."
                ),
            }
        else:
            return _needs_cluster_response(service_name, environment, geo_loc_mst_code, candidates)

        cluster_locator: dict = selected.get("locator") or {}
        cluster_name = cluster_locator.get("cluster_name") or selected.get("name")
        # The web copies cluster placement from the CANVAS EKS node (which for
        # some tenants is the static VPC dataset, not the DB locator). Read the
        # same source so the baseline row matches a web-created one exactly.
        cluster_locator = await _resolve_canvas_eks_cluster(
            db=db,
            tenant_code=tenant_code,
            application_code=application_code,
            environment=environment.value,
            cluster_code=cluster_code,
            locator=cluster_locator,
        )
        cluster_name = cluster_locator.get("cluster_name") or cluster_name
        # `existing_config` was resolved above, before the cluster, because it
        # is what names the cluster for a service that already runs.

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    created_service = False
    created_config = False

    # ── 3. Create the service (web: POST /services/create-service) ─────
    services_mst_code: Optional[str] = service_row.code if service_row is not None else None
    if not services_mst_code:
        try:
            created = await obs_tool_client.create_service(jwt_token=jwt_token, payload=create_payload)
            services_mst_code = created.get("service_code")
            created_service = True
        except obs_tool_client.ObsToolAPIError as e:
            # A retry after a client timeout: the first call landed. Look it up.
            if e.status_code == 400 and "already exists" in e.detail_text.lower():
                async with AsyncSessionLocal() as db:
                    service_row = await ServicesMstRepository(db).find_by_name_for_mcp(
                        tenant_code=tenant_code, service_name=service_name
                    )
                services_mst_code = service_row.code if service_row is not None else None
            if not services_mst_code:
                return _obs_tool_api_error("create the service", e, service_name)
        except Exception as e:
            logger.exception("devlift_mcp create_service: create-service call failed")
            return _obs_tool_api_error("create the service", e, service_name)
        if not services_mst_code:
            return {"status": "error", "message": "DevLift created the service but returned no code."}

    # ── 4. Baseline configuration (web: POST /service-configs) ─────────
    service_config_code: Optional[str] = existing_config.code if existing_config is not None else None
    ingress_group_order = None
    if existing_config is not None:
        ingress_group_order = (existing_config.config or {}).get("ingress_group_order")
    if not service_config_code:
        baseline = sp.build_baseline_config_payload(
            cached,
            services_mst_code=services_mst_code,
            infrastructure_mst_code=cluster_code,
            cluster_locator=cluster_locator,
        )
        try:
            created_cfg = await obs_tool_client.create_service_config(jwt_token=jwt_token, payload=baseline)
            service_config_code = created_cfg.get("code")
            ingress_group_order = (created_cfg.get("config") or {}).get("ingress_group_order")
            created_config = True
        except obs_tool_client.ObsToolAPIError as e:
            if e.status_code == 409:
                service_config_code = _config_code_from_409(e)
            if not service_config_code:
                return _obs_tool_api_error("create the service configuration", e, service_name)
        except Exception as e:
            logger.exception("devlift_mcp create_service: service-configs call failed")
            return _obs_tool_api_error("create the service configuration", e, service_name)
        if not service_config_code:
            return {"status": "error", "message": "DevLift created the configuration but returned no code."}

    # ── 5. Save the configured values as a DRAFT (web: Settings tab Save) ──
    snapshot = sp.build_draft_snapshot(
        cached,
        services_mst_code=services_mst_code,
        infrastructure_mst_code=cluster_code,
        ingress_group_order=ingress_group_order,
    )
    try:
        draft = await obs_tool_client.save_settings_draft(
            jwt_token=jwt_token,
            service_config_code=service_config_code,
            config_snapshot=snapshot,
            case_ref_code=sp.SETTINGS_CASE_REF,
        )
    except Exception as e:
        logger.exception("devlift_mcp create_service: service-settings draft call failed")
        return _obs_tool_api_error("save the configuration draft", e, service_name)

    approval = draft.get("approval") if isinstance(draft, dict) else None
    queue_code = approval.get("code") if isinstance(approval, dict) else None
    queue_status = approval.get("status") if isinstance(approval, dict) else None

    # ── 6. Remember it for the follow-up tools (submit / approve / deploy) ──
    # One entry per service configuration: a re-run (retry, or a later edit of
    # the same service) updates the existing entry rather than stacking a new
    # one, so the lookups by name / ticket stay unambiguous.
    try:
        existing_entry = None
        for candidate in await get_all_drafts(user_code, _SERVICE_DRAFT_KEY):
            if (
                candidate.get("transaction_code") == service_config_code
                and candidate.get("status") != "discarded"
            ):
                existing_entry = candidate
                break
        entry = {
            **_build_draft_entry(
                draft_id=(existing_entry or {}).get("draft_id") or generate_draft_id(),
                project_id=project_id,
                ticket_code=ticket_code,
                queue_code=queue_code or (existing_entry or {}).get("queue_code") or "",
                queue_id=None,
                transaction_code=service_config_code,
                transaction_table="service_config",
                identifier=service_name,
                resource_type="eks_service",
                deployment_environment=environment.value,
                status="pending",
            ),
            "services_mst_code": services_mst_code,
            "cluster_code": cluster_code,
            "queue_status": queue_status,
        }
        if existing_entry:
            await update_draft_by_id(user_code, _SERVICE_DRAFT_KEY, entry["draft_id"], entry)
        else:
            await add_draft(user_code, _SERVICE_DRAFT_KEY, entry)
    except Exception:
        logger.exception("devlift_mcp create_service: failed to record Redis draft (non-fatal)")

    base = {
        "service_name": service_name,
        "service_code": services_mst_code,
        "service_config_code": service_config_code,
        "environment": environment.value,
        "geo_loc_mst_code": geo_loc_mst_code,
        "cluster_name": cluster_name,
        "ticket_code": ticket_code,
        "created_service": created_service,
        "created_config": created_config,
    }
    # The Variables tab link, so 'Add variables & secrets' needs no second
    # lookup. Values are entered there, never through the MCP.
    base["variables_url"] = await _variables_editor_url(tenant_code, service_config_code)

    # When a language template filled most of this, the user answered a handful
    # of questions and accepted twenty-odd values they saw once. Saying which
    # came from the template and which they changed is the difference between
    # a configuration they reviewed and one that merely happened to them.
    template_summary = await _template_summary(user_code, ticket_code, cached)
    if template_summary:
        base["from_template"] = template_summary

    if approval is None:
        return {
            **base,
            "status": "no_changes",
            "action": "created" if (created_service or created_config) else "unchanged",
            "message": (
                (f"**{service_name}** is set up in {environment.value}. " if created_config else "")
                + (draft.get("detail") if isinstance(draft, dict) and draft.get("detail") else
                   "Your values already match the current configuration, so no change request was queued.")
            ),
            "next_action": {
                "type": "present_completion",
                "instruction": "Tell the user. Do not call any deployment tool.",
            },
        }

    action = "created" if (created_service or created_config) else "draft_saved"
    if action == "created":
        message = (
            f"**{service_name}** has been created in {environment.value} and its "
            f"configuration is saved as a draft. Say 'submit it' when you want "
            f"it sent for review."
        )
    else:
        message = (
            f"The configuration changes for **{service_name}** in {environment.value} "
            f"are saved as a draft. Say 'submit it' when you want it sent for review."
        )
    # The draft's diff, computed by DevLift against the live configuration —
    # the same table the web's "Changes" panel shows (Deployed → Requested).
    changes = approval.get("changes") or {}
    you = approval.get("you") or {}
    return {
        **base,
        "status": "success",
        "action": action,
        "queue_code": queue_code,
        "queue_status": queue_status,
        "change_count": len(changes),
        "changes": changes,
        # What this user may do with the draft, resolved by OpenFGA per record.
        # Drives which choices the question below offers.
        "you": {k: you.get(k) for k in ("mine", "can_approve", "can_deploy", "can_write_settings")},
        "message": message,
        "next_action": {
            "type": "present_draft",
            "instruction": (
                "Surface `message`, then a short 'What was created' list "
                "(service, environment/geo, cluster). "
                "When `from_template` is present, follow that list with one line "
                "naming its `language`, `kept_count` and the phrase 'standard "
                "values', then — only if `from_template.changed` is non-empty — "
                "those fields as 'field: template → chosen'. Do NOT list the kept "
                "fields one by one: the count is the point, and the table below "
                "already shows every value. "
                "Then render `changes` as a "
                "markdown table titled 'Changes' with columns Field | Deployed → "
                "Requested, one row per key in order, showing `from` as '–' when "
                "null and values EXACTLY as given (they come from DevLift's diff "
                "engine; do not convert units, do not add notes or caveats about "
                "them). Do NOT submit on your own — only call "
                "submit_service_request when the user asks. Do not call "
                "trigger_resource_deployment or get_deployment_status. "
                "The chat ticket stays open for VALUE CHANGES only: send "
                "'change cpu to 1' / 'generate dockerfile not needed' to "
                "chat(ticket_code=<this ticket>) and the draft is re-saved. "
                "Everything else is NOT a chat message: 'show the preview' / "
                "'what did I change' -> get_service_configuration(ticket_code=<this ticket>); "
                "'show the settings' -> view_service_settings(ticket_code=<this ticket>); "
                "'submit it' -> submit_service_request(ticket_code=<this ticket>). "
            ) + _after_draft_question(service_name, ticket_code, ticket_code, you),
        },
    }


def _after_draft_question(
    service_name: str,
    ticket_code: str,
    configuration_ticket: Optional[str],
    you: Optional[dict] = None,
) -> str:
    """The one question every draft save ends with — the choices the user has on
    the web page after Save: send it, add routes, add variables, or keep editing.

    Someone who may approve AND deploy this service can also take it all the way
    in one go, so they get that as an extra choice. `can_approve` already
    accounts for the resource group's self-approval rule, so an author who may
    not approve their own request never sees it.
    """
    you = you or {}
    value_route = (
        f"chat(ticket_code='{configuration_ticket}')"
        if configuration_ticket
        else f"edit_service_configuration(service_name='{service_name}')"
    )

    options = [
        f"'Submit for review' -> submit_service_request(ticket_code='{ticket_code}')",
    ]
    if you.get("can_approve") and you.get("can_deploy"):
        options.append(
            "'Submit, approve and deploy' -> the same change, taken all the way by "
            "this user, who holds both rights: call "
            f"submit_service_request(ticket_code='{ticket_code}'), then "
            "approve_service_request(queue_code=<the code it returned>), then "
            "deploy_service_request(queue_code=<the same code>, confirmed=true) — "
            "three calls in that order, in one turn. Stop at the first that "
            "answers with an error and surface its message; do not carry on. On "
            "PRODUCTION the deploy still asks the user to type the service name "
            "before anything ships"
        )
    options += [
        f"'Add Kong route' -> edit_service_configuration(service_name='{service_name}', "
        f"section='gateway', route_action='Add') — the choice SAYS add, so the "
        f"form must not open by asking whether this is an add",
        f"'Add variables & secrets' -> open_variables_editor(service_name='{service_name}') and show the returned link",
    ]
    # A dialog shows at most 4 choices and silently drops any beyond that, so
    # the "do nothing" choice is the one that gives way when a fifth appears:
    # it is the only one the dialog already covers, through its own free-text
    # box and through cancelling. Say so in the line above the question rather
    # than let it disappear.
    keep_editing_as_button = len(options) < 4
    if keep_editing_as_button:
        options.append("'Nothing, keep editing' -> say the draft stays open and wait")
        lead_in = ""
    else:
        lead_in = (
            "Say in the line before the question that they can also just keep "
            "editing, or say what to change. "
        )

    return (
        lead_in
        + f"Finally ask ONE AskUserQuestion, 'What next for {service_name}?', with "
        f"exactly these options in this order and nothing else: "
        + "; ".join(options)
        + ". Use a dialog, never a written list — these are buttons. A free-text "
        f"answer that changes a configuration value goes to {value_route}, which "
        f"re-saves the draft. Do not act on any option before the user picks it."
    )


async def _save_gateway_draft_from_ticket(
    *, auth_ctx, ticket_code: str, project_id: Optional[str], cached: dict
) -> dict:
    """The gateway half of the web's Save: one card from `kong_route_form`,
    merged into the service's current gateway and parked as the caller's
    `add_route` draft row for that scope (same change set as the settings
    draft, so submit / approve / deploy move both).

    Steps: the service's configuration in that env/region (the row the gateway
    scope hangs off) → GET the gateway state → build the GatewayGroupSave the
    way the Gateway tab does → POST /transaction/kong-gateway/{sc}.
    """
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt
    from app.repository.service_config_repository import ServiceConfigRepository

    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    try:
        group = sp.gateway_group_block(cached)
        environment = EnvironmentEnum(group["environment"])
    except sp.ServicePayloadError as e:
        return {"status": "error", "reason": "fields_incomplete", "message": str(e)}
    except ValueError as e:
        return {"status": "error", "message": f"Unknown environment from chatbot: {e}"}

    service_mst_code: str = group["service_mst_code"]
    geo_loc_mst_code: str = group["geo_loc_mst_code"]
    service_name: str = group.get("service_name") or service_mst_code

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {"status": "error", "message": "MCP_INTERNAL_JWT_SECRET is not configured."}

    async with AsyncSessionLocal() as db:
        existing_config = await ServiceConfigRepository(db).get_by_tenant_service_env_geo_loc(
            tenant_code, service_mst_code, environment.value, geo_loc_mst_code
        )
    if existing_config is None:
        return {
            "status": "error",
            "reason": "not_found",
            "message": (
                f"**{service_name}** has no configuration in {environment.value} / "
                f"{geo_loc_mst_code}, so there is no gateway to add routes to. Create "
                f"the service there first."
            ),
        }
    service_config_code = existing_config.code

    try:
        state = await obs_tool_client.get_gateway_state(jwt_token=jwt_token, service_config_code=service_config_code)
    except Exception as e:
        logger.exception("devlift_mcp gateway draft: gateway state failed for %s", service_config_code)
        return _obs_tool_api_error("read the service's gateway", e, service_name)

    try:
        save, summary = sp.build_gateway_group_save(group, state)
    except sp.GatewayConflict as e:
        return {"status": "error", "reason": "invalid_configuration", "message": str(e)}

    base = {
        "service_name": service_name,
        "service_code": service_mst_code,
        "service_config_code": service_config_code,
        "environment": environment.value,
        "geo_loc_mst_code": geo_loc_mst_code,
        "ticket_code": ticket_code,
        "group": summary,
    }
    auth_word = "Auth" if summary["secured"] else "No Auth"
    card = f"{summary['http_method']} {auth_word}, tag {summary['route_group_key']}"
    nothing_new = {
        **base,
        "status": "no_changes",
        "queue_code": None,
        "message": (
            f"Those routes are already on **{service_name}** ({card}); nothing to save."
        ),
        "next_action": {"type": "present_completion", "instruction": "Tell the user. Do not call any deployment tool."},
    }
    if save is None:
        return nothing_new

    # Every card already pending in this user's draft rides along: obs_tool
    # replaces the row's groups with what the save sends (the web resends
    # all of them), so a save carrying only the new card would drop the rest.
    pending_entries: list = []
    try:
        listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=service_config_code)
        for row in listing.get("approvals") or []:
            if _is_gateway_item(row) and (row.get("you") or {}).get("mine") and row.get("status") == "draft":
                pending_entries = (row.get("config_snapshot") or {}).get("groups") or []
                break
    except Exception:
        logger.exception("devlift_mcp gateway draft: pending gateway lookup failed (non-fatal)")
    batch = sp.merge_gateway_batch(save, pending_entries)

    try:
        draft = await obs_tool_client.save_gateway_draft(
            jwt_token=jwt_token, service_config_code=service_config_code, gateway_groups=batch
        )
    except Exception as e:
        logger.exception("devlift_mcp gateway draft: kong-gateway transaction call failed")
        return _obs_tool_api_error("save the gateway routes draft", e, service_name)

    approval = draft.get("approval") if isinstance(draft, dict) else None
    if not isinstance(approval, dict) or not approval:
        return nothing_new  # obs_tool found every claimed change already live
    queue_code = approval.get("code")
    queue_status = approval.get("status")
    groups = (approval.get("changes") or {}).get("groups") or []
    gateway_changes = sp.summarize_gateway_groups(groups)

    # Same Redis entry as the configuration draft of this service, when there
    # is one, so every later tool finds one service under one handle.
    configuration_ticket: Optional[str] = None
    try:
        existing_entry = None
        for candidate in await get_all_drafts(user_code, _SERVICE_DRAFT_KEY):
            if candidate.get("transaction_code") == service_config_code and candidate.get("status") != "discarded":
                existing_entry = candidate
                break
        if existing_entry:
            configuration_ticket = existing_entry.get("ticket_code")
            updated = {
                **existing_entry,
                "queue_code": existing_entry.get("queue_code") or queue_code or "",
                "queue_status": existing_entry.get("queue_status") or queue_status,
                "gateway_queue_code": queue_code,
                "gateway_ticket_code": ticket_code,
            }
            await update_draft_by_id(user_code, _SERVICE_DRAFT_KEY, existing_entry["draft_id"], updated)
        else:
            entry = {
                **_build_draft_entry(
                    draft_id=generate_draft_id(),
                    project_id=project_id,
                    ticket_code=ticket_code,
                    queue_code=queue_code or "",
                    queue_id=None,
                    transaction_code=service_config_code,
                    transaction_table="service_config",
                    identifier=service_name,
                    resource_type="eks_service",
                    deployment_environment=environment.value,
                    status="pending",
                ),
                "services_mst_code": service_mst_code,
                "queue_status": queue_status,
                "gateway_queue_code": queue_code,
                "gateway_ticket_code": ticket_code,
            }
            await add_draft(user_code, _SERVICE_DRAFT_KEY, entry)
    except Exception:
        logger.exception("devlift_mcp gateway draft: failed to record Redis draft (non-fatal)")

    added = summary["added_paths"]
    skipped = summary["already_present"]
    message = (
        f"Gateway routes for **{service_name}** in {environment.value} are saved as a "
        f"draft ({card}): {len(added)} path{'s' if len(added) != 1 else ''} added."
    )
    if skipped:
        message += " Already there, skipped: " + ", ".join(skipped) + "."
    return {
        **base,
        "status": "success",
        "action": "gateway_draft_saved",
        "queue_code": queue_code,
        "queue_status": queue_status,
        "configuration_ticket_code": configuration_ticket,
        "change_count": sp.count_gateway_changes(groups),
        "gateway_changes": gateway_changes,
        "you": {
            k: (approval.get("you") or {}).get(k)
            for k in ("mine", "can_approve", "can_deploy", "can_write_settings")
        },
        "message": message,
        "next_action": {
            "type": "present_gateway_draft",
            "instruction": (
                "Surface `message`. Render `gateway_changes` as a markdown table titled "
                "'Gateway routes' with columns Method | Auth | Tag | Change: one row per "
                "entry of `added` ('+ path'), `removed` ('− path') and `changed` "
                "('old → new'), plus one row for `plugins` ('plugins: a, b → c') and one "
                "for `regex_priority` ('priority: 0 → 10') when present. Values EXACTLY "
                "as given. Do NOT submit on your own and do not call any deployment tool. "
            ) + _after_draft_question(
                service_name, ticket_code, configuration_ticket, approval.get("you") or {}
            ),
        },
    }


# ============================================================
# Review lane — the author's own change request
# ============================================================
# submit (draft -> submit), withdraw (submit -> draft), discard (draft -> gone).
# Each is one call to obs_tool's own /approvals/{queue_code}/{verb} route with
# the caller's identity; the route enforces state, ownership and OpenFGA.

_LANE_PREVIOUS_STATUS = {
    # author verbs
    "submit": "draft",
    "withdraw": "submit",
    "discard": "draft",
    # reviewer verbs ("reject" in the product = the request-changes route)
    "approve": "submit",
    "request-changes": "submit",
    "revoke": "approved",
    # shipping
    "deploy": "approved",
}


def _service_request_label(item: dict) -> str:
    """Short human label for a change request from an ApprovalItem dict."""
    name = item.get("display_name") or item.get("resource_code") or item.get("code")
    env = ((item.get("config_snapshot") or {}).get("environment")) or ""
    status = item.get("status") or ""
    return " · ".join(p for p in (str(name), str(env), str(status)) if p)


def _name_matches(candidate: Optional[str], wanted: str) -> bool:
    """Does a stored name refer to the service the user named?

    Accepts the bare name, the `-service` suffixed form, and the queue row's
    display_name format ("update service:<name> <region>") by token.
    """
    import re

    if not candidate or not wanted:
        return False
    c = candidate.strip().lower()
    w = wanted.strip().lower()
    if c in (w, f"{w}-service") or w in (c, f"{c}-service"):
        return True
    tokens = {t for t in re.split(r"[:\s]+", c) if t}
    return w in tokens or f"{w}-service" in tokens or (w.endswith("-service") and w[:-8] in tokens)


async def _find_redis_service_entry(user_code: str, *, ticket_code=None, queue_code=None, service_name=None):
    """The Redis entry create_service_and_save_draft wrote, by any handle."""
    for entry in reversed(await get_all_drafts(user_code, _SERVICE_DRAFT_KEY)):
        if entry.get("status") == "discarded":
            continue
        if queue_code and queue_code in (entry.get("queue_code"), entry.get("gateway_queue_code")):
            return entry
        if ticket_code and ticket_code in (entry.get("ticket_code"), entry.get("gateway_ticket_code")):
            return entry
        if service_name and _name_matches(entry.get("identifier"), service_name):
            return entry
    return None


async def _resolve_service_request(
    *,
    user_code: str,
    jwt_token: str,
    queue_code: Optional[str],
    ticket_code: Optional[str],
    service_name: Optional[str],
    expected_status: str,
) -> tuple[Optional[str], Optional[dict]]:
    """Turn whatever handle the LLM has into a queue_code.

    Order: explicit queue_code → the ticket's Redis entry → the caller's own
    requests in `expected_status` matched by service name (drafts are private
    to their author, so `status=draft` lists exactly their drafts).
    """
    if queue_code:
        return queue_code, None

    if ticket_code:
        entry = await _find_redis_service_entry(user_code, ticket_code=ticket_code)
        if entry and entry.get("queue_code"):
            return entry["queue_code"], None

    if service_name:
        from app.mcp_servers.devlift_mcp import obs_tool_client

        try:
            listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, status=expected_status)
        except Exception as e:
            logger.exception("devlift_mcp service_request: list_approvals failed")
            return None, _obs_tool_api_error("look up your change requests", e, service_name)
        matches = _one_row_per_change_set([
            item for item in (listing.get("approvals") or [])
            if _name_matches(item.get("display_name"), service_name)
            or _name_matches(item.get("resource_code"), service_name)
        ])
        if len(matches) == 1:
            return matches[0]["code"], None
        if len(matches) > 1:
            return None, {
                "status": "error",
                "reason": "ambiguous_target",
                "message": (
                    f"There are {len(matches)} change requests for '{service_name}' "
                    f"in status '{expected_status}'. Which one do you mean?"
                ),
                "next_action": {
                    "type": "choose",
                    "options": [
                        {"label": _service_request_label(m), "value": m["code"]} for m in matches
                    ],
                    "instruction": (
                        "Present the options (AskUserQuestion when 4 or fewer, "
                        "else a NUMBERED list the user can answer by number or "
                        "name) and call this tool again with the "
                        "chosen value as queue_code."
                    ),
                },
            }
        # Fall back to the Redis entry by name (covers a ticket from a
        # previous conversation whose status moved on).
        entry = await _find_redis_service_entry(user_code, service_name=service_name)
        if entry and entry.get("queue_code"):
            return entry["queue_code"], None
        return None, {
            "status": "error",
            "reason": "not_found",
            "message": (
                f"I couldn't find a change request for '{service_name}' in status "
                f"'{expected_status}' that belongs to you."
            ),
        }

    return None, {
        "status": "error",
        "reason": "missing_target",
        "message": "Which service? Tell me the service name (or pass the ticket_code from the chat).",
    }


async def _service_request_verb_handler(
    *,
    verb: str,
    tool_name: str,
    queue_code: Optional[str],
    ticket_code: Optional[str],
    service_name: Optional[str],
    comment: Optional[str],
    success_message: str,
    step_label: str,
    require_comment: bool = False,
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    if require_comment and not (comment or "").strip():
        return {
            "status": "error",
            "reason": "missing_reason",
            "message": (
                "A reason is required so the author knows what to change. "
                "Ask the user for it, then call again with `reason`."
            ),
        }

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    expected_status = _LANE_PREVIOUS_STATUS[verb]
    resolved, err = await _resolve_service_request(
        user_code=user_code,
        jwt_token=jwt_token,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        expected_status=expected_status,
    )
    if err:
        return err

    entry = await _find_redis_service_entry(user_code, queue_code=resolved)
    display_name = service_name or (entry or {}).get("identifier") or resolved

    try:
        result = await obs_tool_client.approval_action(
            jwt_token=jwt_token, queue_code=resolved, verb=verb, comment=comment
        )
    except Exception as e:
        logger.exception("devlift_mcp %s: /approvals/%s/%s failed", tool_name, resolved, verb)
        return _obs_tool_api_error(step_label, e, display_name)

    approval = result.get("approval") if isinstance(result, dict) else None
    new_status = (approval or {}).get("status") or ("discarded" if verb == "discard" else None)
    if display_name == resolved and approval and approval.get("display_name"):
        # Nothing better than the queue code was known; the row's display
        # name ("update service:<name> <region>") is still more readable.
        display_name = approval["display_name"]

    # Keep the Redis entry in step so later tools (and a retry) see the truth.
    if entry is not None:
        try:
            await update_draft_by_id(
                user_code,
                _SERVICE_DRAFT_KEY,
                entry["draft_id"],
                {
                    **entry,
                    "queue_status": new_status,
                    "status": "discarded" if verb == "discard" else entry.get("status", "pending"),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.exception("devlift_mcp %s: failed to update Redis entry (non-fatal)", tool_name)

    response: dict = {
        "status": "success",
        "action": verb,
        "queue_code": resolved,
        "queue_status": new_status,
        "service_name": display_name,
        "message": success_message.format(name=display_name),
        "next_action": {
            "type": "present_completion",
            "instruction": (
                "Tell the user. Do not call any deployment tool, and do not "
                "chain another review-lane verb unless the user asks."
            ),
        },
    }
    if approval:
        # The verb moved the whole change set (settings + gateway rows). The
        # route answers with the row it was called on; fetch the other half so
        # the user sees the same "Changes" view as the web: Configuration, then
        # Gateway routes.
        settings_item = None if _is_gateway_item(approval) else approval
        gateway_item = approval if _is_gateway_item(approval) else None
        if approval.get("resource_code"):
            try:
                listing = await obs_tool_client.list_approvals(
                    jwt_token=jwt_token, resource_code=approval["resource_code"]
                )
                for sibling in listing.get("approvals") or []:
                    if sibling.get("code") == approval.get("code") or _change_set_key(sibling) != _change_set_key(approval):
                        continue
                    if gateway_item is None and _is_gateway_item(sibling):
                        gateway_item = sibling
                    elif settings_item is None and _is_settings_item(sibling):
                        settings_item = sibling
            except Exception:
                logger.exception("devlift_mcp %s: sibling lookup failed (non-fatal)", tool_name)
        if settings_item and settings_item.get("changes"):
            response["changes"] = settings_item["changes"]
        if gateway_item is not None:
            from app.mcp_servers.devlift_mcp import service_payloads as sp

            groups = (gateway_item.get("changes") or {}).get("groups") or []
            response["gateway_changes"] = sp.summarize_gateway_groups(groups)
            response["gateway_queue_code"] = gateway_item.get("code")
        response["change_count"] = len(response.get("changes") or {}) + (
            _gateway_change_count(gateway_item) if gateway_item else 0
        )
        if response.get("changes") or response.get("gateway_changes"):
            response["next_action"]["instruction"] = (
                "Tell the user, then show what the request holds, like the web's "
                "Changes view: `changes` (when present) as a table titled "
                "'Configuration' with columns Field | Deployed → Requested (`from` as "
                "'–' when null, values exactly as given); `gateway_changes` (when "
                "present) as a table titled 'Gateway routes' with columns Method | "
                "Auth | Tag | Change ('+ path', '− path', 'old → new', plugin and "
                "priority moves). Do not call any deployment tool, and do not chain "
                "another review-lane verb unless the user asks."
            )
        you = approval.get("you") or {}
        if you:
            response["you"] = {
                k: you.get(k) for k in ("mine", "can_approve", "can_deploy", "can_write_settings")
            }
    if isinstance(result, dict) and result.get("detail"):
        response["detail"] = result["detail"]
    return response


async def submit_service_request_handler(
    *, queue_code=None, ticket_code=None, service_name=None, comment=None
) -> dict:
    """draft -> submit. Freezes the diff and notifies approvers."""
    return await _service_request_verb_handler(
        verb="submit",
        tool_name="submit_service_request",
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        comment=comment,
        success_message=(
            "**{name}** is submitted for review. The approvers have been notified; "
            "you'll be able to deploy once it is approved."
        ),
        step_label="submit the change request",
    )


async def withdraw_service_request_handler(
    *, queue_code=None, ticket_code=None, service_name=None, comment=None
) -> dict:
    """submit -> draft, by the submitter."""
    return await _service_request_verb_handler(
        verb="withdraw",
        tool_name="withdraw_service_request",
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        comment=comment,
        success_message="**{name}** is withdrawn from review and back with you as a draft.",
        step_label="withdraw the change request",
    )


async def discard_service_request_handler(
    *, queue_code=None, ticket_code=None, service_name=None
) -> dict:
    """draft -> gone (soft delete), by the author."""
    return await _service_request_verb_handler(
        verb="discard",
        tool_name="discard_service_request",
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        comment=None,
        success_message=(
            "The draft for **{name}** was discarded. The live configuration is unchanged."
        ),
        step_label="discard the draft",
    )


# ============================================================
# Review lane — the reviewer's side
# ============================================================
# What the web's approval UI offers, and nothing more: the inbox, one request
# with its frozen diff, Approve, "Reject" (= the request-changes route: back to
# the author as a draft with the reason; the terminal reject is deliberately
# not exposed), and Revoke after approval. Visibility and permission are the
# backend's: `you.can_approve` is resolved per request and already applies the
# resource group's self-approval rule.

_REVIEW_LANE_STATUSES = ("submit", "approved")


def _clean_service_label(item: dict) -> str:
    """'update service:demo region-x' -> 'demo'; else the display name / code."""
    import re

    name = item.get("display_name") or ""
    m = re.match(r"^\s*update service:\s*(\S+)", name, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.match(r"^\s*gateway routes\s*-\s*(\S+)", name, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return name or item.get("resource_code") or item.get("code") or ""


# A change set is one service's open request: the settings row
# (case_ref_code update_service) and, when routes were touched, a gateway row
# (add_route) sharing its transaction_code, author and status. The web saves
# them with two requests and reviews them as one; the tools do the same.
_GATEWAY_CASE_REF = "add_route"
_VARIABLES_CASE_REF = "update_variables"


async def _approval_from_list(*, jwt_token: str, queue_code: str) -> Optional[dict]:
    """One request, read through `GET /approvals` instead of `/approvals/{code}`.

    The single-request endpoint answers only callers who may APPROVE the row
    and 404s everyone else. That is the wrong question once a request is
    approved: the right to ship it is `can_deploy`, granted separately, so a
    user with write + deploy whose change a COLLEAGUE approved was refused the
    very request they were entitled to deploy.

    The list's own rule is `mine or can_approve or can_deploy` — exactly the
    people with business here — and both endpoints build their items with the
    same `_to_item`, so the shape is identical down to `changes` and
    `config_snapshot`. It is also what the web does: its Deploy button takes
    the row from the list and posts straight to `/approvals/{code}/deploy`,
    never calling the single-request endpoint (which has no caller in the
    frontend at all).

    Returns None when the row is not in the caller's list, which callers report
    the way they used to report a 404.
    """
    from app.mcp_servers.devlift_mcp import obs_tool_client

    listing = await obs_tool_client.list_approvals(jwt_token=jwt_token)
    for row in listing.get("approvals") or []:
        if row.get("code") == queue_code:
            return row
    return None


def _is_gateway_item(item: dict) -> bool:
    return (item.get("case_ref_code") or "") == _GATEWAY_CASE_REF


def _is_settings_item(item: dict) -> bool:
    return (item.get("case_ref_code") or "") not in (_GATEWAY_CASE_REF, _VARIABLES_CASE_REF)


def _lane_rows(items: list) -> tuple[Optional[dict], Optional[dict]]:
    """(settings row, gateway row) of the request that matters for a service."""
    settings = _pick_pending_request([i for i in items if _is_settings_item(i)])
    gateway = _pick_pending_request([i for i in items if _is_gateway_item(i)])
    return settings, gateway


def _change_set_key(item: dict) -> tuple:
    return (item.get("resource_code"), item.get("requested_by"), item.get("status"))


def _one_row_per_change_set(items: list) -> list[dict]:
    """Collapse settings + gateway rows of one change set into the settings
    row (or the gateway row when the request is routes-only), carrying the
    sibling under `_gateway` / `_settings` for the summary."""
    by_key: dict = {}
    for item in items:
        key = _change_set_key(item)
        slot = by_key.setdefault(key, {"settings": None, "gateway": None, "other": None})
        if _is_gateway_item(item):
            slot["gateway"] = slot["gateway"] or item
        elif _is_settings_item(item):
            slot["settings"] = slot["settings"] or item
        else:
            slot["other"] = slot["other"] or item
    out = []
    for slot in by_key.values():
        primary = slot["settings"] or slot["gateway"] or slot["other"]
        if primary is None:
            continue
        merged = dict(primary)
        if slot["settings"] and slot["gateway"]:
            merged["_gateway"] = slot["gateway"]
        out.append(merged)
    return out


def _gateway_change_count(item: Optional[dict]) -> int:
    from app.mcp_servers.devlift_mcp import service_payloads as sp

    if not item:
        return 0
    return sp.count_gateway_changes((item.get("changes") or {}).get("groups") or [])


def _approval_summary(item: dict) -> dict:
    you = item.get("you") or {}
    snapshot = item.get("config_snapshot") or {}
    gateway_item = item.get("_gateway") or (item if _is_gateway_item(item) else None)
    settings_changes = 0 if _is_gateway_item(item) else len(item.get("changes") or {})
    gateway_changes = _gateway_change_count(gateway_item)
    includes = [k for k, n in (("configuration", settings_changes), ("gateway", gateway_changes)) if n]
    return {
        "queue_code": item.get("code"),
        "status": item.get("status"),
        "service_name": _clean_service_label(item),
        "environment": snapshot.get("environment"),
        "requested_by": item.get("requested_by_name") or item.get("requested_by"),
        "requested_at": item.get("requested_at"),
        "change_count": settings_changes + gateway_changes,
        "includes": includes,
        "mine": bool(you.get("mine")),
        "can_approve": bool(you.get("can_approve")),
        "can_deploy": bool(you.get("can_deploy")),
    }


def _decisions_for(item: dict) -> list[str]:
    """Which decision tools apply to this request for THIS caller."""
    status = item.get("status")
    you = item.get("you") or {}
    if status == "submit":
        if you.get("can_approve"):
            return ["approve", "reject"]
        if you.get("mine"):
            return ["withdraw"]
        return []
    if status == "approved":
        out = []
        if you.get("can_approve"):
            out.append("revoke")
        if you.get("can_deploy"):
            out.append("deploy")
        return out
    if status == "draft" and you.get("mine"):
        return ["submit", "discard"]
    return []


async def list_pending_approvals_handler() -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {"status": "error", "message": "MCP_INTERNAL_JWT_SECRET is not configured."}

    waiting: list[dict] = []
    approved: list[dict] = []
    for status in _REVIEW_LANE_STATUSES:
        try:
            listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, status=status)
        except Exception as e:
            logger.exception("devlift_mcp list_pending_approvals: listing %s failed", status)
            return _obs_tool_api_error("list the requests waiting for you", e, "approvals")
        for item in _one_row_per_change_set(listing.get("approvals") or []):
            if not (item.get("you") or {}).get("can_approve"):
                continue
            (waiting if status == "submit" else approved).append(_approval_summary(item))

    waiting.sort(key=lambda i: str(i.get("requested_at") or ""), reverse=True)
    approved.sort(key=lambda i: str(i.get("requested_at") or ""), reverse=True)

    if not waiting and not approved:
        message = "Nothing is waiting for your review."
    else:
        parts = []
        if waiting:
            parts.append(f"{len(waiting)} request{'s' if len(waiting) != 1 else ''} waiting for your review")
        if approved:
            parts.append(f"{len(approved)} approved by you (can still be revoked)")
        message = ", ".join(parts) + "."

    return {
        "status": "success",
        "waiting_for_review": waiting,
        "approved_by_you": approved,
        "message": message,
        "next_action": {
            "type": "present_list",
            "instruction": (
                "Surface `message`. Render `waiting_for_review` (then "
                "`approved_by_you`) as a table: Service | Environment | Requested by | "
                "Changes | Requested at. `includes` says whether the request touches "
                "the configuration, the gateway routes, or both. Mark rows where "
                "`mine` is true as the user's own. Then ask which request they want to look at — call "
                "review_service_request with its queue_code. Never approve, reject "
                "or revoke without an explicit decision from the user."
            ),
        },
    }


async def review_service_request_handler(
    *, queue_code: Optional[str] = None, service_name: Optional[str] = None
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {"status": "error", "message": "MCP_INTERNAL_JWT_SECRET is not configured."}

    resolved = queue_code
    if not resolved:
        if not service_name:
            return {
                "status": "error",
                "reason": "missing_target",
                "message": "Which request? Give the service name, or pick one from list_pending_approvals.",
            }
        # A reviewer's request is found in the lane (submitted first, then approved).
        for status in _REVIEW_LANE_STATUSES:
            resolved, err = await _resolve_service_request(
                user_code=user_code,
                jwt_token=jwt_token,
                queue_code=None,
                ticket_code=None,
                service_name=service_name,
                expected_status=status,
            )
            if resolved or (err and err.get("reason") == "ambiguous_target"):
                break
        if err and not resolved:
            return err

    # From the list, not GET /approvals/{code} — see _approval_from_list. A
    # request is opened here on the way to deploying it too, and the deployer
    # is often NOT an approver, so gating the read on can_approve refused them
    # a request that was already approved and theirs to ship.
    try:
        item = await _approval_from_list(jwt_token=jwt_token, queue_code=resolved)
    except obs_tool_client.ObsToolAPIError as e:
        return _obs_tool_api_error("open the request", e, resolved)
    except Exception as e:
        logger.exception("devlift_mcp review_service_request: approval lookup failed")
        return _obs_tool_api_error("open the request", e, resolved)

    if item is None:
        return {
            "status": "error",
            "reason": "not_found",
            "message": (
                f"I can't find a request '{resolved}'. It may not exist, or it "
                f"may not be one you can see."
            ),
        }

    # The other half of the change set (routes next to settings, or the
    # reverse), so the reviewer sees the whole request whichever row they hold.
    settings_item: Optional[dict] = None if _is_gateway_item(item) else item
    gateway_item: Optional[dict] = item if _is_gateway_item(item) else None
    if item.get("resource_code"):
        try:
            listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=item["resource_code"])
            for sibling in listing.get("approvals") or []:
                if sibling.get("code") == item.get("code") or _change_set_key(sibling) != _change_set_key(item):
                    continue
                if gateway_item is None and _is_gateway_item(sibling):
                    gateway_item = sibling
                elif settings_item is None and _is_settings_item(sibling):
                    settings_item = sibling
        except Exception:
            logger.exception("devlift_mcp review_service_request: sibling lookup failed (non-fatal)")

    merged = dict(settings_item or item)
    if settings_item is not None and gateway_item is not None:
        merged["_gateway"] = gateway_item
    summary = _approval_summary(merged)
    decisions = _decisions_for(item)
    changes = (settings_item or {}).get("changes") or {}
    gateway_changes = None
    if gateway_item is not None:
        from app.mcp_servers.devlift_mcp import service_payloads as sp

        gateway_changes = sp.summarize_gateway_groups((gateway_item.get("changes") or {}).get("groups") or [])
    history = [
        {k: h.get(k) for k in ("event", "by_name", "by", "at", "comment") if h.get(k) is not None}
        for h in (item.get("history") or [])
    ]
    status_word = {"submit": "submitted", "approved": "approved", "draft": "a draft"}.get(summary["status"], summary["status"])
    message = (
        f"**{summary['service_name']}** ({summary['environment'] or '?'}): {status_word} by "
        f"{summary['requested_by'] or 'someone'}, {summary['change_count']} change(s)"
        + (" (configuration and gateway routes)" if len(summary["includes"]) == 2 else
           " (gateway routes)" if summary["includes"] == ["gateway"] else "")
        + "."
    )
    if decisions:
        message += " You can: " + ", ".join(decisions) + "."
    else:
        message += " No decision is available to you on it."

    return {
        **{k: v for k, v in summary.items() if k != "status"},
        "queue_code": item.get("code"),
        "request_status": summary["status"],
        "status": "success",
        "changes": changes,
        "gateway_changes": gateway_changes,
        "history": history,
        "decisions_available": decisions,
        "message": message,
        "next_action": {
            "type": "present_request",
            "instruction": (
                "Surface `message`, then render `changes` (when non-empty) as a markdown "
                "table titled 'Changes' with columns Field | Deployed → Requested (`from` "
                "as '–' when null, values exactly as given). Then, when `gateway_changes` "
                "is present, a table titled 'Gateway routes' with columns Method | Auth | "
                "Tag | Change: one row per `added` ('+ path'), `removed` ('− path'), "
                "`changed` ('old → new'), and one for `plugins` / `regex_priority` moves "
                "when present. List `decisions_available` as the options and WAIT for "
                "the user's decision. 'reject' needs a reason — ask for it before calling "
                "reject_service_request. Do not call any decision tool on your own."
            ),
        },
    }


async def approve_service_request_handler(
    *, queue_code=None, service_name=None, comment=None
) -> dict:
    """submit -> approved (snapshot sealed). The backend enforces can_approve
    and the resource group's self-approval rule."""
    return await _service_request_verb_handler(
        verb="approve",
        tool_name="approve_service_request",
        queue_code=queue_code,
        ticket_code=None,
        service_name=service_name,
        comment=comment,
        success_message=(
            "**{name}** is approved. Whoever holds deploy permission on it can now deploy."
        ),
        step_label="approve the request",
    )


async def reject_service_request_handler(
    *, queue_code=None, service_name=None, reason=None
) -> dict:
    """'Reject' as the product means it: back to the author as a draft, values
    intact, with the reason — the request-changes route. The terminal reject
    route is deliberately not used (the web does not offer it either)."""
    result = await _service_request_verb_handler(
        verb="request-changes",
        tool_name="reject_service_request",
        queue_code=queue_code,
        ticket_code=None,
        service_name=service_name,
        comment=reason,
        success_message="**{name}** is rejected and sent back to its author as a draft.",
        step_label="reject the request",
        require_comment=True,
    )
    if result.get("status") == "success":
        result["action"] = "reject"
        result["reason_given"] = reason
        result["message"] = f"{result['message']} Reason: {reason}"
    return result


async def revoke_approval_handler(
    *, queue_code=None, service_name=None, comment=None
) -> dict:
    """approved -> submit. Refused by the backend once a deployment is in flight."""
    return await _service_request_verb_handler(
        verb="revoke",
        tool_name="revoke_approval",
        queue_code=queue_code,
        ticket_code=None,
        service_name=service_name,
        comment=comment,
        success_message="The approval on **{name}** is revoked; it is back in the review queue.",
        step_label="revoke the approval",
    )


# ============================================================
# Service details — live configuration + pending request preview
# ============================================================
# Read-only. Mirrors the web's service details page and its preview tab:
#   GET /service-configs/by-code/{sc}   (can_view_settings)  -> live config
#   GET /approvals?resource_code={sc}   (yours / reviewable) -> request + diff
# Visibility is the backend's: a draft is seen by its author only; a
# submitted request by its author and by those who may approve or deploy it.

# ============================================================
# Deploy — approved -> shipping (the web's Deploy button)
# ============================================================
# Two calls, in the web's order: the gate (POST /approvals/{code}/deploy —
# status, can_deploy, seal) and the start (POST /deployments/multiple-deploy
# with the settings row id and, when routes were approved, the gateway part).
# The whole change set ships as one Temporal batch; get_deployment_status
# then polls it through the Redis entry, exactly like a resource deploy.

_SERVICE_DEPLOY_POLL_NEXT_ACTION = (
    "Immediately invoke the `/loop` skill now — do not wait for the user. "
    "Run: /loop 30s call get_deployment_status() — show the current `stage` "
    "each tick, present `pr_url` as a clickable link the first time it appears, "
    "and stop when all_completed=true or status=no_deployments."
)


async def deploy_service_request_handler(
    *,
    queue_code: Optional[str] = None,
    ticket_code: Optional[str] = None,
    service_name: Optional[str] = None,
    confirmed: bool = False,
    confirm_service_name: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    tool_name = "deploy_service_request"
    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {"status": "error", "message": "MCP_INTERNAL_JWT_SECRET is not configured."}

    resolved, err = await _resolve_service_request(
        user_code=user_code,
        jwt_token=jwt_token,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
        expected_status="approved",
    )
    if err:
        return err

    # From the list, not GET /approvals/{code} — see _approval_from_list.
    # Deploying asks for can_deploy and nothing else; that endpoint asks for
    # can_approve, which is a different right and often held by someone else.
    try:
        item = await _approval_from_list(jwt_token=jwt_token, queue_code=resolved)
    except obs_tool_client.ObsToolAPIError as e:
        return _obs_tool_api_error("open the request", e, service_name or resolved)
    except Exception as e:
        logger.exception("devlift_mcp %s: approval lookup failed", tool_name)
        return _obs_tool_api_error("open the request", e, service_name or resolved)

    if item is None:
        return {
            "status": "error",
            "reason": "not_found",
            "message": f"I can't find a request '{resolved}' that you may deploy.",
        }

    # The change set: the settings row is the one the web gates on; the
    # approved gateway row ships with it by code.
    settings_item: Optional[dict] = None if _is_gateway_item(item) else item
    gateway_item: Optional[dict] = item if _is_gateway_item(item) else None
    if item.get("resource_code"):
        try:
            listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=item["resource_code"])
            for sibling in listing.get("approvals") or []:
                if sibling.get("code") == item.get("code") or _change_set_key(sibling) != _change_set_key(item):
                    continue
                if gateway_item is None and _is_gateway_item(sibling):
                    gateway_item = sibling
                elif settings_item is None and _is_settings_item(sibling):
                    settings_item = sibling
        except Exception:
            logger.exception("devlift_mcp %s: sibling lookup failed (non-fatal)", tool_name)

    primary = settings_item or item
    real_name = _clean_service_label(primary)
    display_name = service_name or real_name
    status = primary.get("status")
    you = primary.get("you") or {}
    snapshot = primary.get("config_snapshot") or {}
    environment = snapshot.get("environment") or ((gateway_item or {}).get("config_snapshot") or {}).get("environment")
    production = str(environment or "").lower().startswith("prod")
    sc_code = primary.get("resource_code")
    changes = (settings_item or {}).get("changes") or {}
    gateway_changes = None
    if gateway_item is not None:
        gateway_changes = sp.summarize_gateway_groups(((gateway_item.get("changes") or {}).get("groups") or []))
    shipped = [k for k, present in (("configuration", bool(changes)), ("gateway", gateway_item is not None)) if present]

    base = {
        "service_name": display_name,
        "queue_code": primary.get("code"),
        "gateway_queue_code": (gateway_item or {}).get("code"),
        "service_config_code": sc_code,
        "environment": environment,
        "production": production,
        "shipped": shipped,
        "changes": changes,
        "gateway_changes": gateway_changes,
    }

    if status != "approved":
        hint = {
            "draft": "it is still a draft — submit it for review first",
            "submit": "it is waiting for an approver",
        }.get(status, f"its status is '{status}'")
        return {**base, "status": "error", "reason": "wrong_status",
                "message": f"**{display_name}** cannot be deployed: {hint}."}
    if not you.get("can_deploy"):
        return {**base, "status": "error", "reason": "permission_denied",
                "message": f"You may not deploy **{display_name}**: the deploy right on this service belongs to someone else."}
    if you.get("deploy_in_flight"):
        return {**base, "status": "error", "reason": "already_deploying",
                "message": f"**{display_name}** is already being deployed. Say 'deployment status' to follow it.",
                "next_action": {"type": "present_completion", "instruction": "Tell the user; offer get_deployment_status."}}

    # Confirmation is the tool's, not the LLM's: the web shows a confirm
    # dialog, and prod makes the user type the service name.
    typed = (confirm_service_name or "").strip().lower()
    if not confirmed or (production and typed != real_name.lower()):
        if production and confirmed and typed and typed != real_name.lower():
            return {**base, "status": "error", "reason": "confirmation_mismatch",
                    "message": f"'{confirm_service_name}' does not match the service name **{real_name}**. Nothing was deployed."}
        what = " and ".join(
            (["the configuration change(s)"] if changes else [])
            + ([f"{sp.count_gateway_changes(((gateway_item or {}).get('changes') or {}).get('groups') or [])} gateway route change(s)"] if gateway_item else [])
        ) or "no changes"
        return {
            **base,
            "status": "needs_confirmation",
            "message": (
                f"Ready to deploy **{display_name}** to {environment}: {what}."
                + (" This is PRODUCTION: a promotion PR to main is raised and applied." if production else "")
            ),
            "next_action": {
                "type": "confirm_deploy",
                "instruction": (
                    "Surface `message`. Render `changes` (when non-empty) as a table titled "
                    "'Configuration' with columns Field | Deployed → Requested, and "
                    "`gateway_changes` (when present) as a table titled 'Gateway routes' "
                    "with columns Method | Auth | Tag | Change. Then "
                    + (
                        f"ask the user to TYPE the service name to confirm a production "
                        f"deploy (no pills), and call deploy_service_request again with "
                        f"queue_code='{primary.get('code')}', confirmed=true, "
                        f"confirm_service_name=<what they typed>."
                        if production else
                        f"ask ONE AskUserQuestion 'Deploy {display_name} to {environment}?' "
                        f"with the options 'Deploy' and 'Cancel'. On Deploy, call "
                        f"deploy_service_request again with queue_code='{primary.get('code')}' "
                        f"and confirmed=true in the same turn; on Cancel say nothing was deployed."
                    )
                ),
            },
        }

    # 1. The gate.
    try:
        await obs_tool_client.approval_action(jwt_token=jwt_token, queue_code=primary["code"], verb="deploy")
    except Exception as e:
        logger.exception("devlift_mcp %s: deploy gate failed", tool_name)
        return _obs_tool_api_error("clear the deployment", e, display_name)

    # 2. The start.
    try:
        started = await obs_tool_client.multiple_deploy(
            jwt_token=jwt_token,
            service_config_code=sc_code,
            item_ids=[primary["id"]] if settings_item is not None and primary.get("id") is not None else [],
            include_gateway=gateway_item is not None,
        )
    except Exception as e:
        logger.exception("devlift_mcp %s: multiple-deploy failed", tool_name)
        return _obs_tool_api_error("start the deployment", e, display_name)
    workflow_id = (started or {}).get("workflow_id")

    # 3. Remember it where get_deployment_status looks.
    try:
        existing_entry = None
        for candidate in await get_all_drafts(user_code, _SERVICE_DRAFT_KEY):
            if candidate.get("transaction_code") == sc_code and candidate.get("status") != "discarded":
                existing_entry = candidate
                break
        deploy_fields = {
            "status": "committed",
            "deploy_in_progress": True,
            "deploy_mode": "temporal",
            "workflow_id": workflow_id,
            "queue_code": primary.get("code") or "",
            "queue_status": "deploying",
            "gateway_queue_code": (gateway_item or {}).get("code"),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if existing_entry:
            await update_draft_by_id(user_code, _SERVICE_DRAFT_KEY, existing_entry["draft_id"], {**existing_entry, **deploy_fields})
        else:
            entry = {
                **_build_draft_entry(
                    draft_id=generate_draft_id(),
                    project_id=project_id or "",
                    ticket_code=ticket_code or "",
                    queue_code=primary.get("code") or "",
                    queue_id=primary.get("id"),
                    transaction_code=sc_code,
                    transaction_table="service_config",
                    identifier=real_name,
                    resource_type="eks_service",
                    deployment_environment=str(environment or ""),
                    status="committed",
                ),
                **deploy_fields,
            }
            await add_draft(user_code, _SERVICE_DRAFT_KEY, entry)
    except Exception:
        logger.exception("devlift_mcp %s: failed to record deploy in Redis (non-fatal)", tool_name)

    parts = " and ".join(
        (["the configuration"] if changes else [])
        + (["the gateway routes"] if gateway_item is not None else [])
    ) or "the request"
    message = f"Deployment of **{display_name}** to {environment} started: {parts}."
    if production:
        message += " A promotion PR to main will be raised, planned and applied."
    return {
        **base,
        "status": "success",
        "action": "deploy",
        "workflow_id": workflow_id,
        "message": message,
        "next_action": {
            "type": "poll_deployment",
            "instruction": "Surface `message`. " + _SERVICE_DEPLOY_POLL_NEXT_ACTION,
        },
    }


_LANE_STATUS_PRIORITY = {"draft": 0, "submit": 1, "approved": 2}


async def _resolve_service_config_code(
    *,
    user_code: str,
    tenant_code: str,
    jwt_token: str,
    service_config_code: Optional[str],
    queue_code: Optional[str],
    ticket_code: Optional[str],
    service_name: Optional[str],
) -> tuple[Optional[str], Optional[dict]]:
    """Turn any handle into the service_configs code (`sc-…`).

    A configuration exists independently of any change request, so a read
    must not depend on a draft being around: Redis (this conversation's
    handle) → the database by service name → the approvals listing (covers
    requests on services the caller may review but did not create).
    """
    if service_config_code:
        return service_config_code, None

    entry = await _find_redis_service_entry(
        user_code, ticket_code=ticket_code, queue_code=queue_code, service_name=service_name
    )
    if entry and entry.get("transaction_code"):
        return entry["transaction_code"], None

    if service_name:
        # The database: the service row by name, then its configurations.
        # Read-only; the permission check happens on the by-code call after.
        try:
            async with AsyncSessionLocal() as db:
                service_row = await ServicesMstRepository(db).find_by_name_for_mcp(
                    tenant_code=tenant_code, service_name=service_name
                )
                configs = []
                if service_row is not None:
                    from app.db.models.service_config_model import ServiceConfigModel

                    stmt = (
                        select(ServiceConfigModel)
                        .where(
                            ServiceConfigModel.tenant_mst_code == tenant_code,
                            ServiceConfigModel.services_mst_code == service_row.code,
                            ServiceConfigModel.is_deleted == False,  # noqa: E712
                        )
                        .order_by(ServiceConfigModel.created_at.desc())
                    )
                    configs = list((await db.execute(stmt)).scalars().all())
        except Exception:
            logger.exception("devlift_mcp: DB lookup of service '%s' failed (non-fatal)", service_name)
            configs = []
        if len(configs) == 1:
            return configs[0].code, None
        if len(configs) > 1:
            return None, {
                "status": "error",
                "reason": "ambiguous_target",
                "message": f"'{service_name}' is configured in {len(configs)} places. Which one?",
                "next_action": {
                    "type": "choose",
                    "options": [
                        {
                            "label": " · ".join(
                                p for p in (
                                    service_name,
                                    getattr(getattr(c, "environment", None), "value", None) or str(getattr(c, "environment", "") or ""),
                                    getattr(c, "geo_loc_mst_code", "") or "",
                                ) if p
                            ),
                            "value": c.code,
                        }
                        for c in configs
                    ],
                    "instruction": (
                        "Present the options and call this tool again with the "
                        "chosen value as service_config_code."
                    ),
                },
            }

        # The approvals listing: requests on services the caller may review.
        from app.mcp_servers.devlift_mcp import obs_tool_client

        try:
            listing = await obs_tool_client.list_approvals(jwt_token=jwt_token)
        except Exception as e:
            logger.exception("devlift_mcp get_service_configuration: list_approvals failed")
            return None, _obs_tool_api_error("look up the service", e, service_name)
        matches = {}
        for item in listing.get("approvals") or []:
            if _name_matches(item.get("display_name"), service_name) or _name_matches(
                item.get("resource_code"), service_name
            ):
                code = item.get("resource_code")
                if code and code not in matches:
                    matches[code] = item
        if len(matches) == 1:
            return next(iter(matches)), None
        if len(matches) > 1:
            return None, {
                "status": "error",
                "reason": "ambiguous_target",
                "message": (
                    f"'{service_name}' is configured in {len(matches)} places. Which one?"
                ),
                "next_action": {
                    "type": "choose",
                    "options": [
                        {"label": _service_request_label(item), "value": code}
                        for code, item in matches.items()
                    ],
                    "instruction": (
                        "Present the options and call this tool again with the "
                        "chosen value as service_config_code."
                    ),
                },
            }
        return None, {
            "status": "error",
            "reason": "not_found",
            "message": (
                f"I couldn't find a service configuration for '{service_name}' that you "
                f"can view. Check the name, or open it in DevLift."
            ),
        }

    return None, {
        "status": "error",
        "reason": "missing_target",
        "message": "Which service? Tell me the service name.",
    }


def _pick_pending_request(items: list) -> Optional[dict]:
    """The request that matters: the one in the review lane, else nothing."""
    lane = [i for i in items if (i.get("status") or "") in _LANE_STATUS_PRIORITY]
    if not lane:
        return None
    lane.sort(
        key=lambda i: (
            _LANE_STATUS_PRIORITY.get(i.get("status"), 9),
            str(i.get("requested_at") or ""),
        ),
        reverse=True,
    )
    return lane[0]


async def get_service_configuration_handler(
    *,
    service_config_code: Optional[str] = None,
    queue_code: Optional[str] = None,
    ticket_code: Optional[str] = None,
    service_name: Optional[str] = None,
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    sc_code, err = await _resolve_service_config_code(
        user_code=user_code,
        tenant_code=auth_ctx.tenant_code,
        jwt_token=jwt_token,
        service_config_code=service_config_code,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
    )
    if err:
        return err

    label = service_name or sc_code
    try:
        live = await obs_tool_client.get_service_config(jwt_token=jwt_token, service_config_code=sc_code)
    except Exception as e:
        logger.exception("devlift_mcp get_service_configuration: by-code failed for %s", sc_code)
        return _obs_tool_api_error("view the service configuration", e, label)

    live_config = {k: v for k, v in (live.get("config") or {}).items() if v is not None}
    display_name = service_name or live.get("name") or sc_code

    pending: Optional[dict] = None
    gateway_pending: Optional[dict] = None
    try:
        listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=sc_code)
        item, gateway_item = _lane_rows(listing.get("approvals") or [])
        if item:
            you = item.get("you") or {}
            changes = item.get("changes") or {}
            pending = {
                "queue_code": item.get("code"),
                "status": item.get("status"),
                "requested_by": item.get("requested_by_name") or item.get("requested_by"),
                "requested_at": item.get("requested_at"),
                "change_count": len(changes),
                "changes": changes,
                "you": {
                    k: you.get(k) for k in ("mine", "can_approve", "can_deploy", "can_write_settings")
                },
            }
        if gateway_item:
            from app.mcp_servers.devlift_mcp import service_payloads as sp

            groups = (gateway_item.get("changes") or {}).get("groups") or []
            gateway_pending = {
                "queue_code": gateway_item.get("code"),
                "status": gateway_item.get("status"),
                "requested_by": gateway_item.get("requested_by_name") or gateway_item.get("requested_by"),
                "requested_at": gateway_item.get("requested_at"),
                "change_count": sp.count_gateway_changes(groups),
                "changes": sp.summarize_gateway_groups(groups),
            }
    except Exception:
        logger.exception("devlift_mcp get_service_configuration: approvals lookup failed (non-fatal)")

    environment = live.get("environment")
    geo = live.get("geo_loc_mst_code")
    cluster_name = live_config.get("cluster_name") or live.get("infrastructure_mst_code")
    where = " / ".join(p for p in (environment, geo) if p)
    on = f" on **{cluster_name}**" if cluster_name else ""
    lead = pending or gateway_pending
    if lead:
        who = lead["requested_by"] or "someone"
        status_word = {"submit": "submitted", "draft": "draft", "approved": "approved"}.get(
            lead["status"], lead["status"]
        )
        parts = []
        if pending:
            parts.append(f"{pending['change_count']} configuration change(s)")
        if gateway_pending:
            parts.append(f"{gateway_pending['change_count']} gateway route change(s)")
        message = (
            f"**{display_name}** in {where}{on}. {' and '.join(parts)} pending in a "
            f"{status_word} request by {who}."
        )
    else:
        message = f"**{display_name}** in {where}{on}. No pending changes."

    return {
        "status": "success",
        "service_name": display_name,
        "service_config_code": sc_code,
        "environment": environment,
        "geo_loc_mst_code": geo,
        "cluster_name": cluster_name,
        "sync_status": live.get("sync_status"),
        "live_config": live_config,
        "pending_request": pending,
        "gateway_request": gateway_pending,
        "message": message,
        "next_action": {
            "type": "present_configuration",
            "instruction": (
                "Surface `message`. Show `live_config` as a compact key/value "
                "list (repository, branches, cpu/memory, port, health, service "
                "path, replicas or HPA, compute, flags). If `pending_request` is "
                "present, render its `changes` as a markdown table titled "
                "'Changes' with columns Field | Deployed → Requested, one row per "
                "key in order, `from` shown as '–' when null, values EXACTLY as "
                "given (DevLift's diff engine produced them; do not convert units "
                "or add caveats), plus the request status and author. If "
                "`gateway_request` is present, render its `changes` as a table "
                "titled 'Gateway routes' with columns Method | Auth | Tag | Change: "
                "one row per `added` ('+ path'), `removed` ('− path'), `changed` "
                "('old → new'), and one for `plugins` / `regex_priority` moves when "
                "present. Do not call any other tool."
            ),
        },
    }


# ============================================================
# Edit a service — seed a chat session with its current configuration
# ============================================================
# "Edit X" must not re-ask 38 questions. This starts a NEW chatbot session on
# the EKS form pre-filled with the service's effective configuration (the
# pending request's values when one exists, else the live row), trusted
# as-is. The user then only says what changes; at isReady the ordinary
# create_service_and_save_draft path finds the existing service and config
# and saves the draft, whose diff shows just the changed fields.

_EKS_FORM_ID = "eks_service_form"
_KONG_FORM_ID = "kong_route_form"
_EDIT_SECTIONS = ("configuration", "gateway")


async def _load_service_as_form_answers(
    *, jwt_token: str, sc_code: str, label: str, tool_name: str
) -> tuple[Optional[dict], Optional[dict]]:
    """Read a service's effective configuration (pending request over the live
    row, permission-checked by the by-code route) and map it to EKS form
    answers. Shared by edit (same service) and clone (new service).

    Returns ({live, pending_item, service_name, prefill}, None) or (None, error).
    """
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp

    try:
        live = await obs_tool_client.get_service_config(jwt_token=jwt_token, service_config_code=sc_code)
    except Exception as e:
        logger.exception("devlift_mcp %s: by-code failed for %s", tool_name, sc_code)
        return None, _obs_tool_api_error("read the service configuration", e, label)

    # The pending request's values are what the author is editing; overlay them.
    effective_config = dict(live.get("config") or {})
    pending_item: Optional[dict] = None
    try:
        listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=sc_code)
        pending_item, _ = _lane_rows(listing.get("approvals") or [])
        if pending_item:
            snapshot = pending_item.get("config_snapshot") or {}
            for k, v in (snapshot.get("config") or {}).items():
                if v is not None:
                    effective_config[k] = v
    except Exception:
        logger.exception("devlift_mcp %s: approvals lookup failed (non-fatal)", tool_name)

    language_ref_code = (
        (pending_item or {}).get("config_snapshot", {}).get("language_ref_code")
        or live.get("language_ref_code")
    )

    # Labels for the lookup fields, the way the user would have answered them.
    async with AsyncSessionLocal() as db:
        svc = (await db.execute(
            text("""select s.name, s.service_type::text, a.name as product_name, rg.name as rg_name,
                           s.applications_mst_code, s.resource_group_mst_code
                    from services_mst s
                    left join applications_mst a on a.code = s.applications_mst_code
                    left join resource_group_mst rg on rg.code = s.resource_group_mst_code
                    where s.code = :c"""),
            {"c": live.get("services_mst_code")},
        )).fetchone()
        geo = (await db.execute(
            text("select name from geo_loc_mst where code = :c"), {"c": live.get("geo_loc_mst_code")}
        )).fetchone()
        lang = None
        if language_ref_code:
            lang = (await db.execute(
                text("select name, version from language_ref where code = :c"), {"c": language_ref_code}
            )).fetchone()

    resolved_name = (svc[0] if svc else None) or live.get("name") or label
    prefill = sp.build_edit_prefill(
        effective_config,
        service_name=resolved_name,
        service_type=(svc[1] if svc else None),
        product_name=(svc[2] if svc else None),
        environment=live.get("environment"),
        geo_name=(geo[0] if geo else None),
        resource_group_name=(svc[3] if svc else None),
        language_name=sp.language_base_name(lang[0] if lang else None, language_ref_code),
        language_version=(lang[1] if lang else None),
    )
    labels = {
        "product_name": svc[2] if svc else None,
        "geo_name": geo[0] if geo else None,
        "service_type": svc[1] if svc else None,
        "resource_group_name": svc[3] if svc else None,
        # Codes, for comparing a proposed placement against this one. The
        # names above are for the user; only the codes can be compared.
        "application_code": svc[4] if svc else None,
        "resource_group_code": svc[5] if svc else None,
    }
    return {
        "live": live, "pending_item": pending_item, "service_name": resolved_name,
        "prefill": prefill, "labels": labels,
    }, None


#: The card actions `kong_route_form` offers, by their option value. A hint
#: from the caller is matched against these and ignored if it is anything
#: else — a wrong guess would silently ask for the wrong path field.
#: The `route_action` values the form knows. Kept in step with
#: `route_action.dropdown_options` in kong_route_form.json — a value missing
#: here is silently dropped by `_clean_route_action`, and the user is asked a
#: question they already answered.
_ROUTE_ACTIONS = ("Add", "Remove", "Rename", "Plugins")


def _clean_route_action(value):
    """The caller's intent hint, or None when it is absent or unrecognised."""
    text = (value or "").strip().lower()
    return next((a for a in _ROUTE_ACTIONS if a.lower() == text), None)


async def start_service_edit_handler(
    *,
    service_name: Optional[str] = None,
    queue_code: Optional[str] = None,
    ticket_code: Optional[str] = None,
    service_config_code: Optional[str] = None,
    section: Optional[str] = None,
    route_action: Optional[str] = None,
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt
    from app.mcp_servers.devlift_mcp.chatbot_client import cache_edit_origin, mark_session_prefilled, post_select_form

    section = (section or "configuration").strip().lower()
    if section not in _EDIT_SECTIONS:
        return {
            "status": "error",
            "reason": "invalid_section",
            "message": f"Unknown section '{section}'. Use 'configuration' or 'gateway'.",
        }

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    sc_code, err = await _resolve_service_config_code(
        user_code=user_code,
        tenant_code=tenant_code,
        jwt_token=jwt_token,
        service_config_code=service_config_code,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
    )
    if err:
        return err

    loaded, err = await _load_service_as_form_answers(
        jwt_token=jwt_token, sc_code=sc_code, label=service_name or sc_code, tool_name="edit_service"
    )
    if err:
        return err
    live, pending_item, resolved_name, prefill, labels = (
        loaded["live"], loaded["pending_item"], loaded["service_name"], loaded["prefill"],
        loaded["labels"],
    )

    if section == "gateway":
        return await _start_gateway_edit(
            auth_ctx=auth_ctx, jwt_token=jwt_token, sc_code=sc_code, live=live,
            service_name=resolved_name, labels=loaded["labels"], pending_item=pending_item,
            route_action=_clean_route_action(route_action),
        )

    new_ticket = f"mcp-{user_code}-{secrets.token_hex(4)}"
    try:
        chat_response = await post_select_form(
            form_id=_EKS_FORM_ID,
            ticket_code=new_ticket,
            tenant_code=tenant_code,
            user_mst_code=user_code,
            jwt_token=jwt_token,
            prefill=prefill,
        )
    except Exception as e:
        logger.exception("devlift_mcp edit_service: chatbot /select-service failed")
        return {"status": "error", "message": f"Failed to start the edit session: {str(e).strip() or type(e).__name__}"}

    # Pin the placement this session is editing. The save handler resolves its
    # target row from the ANSWERS, so without this an answer of "prod" simply
    # resolves to a different row and the edit lands there — or creates it.
    await cache_edit_origin(
        user_code,
        new_ticket,
        {
            "service_config_code": sc_code,
            "service_name": resolved_name,
            "environment": live.get("environment"),
            "geo_loc_mst_code": live.get("geo_loc_mst_code"),
            "infrastructure_mst_code": live.get("infrastructure_mst_code"),
            "product_name": labels.get("product_name"),
            "geo_name": labels.get("geo_name"),
            "resource_group_name": labels.get("resource_group_name"),
            "application_code": labels.get("application_code"),
            "resource_group_code": labels.get("resource_group_code"),
            "service_type": labels.get("service_type"),
        },
    )

    # The language template bootstraps a NEW service. This session opens with
    # the live values already in place, so claim the slot before the first
    # chat turn — otherwise the language is known immediately and the offer
    # fires, proposing defaults over a running configuration.
    await mark_session_prefilled(user_code, new_ticket, "edit")

    missing = ((chat_response.get("missing_fields") or {}).get("required")) or []
    return {
        "status": "success",
        "ticket_code": new_ticket,
        "service_name": resolved_name,
        "service_config_code": sc_code,
        "environment": live.get("environment"),
        "geo_loc_mst_code": live.get("geo_loc_mst_code"),
        "editing_request": (
            {"queue_code": pending_item.get("code"), "status": pending_item.get("status")} if pending_item else None
        ),
        "prefilled_fields": chat_response.get("prefilled_fields") or sorted(prefill),
        "missing_required": missing,
        "message": chat_response.get("message") or f"Loaded the current settings of **{resolved_name}**. Tell me what to change.",
        "next_action": {
            "type": "continue_chat",
            "ticket_code": new_ticket,
            "instruction": (
                f"Surface `message`. Ask the user what they want to change (if they "
                f"already said, e.g. 'set cpu to 2', send that now). Send each reply "
                f"to chat(message=<reply>, ticket_code='{new_ticket}'). Do NOT replay "
                f"or re-enter the existing values — they are already loaded. When the "
                f"chat responds with next_action.type == 'create_service_and_save_draft', "
                f"call that tool with the same ticket_code; it will save the changes as a "
                f"draft against the existing service."
            ),
        },
    }


async def _start_gateway_edit(
    *, auth_ctx, jwt_token: str, sc_code: str, live: dict, service_name: str,
    labels: dict, pending_item: Optional[dict], route_action: Optional[str] = None,
) -> dict:
    """edit_service_configuration(section='gateway'): open `kong_route_form`
    with the placement and the service already answered, so the user is asked
    only for the card — method, auth, paths (tag / priority / plugins optional).
    Same ticket → chat → create_service_and_save_draft as everything else."""
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp.chatbot_client import post_select_form

    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code
    environment = live.get("environment")
    geo = live.get("geo_loc_mst_code")

    # What the gateway holds today (non-fatal: the form works without it).
    existing_routes: list[dict] = []
    try:
        state = await obs_tool_client.get_gateway_state(jwt_token=jwt_token, service_config_code=sc_code)
        existing_routes = sp.summarize_gateway_state(state)
    except Exception:
        logger.exception("devlift_mcp edit_service(gateway): gateway state failed (non-fatal)")

    prefill = sp.build_gateway_prefill(
        service_name=service_name,
        product_name=labels.get("product_name"),
        environment=environment,
        geo_name=labels.get("geo_name"),
        route_action=route_action,
    )
    new_ticket = f"mcp-{user_code}-{secrets.token_hex(4)}"
    try:
        chat_response = await post_select_form(
            form_id=_KONG_FORM_ID,
            ticket_code=new_ticket,
            tenant_code=tenant_code,
            user_mst_code=user_code,
            jwt_token=jwt_token,
            prefill=prefill,
            # Only the placement is prefilled here; the card itself is what the
            # user came to fill in. Without this the form skips tag, priority
            # and plugins before they are ever shown and reports ready as soon
            # as the path lands.
            ask_optional=True,
        )
    except Exception as e:
        logger.exception("devlift_mcp edit_service(gateway): chatbot /select-service failed")
        return {"status": "error", "message": f"Failed to start the gateway session: {str(e).strip() or type(e).__name__}"}

    missing = ((chat_response.get("missing_fields") or {}).get("required")) or []
    path_count = sum(len(c["paths"]) for c in existing_routes)
    live_cards = existing_routes
    # Counted off the full cards above; the payload carries the trimmed ones.
    existing_routes = sp.preview_gateway_cards(existing_routes)
    where = " / ".join(p for p in (environment, geo) if p)
    if existing_routes:
        message = (
            f"Gateway of **{service_name}** in {where}: {len(existing_routes)} route "
            f"group(s), {path_count} path(s). Pick the card to add to."
        )
    else:
        message = f"**{service_name}** in {where} has no gateway routes yet. Pick the card to add to."

    # The web's Gateway tab: the cards first, then "Add route" on one card, then
    # the path. Here: the current cards as a table, ONE dialog for method + auth
    # (the form's first section), then one question for the path(s).
    cards_preface = (
        "Surface `message`. When `existing_routes` is non-empty, first render it as "
        "a table Method | Auth | Tag | Paths (Auth = 'JWT' when `secured` else "
        "'public'; paths joined with ', '). A card carries at most "
        f"{sp.GATEWAY_PATH_PREVIEW} paths: when its `paths_more` is above 0, end "
        "that cell with '+N more' (N = `paths_more`) rather than listing them — "
        "`paths_total` is how many the group really holds. Do not offer to page "
        "through the rest; the table is there to name the cards, and the user "
        "can open the Gateway tab for the full list. "
    )
    path_rule = (
        " PATHS, when the chat asks for them next: ask for the Kong regex path and "
        "SHOW the shape in the question — 'Which path? Kong form, e.g. "
        "'~/api/v1/users$'' — because that is exactly what the Gateway tab takes "
        "and exactly what gets stored. Send what the user typed, unchanged. Never "
        "convert a plain path yourself: if they give '/api/v1/users' the form "
        "rejects it and the rejection comes back with devlift's own suggestion for "
        "them to confirm. Several paths may be given at once, comma separated. "
        "Tag, priority and plugins ARE asked after the path, each with a Skip "
        "choice; let the user answer them rather than skipping on their behalf. "
        "When chat returns next_action.type == 'create_service_and_save_draft', "
        "call it with this ticket_code."
    )
    # A Remove, a Rename or a Plugins card acts on a group that already
    # EXISTS, and this is the turn its intent is known — so the same question
    # `chat` would build is built here. Without it the first turn of an edit
    # session bypassed all of it: the model was handed the form's own section
    # and asked for the paths in prose, off a gateway table it had printed
    # itself. `chat` enriches every LATER turn, which is what made this look
    # like a reload problem rather than a missing call.
    from app.mcp_servers.devlift_mcp.tools.chat import (
        _LIVE_GROUP_INTENTS,
        _card_group_is_live,
        _group_pick_action,
        _named_paths,
        _plugin_card_action,
    )

    section = chat_response.get("section") or {}
    gw = chat_response.get("gateway_group") or {}
    picked = None
    if route_action in _LIVE_GROUP_INTENTS and live_cards and not _card_group_is_live(gw, live_cards):
        want_tag = str(gw.get("route_group_key") or "")
        if route_action == "Plugins":
            picked = _plugin_card_action(live_cards, want_tag)
        else:
            picked = _group_pick_action(
                live_cards, want_tag, route_action,
                tuple(_named_paths(gw, route_action)), str(gw.get("http_method") or ""),
            )

    if picked:
        # No table and no `path_rule`: the picker IS the table, and the rule is
        # about adding a path, which this card is not doing.
        picked["ticket_code"] = new_ticket
        next_action = picked
    elif len(section.get("fields") or []) >= 2:
        next_action = _section_action(section, preface=cards_preface)
        next_action["ticket_code"] = new_ticket
        next_action["instruction"] += path_rule
    else:
        asking = missing[0] if missing else None
        next_action = {
            "type": "continue_chat",
            "ticket_code": new_ticket,
            "instruction": (
                cards_preface
                + (
                    f"The form is asking for ONE field: `{asking}`. Ask exactly "
                    f"that, as ONE AskUserQuestion, and nothing else — not the "
                    f"method, not the auth, not a path, unless `{asking}` IS "
                    f"one of them. "
                    if asking else
                    "Ask the single field the form is waiting on, as ONE "
                    "AskUserQuestion, and nothing else. "
                )
                + f"Then send the answer to chat(ticket_code='{new_ticket}', "
                f"answers={{'{asking or '<field>'}': <value>}})."
                + path_rule
            ),
        }
    return {
        "status": "success",
        "section": "gateway",
        "ticket_code": new_ticket,
        "service_name": service_name,
        "service_config_code": sc_code,
        "environment": environment,
        "geo_loc_mst_code": geo,
        "editing_request": (
            {"queue_code": pending_item.get("code"), "status": pending_item.get("status")} if pending_item else None
        ),
        "existing_routes": existing_routes,
        "prefilled_fields": chat_response.get("prefilled_fields") or sorted(prefill),
        "missing_required": missing,
        "message": message,
        "next_action": next_action,
    }


# ============================================================
# Clone a service's configuration into a NEW service
# ============================================================
# The web's "clone service": a new service whose first configuration copies
# another service's settings. Here it is the edit machinery with the name
# swapped: the source is loaded as form answers, the name (and optionally the
# environment / geo) replaced, and a pre-filled session opened. "done" — or
# any differences — reaches isReady, and create_service_and_save_draft then
# creates the new service, its base config and a draft holding the copied
# values. Settings only: variables/secrets and sidecars are not copied.

_SERVICE_NAME_RE = r"^[a-z][a-z0-9-]{1,254}$"


async def start_service_clone_handler(
    *,
    source_service_name: Optional[str] = None,
    source_service_config_code: Optional[str] = None,
    new_service_name: Optional[str] = None,
    environment: Optional[str] = None,
    geo_location: Optional[str] = None,
) -> dict:
    import re

    from app.mcp_servers.devlift_mcp import service_payloads as sp
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt
    from app.mcp_servers.devlift_mcp.chatbot_client import mark_session_prefilled, post_select_form

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    new_name = (new_service_name or "").strip().lower()
    if not new_name:
        return {
            "status": "error",
            "reason": "missing_target",
            "message": "What should the new service be called? Lowercase letters, numbers and hyphens.",
        }
    if not re.match(_SERVICE_NAME_RE, new_name):
        return {
            "status": "error",
            "reason": "invalid_name",
            "message": (
                f"'{new_service_name}' is not a valid service name. Use lowercase "
                f"letters, numbers and hyphens, e.g. 'payments-api'."
            ),
        }
    if not (source_service_name or source_service_config_code):
        return {
            "status": "error",
            "reason": "missing_target",
            "message": "Which service should it be copied from?",
        }

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {"status": "error", "message": "MCP_INTERNAL_JWT_SECRET is not configured."}

    # The target must not exist yet — an existing name would turn this into an
    # edit of that service, which is not what "clone" means.
    async with AsyncSessionLocal() as db:
        existing = await ServicesMstRepository(db).find_by_name_for_mcp(
            tenant_code=tenant_code, service_name=new_name
        )
    if existing is not None:
        return {
            "status": "error",
            "reason": "already_exists",
            "message": (
                f"A service called '{existing.name}' already exists. Pick another name, "
                f"or use edit_service_configuration to change that one."
            ),
        }

    sc_code, err = await _resolve_service_config_code(
        user_code=user_code,
        tenant_code=tenant_code,
        jwt_token=jwt_token,
        service_config_code=source_service_config_code,
        queue_code=None,
        ticket_code=None,
        service_name=source_service_name,
    )
    if err:
        return err

    loaded, err = await _load_service_as_form_answers(
        jwt_token=jwt_token, sc_code=sc_code, label=source_service_name or sc_code, tool_name="clone_service"
    )
    if err:
        return err
    source_name = loaded["service_name"]
    prefill = dict(loaded["prefill"])
    prefill["service_name"] = new_name

    # Optional placement overrides, given as the form's labels.
    overrides: dict = {}
    if environment:
        env_label = sp._ENVIRONMENT_LABELS.get(environment.strip().lower(), environment.strip().capitalize())
        prefill["environment"] = env_label
        overrides["environment"] = env_label
    if geo_location:
        geo_label = geo_location.strip()
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text("select name from geo_loc_mst where code = :c or lower(name) = lower(:c)"),
                {"c": geo_label},
            )).fetchone()
        if row:
            geo_label = row[0]
        prefill["geo_location"] = geo_label
        overrides["geo_location"] = geo_label

    new_ticket = f"mcp-{user_code}-{secrets.token_hex(4)}"
    try:
        chat_response = await post_select_form(
            form_id=_EKS_FORM_ID,
            ticket_code=new_ticket,
            tenant_code=tenant_code,
            user_mst_code=user_code,
            jwt_token=jwt_token,
            prefill=prefill,
        )
    except Exception as e:
        logger.exception("devlift_mcp clone_service: chatbot /select-service failed")
        return {"status": "error", "message": f"Failed to start the clone session: {str(e).strip() or type(e).__name__}"}

    # A clone exists to carry the source service's settings across. Offering
    # the language's defaults here would offer to discard the very thing the
    # user asked to copy, so claim the slot the same way an edit does.
    await mark_session_prefilled(user_code, new_ticket, "clone")

    missing = ((chat_response.get("missing_fields") or {}).get("required")) or []
    where = " / ".join(p for p in (prefill.get("environment"), prefill.get("geo_location")) if p)
    return {
        "status": "success",
        "ticket_code": new_ticket,
        "source_service_name": source_name,
        "source_service_config_code": sc_code,
        "new_service_name": new_name,
        "placement": where,
        "overrides": overrides,
        "prefilled_fields": chat_response.get("prefilled_fields") or sorted(prefill),
        "missing_required": missing,
        "message": (
            f"Loaded the settings of **{source_name}** for a new service **{new_name}** "
            f"in {where}. Say **done** to create it as an exact copy, or tell me what "
            f"should differ first."
        ),
        "next_action": {
            "type": "continue_chat",
            "ticket_code": new_ticket,
            "instruction": (
                f"Surface `message`. Ask whether anything should differ from the source; "
                f"send the user's answer — or 'done' — to chat(message=..., "
                f"ticket_code='{new_ticket}'). Do NOT re-enter the copied values. When "
                f"chat responds with next_action.type == 'create_service_and_save_draft', "
                f"call it with the same ticket_code: it creates the new service, its "
                f"base configuration and a draft holding the copied settings."
            ),
        },
    }


# ============================================================
# Service settings — every configuration field and its value (read-only)
# ============================================================
# The Settings tab, as text: the fields grouped the way the web groups them
# (Repository / Dockerfile / Manifest / AWS Resource Provisioning), each with
# the value the tab would show. The tab shows the pending request's values
# when one exists (that is what the author is editing) and the live values
# otherwise; each field says which it is.

_SETTINGS_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Placement", (
        "environment", "geo_loc_mst_code", "cluster_name", "namespace", "alb_selection",
    )),
    ("Repository", (
        "repository", "branches", "build_path", "other_paths",
    )),
    ("Dockerfile", (
        "language_name", "language_version", "language_ref_code",
        "generate_dockerfile", "dockerfile_path", "build_args",
        "xms", "xmx", "go_config_path", "go_use_aws_secrets",
    )),
    ("Manifest", (
        "cpu_requested", "cpu_limit", "memory_requested", "memory_limit",
        "port", "health", "service_path", "alb_schema", "compute",
        "replica_count", "hpa", "ebs_enabled", "secrets_enabled",
    )),
    ("AWS Resource Provisioning", (
        "custom_iam_policies", "create_ecr", "create_secrets", "create_ssm",
        "create_argo", "auth_mode",
    )),
]

# Root-level snapshot / row keys that belong to a section but are not inside
# the `config` block.
_SETTINGS_ROOT_KEYS = ("environment", "geo_loc_mst_code", "language_name", "language_version", "language_ref_code")


def _is_set(value) -> bool:
    return value not in (None, "", [], {})


def _build_settings_sections(live_row: dict, pending_item: Optional[dict]) -> list[dict]:
    """Group fields the way the Settings tab does, with the value it would show."""
    live_config = live_row.get("config") or {}
    live_root = {
        "environment": live_row.get("environment"),
        "geo_loc_mst_code": live_row.get("geo_loc_mst_code"),
        "language_ref_code": live_row.get("language_ref_code"),
    }
    draft_config: dict = {}
    draft_root: dict = {}
    if pending_item:
        snapshot = pending_item.get("config_snapshot") or {}
        draft_config = snapshot.get("config") or {}
        draft_root = {k: snapshot.get(k) for k in _SETTINGS_ROOT_KEYS}

    sections: list[dict] = []
    for title, keys in _SETTINGS_SECTIONS:
        fields = []
        for key in keys:
            if key in _SETTINGS_ROOT_KEYS:
                draft_val, live_val = draft_root.get(key), live_root.get(key)
            else:
                draft_val, live_val = draft_config.get(key), live_config.get(key)
            if _is_set(draft_val):
                fields.append({"field": key, "value": draft_val, "source": "draft"})
            elif _is_set(live_val):
                fields.append({"field": key, "value": live_val, "source": "live"})
            elif isinstance(live_val, bool) or isinstance(draft_val, bool):
                fields.append({"field": key, "value": draft_val if draft_val is not None else live_val, "source": "draft" if draft_val is not None else "live"})
        if fields:
            sections.append({"title": title, "fields": fields})
    return sections


async def view_service_settings_handler(
    *,
    service_config_code: Optional[str] = None,
    queue_code: Optional[str] = None,
    ticket_code: Optional[str] = None,
    service_name: Optional[str] = None,
    include_gateway: bool = False,
) -> dict:
    from app.mcp_servers.devlift_mcp import obs_tool_client
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    sc_code, err = await _resolve_service_config_code(
        user_code=user_code,
        tenant_code=auth_ctx.tenant_code,
        jwt_token=jwt_token,
        service_config_code=service_config_code,
        queue_code=queue_code,
        ticket_code=ticket_code,
        service_name=service_name,
    )
    if err:
        return err

    label = service_name or sc_code
    try:
        live = await obs_tool_client.get_service_config(jwt_token=jwt_token, service_config_code=sc_code)
    except Exception as e:
        logger.exception("devlift_mcp view_service_settings: by-code failed for %s", sc_code)
        return _obs_tool_api_error("view the service settings", e, label)

    pending_item: Optional[dict] = None
    try:
        listing = await obs_tool_client.list_approvals(jwt_token=jwt_token, resource_code=sc_code)
        pending_item, _ = _lane_rows(listing.get("approvals") or [])
    except Exception:
        logger.exception("devlift_mcp view_service_settings: approvals lookup failed (non-fatal)")

    # The Gateway tab, as cards (non-fatal: a user without gateway view
    # permission still sees the settings).
    gateway_routes: list[dict] = []
    try:
        from app.mcp_servers.devlift_mcp import service_payloads as sp

        state = await obs_tool_client.get_gateway_state(jwt_token=jwt_token, service_config_code=sc_code)
        gateway_routes = sp.preview_gateway_cards(sp.summarize_gateway_state(state))
    except Exception:
        logger.exception("devlift_mcp view_service_settings: gateway state failed (non-fatal)")

    # Routes are fetched either way — the count is worth one line even when the
    # table is not — but only HANDED OVER when they were asked for. A service's
    # routes run to dozens of regex paths, and printing them under "show me the
    # config of X" buried the settings the user actually asked about.
    gateway_route_count = len(gateway_routes)
    if not include_gateway:
        gateway_routes = []

    sections = _build_settings_sections(live, pending_item)
    display_name = service_name or live.get("name") or sc_code
    live_config = live.get("config") or {}
    cluster_name = live_config.get("cluster_name") or live.get("infrastructure_mst_code")
    environment = live.get("environment")
    geo = live.get("geo_loc_mst_code")

    pending: Optional[dict] = None
    if pending_item:
        pending = {
            "queue_code": pending_item.get("code"),
            "status": pending_item.get("status"),
            "requested_by": pending_item.get("requested_by_name") or pending_item.get("requested_by"),
        }
    field_count = sum(len(s["fields"]) for s in sections)
    draft_count = sum(1 for s in sections for f in s["fields"] if f["source"] == "draft")
    where = " / ".join(p for p in (environment, geo) if p)
    if pending:
        message = (
            f"Settings of **{display_name}** in {where}: {field_count} fields, "
            f"{draft_count} of them from the {pending['status']} request by "
            f"{pending['requested_by'] or 'someone'} (not yet deployed)."
        )
    else:
        message = f"Settings of **{display_name}** in {where}: {field_count} fields, all live."

    return {
        "status": "success",
        "service_name": display_name,
        "service_config_code": sc_code,
        "environment": environment,
        "geo_loc_mst_code": geo,
        "cluster_name": cluster_name,
        "sync_status": live.get("sync_status"),
        "pending_request": pending,
        "sections": sections,
        "gateway_routes": gateway_routes,
        "gateway_route_count": gateway_route_count,
        "message": message,
        "next_action": {
            "type": "present_settings",
            "instruction": (
                "Surface `message`, then render each entry of `sections` as its "
                "own markdown table titled with the section `title`, columns "
                "Field | Value. Show values EXACTLY as given (lists joined with "
                "', ', booleans as true/false, nested hpa as 'enabled: …, min: …, "
                "max: …'). Append ' (draft)' to a value whose `source` is 'draft'. "
                + (
                    "Then one table titled 'Gateway routes' with columns Method "
                    "| Auth | Tag | Paths | Priority | Plugins — Auth is 'JWT' "
                    "when `secured` else 'public', paths joined with ', ' with "
                    "' (pending)' after a path whose `deployed` is false. "
                    "A card carries at most 10 paths: when its `paths_more` is "
                    "above 0, end that cell with '+N more' (N = `paths_more`) "
                    "instead of listing them, and say the group holds "
                    "`paths_total` in all. "
                    if gateway_routes else
                    (
                        f"Do NOT list the gateway routes: they were not asked "
                        f"for. Close with ONE line offering them — this service "
                        f"has {gateway_route_count} gateway route(s), and they "
                        f"arrive by calling this tool again with "
                        f"include_gateway=true. "
                        if gateway_route_count else ""
                    )
                )
                + "Do not add fields, notes or unit conversions. Do not call any "
                "other tool. Do not offer to open a form and wait for a "
                "go-ahead either ('say the word and I'll…'): this is a read, so "
                "end with the tables. If the user then asks for a change, act on "
                "it in THAT turn"
                # Named only when the service HAS routes: a service with none
                # says nothing about the gateway at all, not even how to edit
                # one that does not exist.
                + (
                    " — routes go to edit_service_configuration("
                    "section='gateway'), which asks the card itself, so never "
                    "collect method / auth / path in prose first."
                    if gateway_route_count else "."
                )
            ),
        },
    }


# ============================================================
# open_variables_editor — the browser hand-off for variables & secrets
# ============================================================

async def _load_service_link_context(service_config_code: str) -> Optional[dict]:
    """Environment, service name, application and workspace codes for the
    canvas deep link. The canvas restores a node only once the matching
    application and environment are selected, so pinning them makes the link
    land on the panel instead of an empty canvas. None when the code is
    unknown; the link then falls back to `resource` + `tab` alone."""
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(
                    ServiceConfigModel.environment,
                    ServicesMstModel.name,
                    ServicesMstModel.applications_mst_code,
                    ApplicationsMstModel.workspace_code,
                )
                .join(ServicesMstModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
                .outerjoin(ApplicationsMstModel, ApplicationsMstModel.code == ServicesMstModel.applications_mst_code)
                .where(
                    ServiceConfigModel.code == service_config_code,
                    ServiceConfigModel.is_deleted.isnot(True),
                )
            )
        ).first()
    if row is None:
        return None
    environment = row[0].value if hasattr(row[0], "value") else row[0]
    return {
        "environment": environment or "",
        "service_name": row[1],
        "app_code": row[2] or "",
        "workspace_code": row[3] or "",
    }


async def _variables_editor_url(tenant_code: str, service_config_code: str) -> Optional[str]:
    """Deep link to the Variables tab, or None when FRONTEND_BASE_URL is unset.
    The context lookup is best-effort: a bare `resource` link still opens the
    panel when the user's default application/environment match."""
    from app.services.frontend_links import tenant_url_slug, variables_tab_link

    ctx: Optional[dict] = None
    try:
        ctx = await _load_service_link_context(service_config_code)
    except Exception:
        logger.exception("devlift_mcp variables link: context lookup failed for %s (non-fatal)", service_config_code)
    ctx = ctx or {}
    return variables_tab_link(
        tenant_slug=await tenant_url_slug(tenant_code),
        resource_code=service_config_code,
        environment=ctx.get("environment") or "",
        app_code=ctx.get("app_code") or "",
        workspace_code=ctx.get("workspace_code") or "",
    )


async def open_variables_editor_handler(
    *,
    service_name: Optional[str] = None,
    ticket_code: Optional[str] = None,
    service_config_code: Optional[str] = None,
) -> dict:
    """Resolve the service and return the dashboard link to its Variables tab.

    Deliberately value-free: variable and secret values are typed in the
    browser and saved straight to the secret service, so neither the model,
    the MCP server nor the chatbot ever holds one.
    """
    from app.mcp_servers.devlift_mcp._internal_jwt import mint_internal_jwt

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err

    jwt_token = mint_internal_jwt(auth_ctx)
    if not jwt_token:
        return {
            "status": "error",
            "message": (
                "The MCP server cannot call DevLift's API: MCP_INTERNAL_JWT_SECRET "
                "is not configured. Ask the platform team."
            ),
        }

    sc_code, err = await _resolve_service_config_code(
        user_code=auth_ctx.user_code,
        tenant_code=auth_ctx.tenant_code,
        jwt_token=jwt_token,
        service_config_code=service_config_code,
        queue_code=None,
        ticket_code=ticket_code,
        service_name=service_name,
    )
    if err:
        if err.get("reason") == "not_found":
            err = {
                **err,
                "message": err.get("message", "") + (
                    " Variables can only be added to a service that exists in DevLift; "
                    "create it first (a saved draft is enough), then ask again."
                ),
            }
        return err

    ctx: Optional[dict] = None
    try:
        ctx = await _load_service_link_context(sc_code)
    except Exception:
        logger.exception("devlift_mcp open_variables_editor: context lookup failed for %s (non-fatal)", sc_code)
    ctx = ctx or {}

    from app.services.frontend_links import tenant_url_slug, variables_tab_link

    url = variables_tab_link(
        tenant_slug=await tenant_url_slug(auth_ctx.tenant_code),
        resource_code=sc_code,
        environment=ctx.get("environment") or "",
        app_code=ctx.get("app_code") or "",
        workspace_code=ctx.get("workspace_code") or "",
    )
    if not url:
        return {
            "status": "error",
            "reason": "not_configured",
            "message": (
                "The dashboard link cannot be built: FRONTEND_BASE_URL is not set on "
                "the MCP server. Ask the platform team; meanwhile the Variables tab "
                "is reachable from the service panel in the DevLift dashboard."
            ),
        }

    display_name = service_name or ctx.get("service_name") or sc_code
    environment = ctx.get("environment") or ""
    where = f" in {environment}" if environment else ""
    return {
        "status": "success",
        "service_name": display_name,
        "service_config_code": sc_code,
        "environment": environment,
        "url": url,
        "message": (
            f"DevLift policy requires variables and secrets to be managed in the "
            f"dashboard.\n\n"
            f"[Open **{display_name}**{where}]({url})\n\n"
            f"Saving there creates a draft. Tell me when you are done and I can "
            f"submit it for review."
        ),
        "next_action": {
            "type": "present_link",
            "instruction": (
                "Show `message` exactly as given. It already carries the link as "
                "markdown, so reproducing it verbatim renders a clickable link — do "
                "not unwrap it, and do not print the raw `url` beside it. "
                "Add nothing to it: no warnings about other requests or the review "
                "lane, no caveats, no explanation of why this is policy, no apology. "
                "Never ask for, accept or repeat a variable or secret value; if the "
                "user offers one, restate the policy in one line and point at the "
                "link. Then wait. When the user says they have saved, offer "
                "submit_service_request for this service; do not submit on your own."
            ),
        },
    }

# ============================================================
# Public entry: start_resource_edit_handler / apply_resource_edit_handler
# ============================================================

async def _describe_placement(db: AsyncSession, row) -> dict:
    """Product / environment / region for one row, in words the user recognises."""
    product = row.applications_mst_code or ""
    if row.applications_mst_code:
        result = await db.execute(
            select(ApplicationsMstModel.name)
            .where(ApplicationsMstModel.code == row.applications_mst_code)
        )
        product = result.scalar_one_or_none() or row.applications_mst_code

    return {
        "product": product,
        "environment": row.environments_enum.value if row.environments_enum else "",
        "region": row.geo_loc_mst_code or "",
    }


async def start_resource_edit_handler(
    *,
    resource_name: str,
    environment: Optional[str] = None,
    product: Optional[str] = None,
) -> dict:
    """Open an edit session for a live S3 bucket or SQS queue. Read-only.

    Reads the row straight out of infrastructure_mst and hands the LLM the
    current values plus the field lists. Nothing is written and nothing deploys
    — apply_resource_edit does that, from the state cached here.

    `environment` and `product` narrow the search when one name exists in more
    than one placement. They are a FILTER, never a change: the placement is read
    from the matched row and cached, and the apply path reads it from there.
    Neither tool has an input that can move a resource.
    """
    from app.mcp_servers.devlift_mcp import resource_edit as redit

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code

    if tenant_code not in redit.SUPPORTED_TENANTS:
        return {
            "status": "error",
            "reason": "unsupported_tenant",
            "message": (
                "Editing a resource is not available for this tenant yet. "
                "Change it in DevLift instead."
            ),
        }

    wanted = (resource_name or "").strip()
    if not wanted:
        return {
            "status": "error",
            "reason": "missing_name",
            "message": "Which resource do you want to edit? Give me its name.",
        }

    async with AsyncSessionLocal() as db:
        infra_repo = InfrastructureMstRepository(db)
        matches = []
        for infra_type in redit.supported_infra_types():
            rows = await infra_repo.list_by_filters(
                tenant_code=tenant_code,
                infrastructuretype_ref_code=infra_type,
            )
            matches.extend(
                row for row in rows
                if (row.locator or {}).get("identifier") == wanted
            )

        if not matches:
            return {
                "status": "error",
                "reason": "not_found",
                "message": (
                    f"No S3 bucket or SQS queue named **{wanted}** in this tenant. "
                    f"Editing other resource types is not supported yet."
                ),
            }

        placements = [await _describe_placement(db, row) for row in matches]

        if environment or product:
            wanted_env = (environment or "").strip().lower()
            wanted_product = (product or "").strip().lower()
            narrowed = [
                (row, place) for row, place in zip(matches, placements)
                if (not wanted_env or place["environment"].lower() == wanted_env)
                and (not wanted_product or place["product"].lower() == wanted_product)
            ]
            if narrowed:
                matches = [r for r, _ in narrowed]
                placements = [p for _, p in narrowed]

        if len(matches) > 1:
            return {
                "status": "needs_choice",
                "reason": "multiple_matches",
                "resource_name": wanted,
                "matches": placements,
                "message": f"There is more than one **{wanted}**. Which one do you mean?",
                "next_action": {
                    "type": "ask_user",
                    "instruction": (
                        "Ask the user which one, listing `matches` by environment "
                        "and product. Then call start_resource_edit again with the "
                        "same resource_name plus environment (and product if two "
                        "share an environment). Do not guess."
                    ),
                },
            }

        row, placement = matches[0], placements[0]
        infra_type = row.infrastructuretype_ref_code
        locator = dict(row.locator or {})

    editable = redit.editable_fields(infra_type)
    locked = redit.locked_fields(infra_type)

    edit_id = f"edit-{secrets.token_hex(6)}"
    await redit.save_edit_state(user_code, edit_id, {
        "infrastructure_mst_code": row.code,
        "infrastructuretype_ref_code": infra_type,
        "identifier": wanted,
        "locator": locator,
        "applications_mst_code": row.applications_mst_code,
        "environment": placement["environment"],
        "geo_loc_mst_code": row.geo_loc_mst_code,
        "product_name": placement["product"],
    })

    return {
        "status": "success",
        "edit_id": edit_id,
        "resource_name": wanted,
        "placement": placement,
        "editable_fields": [
            {**field, "current": locator.get(field["field"])} for field in editable
        ],
        "locked_fields": [
            {"field": f, "label": redit.label_for(f), "current": locator.get(f)}
            for f in locked
        ],
        "message": (
            f"Loaded **{wanted}** in {placement['environment']}. "
            f"Tell me what to change."
        ),
        "next_action": {
            "type": "collect_changes",
            "edit_id": edit_id,
            "instruction": (
                "Show the current values from `editable_fields` as a short list "
                "using each entry's `label`, never its `field`. Ask what the user "
                "wants to change.\n\n"
                "ONLY the fields in `editable_fields` can change. `locked_fields` "
                "cannot: the name, and anything derived from it, decide where the "
                "resource lives, and changing one would leave the existing "
                "resource running and build a second one. Product, environment "
                "and region are fixed for the same reason and are not inputs on "
                "either tool. If the user asks for any of those, say plainly that "
                "it cannot be changed on an existing resource and offer to create "
                "a new one instead.\n\n"
                "When the user has said what they want, call "
                "apply_resource_edit(edit_id=..., changes={field: value, ...}) "
                "with ONLY the fields they actually changed, keyed by `field`. "
                "That call returns a before/after summary and writes nothing "
                "until you call it again with confirmed=true."
            ),
        },
    }


async def apply_resource_edit_handler(
    *,
    edit_id: str,
    changes: dict,
    confirmed: bool = False,
    project_id: Optional[str] = None,
) -> dict:
    """Write an edit opened by start_resource_edit, after the user confirms.

    Placement comes from the cached state, never from the caller, so this
    cannot move a resource. The backend refuses a rename or a placement change
    regardless (infrastructure_update_guard) — this is the layer that keeps the
    LLM from trying.
    """
    from app.mcp_servers.devlift_mcp import resource_edit as redit

    project_id, init_response = _ensure_project(project_id, "apply_resource_edit")
    if init_response is not None:
        return init_response

    auth_ctx, err = await _get_auth_or_error()
    if err:
        return err
    user_code = auth_ctx.user_code
    tenant_code = auth_ctx.tenant_code
    user_email = auth_ctx.user_email

    state = await redit.load_edit_state(user_code, edit_id)
    if not state:
        return {
            "status": "error",
            "reason": "edit_expired",
            "message": (
                "That edit session is no longer open. Call start_resource_edit "
                "again to reload the resource's current values."
            ),
        }

    infra_type = state["infrastructuretype_ref_code"]
    opened_locator = state.get("locator") or {}

    if not changes:
        return {
            "status": "error",
            "reason": "no_changes",
            "message": "Tell me which fields to change.",
        }

    refused = redit.refused_changes(changes, infra_type)
    if any(refused.values()):
        parts = []
        if refused["locked"]:
            names = ", ".join(redit.label_for(f) for f in refused["locked"])
            parts.append(
                f"{names} cannot be changed on an existing resource — it decides "
                f"the resource's real name, so changing it would leave the current "
                f"one running and create a second."
            )
        if refused["placement"]:
            names = ", ".join(refused["placement"])
            parts.append(
                f"A resource cannot move: {names} is fixed once it exists. "
                f"Its product, environment and region decide where it lives, and "
                f"changing one would orphan the existing resource and build a new "
                f"one beside it."
            )
        if refused["unknown"]:
            parts.append(f"Not a field on this resource: {', '.join(refused['unknown'])}.")
        return {
            "status": "error",
            "reason": "field_not_editable",
            "refused": refused,
            "message": " ".join(parts),
            "next_action": {
                "type": "ask_user",
                "instruction": (
                    "Tell the user plainly which change cannot be made and why, "
                    "then offer to create a NEW resource in the place they want "
                    "(provision_resource) instead. Do not retry with the same "
                    "field, and do not look for another tool that might allow it "
                    "— there is none. Apply the remaining changes only if the "
                    "user still wants them."
                ),
            },
        }

    # Read the row again rather than trusting the copy cached when the session
    # opened, up to six hours ago. Building the write on that copy puts every
    # stale value back, so a change someone made in DevLift meanwhile is undone
    # without appearing in the diff or anywhere else.
    async with AsyncSessionLocal() as db:
        row = await InfrastructureMstRepository(db).get_by_code(
            state["infrastructure_mst_code"]
        )
        if row is None or row.tenants_mst_code != tenant_code or row.is_deleted:
            await redit.clear_edit_state(user_code, edit_id)
            return {
                "status": "error",
                "reason": "resource_gone",
                "message": (
                    f"**{state['identifier']}** is no longer available to edit. "
                    f"It may have been deleted since you opened this."
                ),
            }
        current_locator = dict(row.locator or {})

        # The same rule the API enforces: a running deploy is reading this row.
        # Caught here so the user is told before being asked to confirm, rather
        # than by a 400 after they say yes.
        try:
            update_guard.check_not_deploying(row)
        except update_guard.InfrastructureUpdateError as exc:
            return {
                "status": "error",
                "reason": "deploy_in_progress",
                "message": str(exc),
            }

    moved = redit.concurrent_changes(opened_locator, current_locator, changes)
    if moved["collisions"] and not confirmed:
        return {
            "status": "needs_confirmation",
            "reason": "changed_since_opened",
            "edit_id": edit_id,
            "resource_name": state["identifier"],
            "conflicts": moved["collisions"],
            "diff": redit.diff_rows(current_locator, changes),
            "message": (
                f"**{state['identifier']}** changed after you opened this edit. "
                f"Saving now would overwrite that."
            ),
            "next_action": {
                "type": "confirm_with_user",
                "instruction": (
                    "Show each row of `conflicts` as: the value when the edit "
                    "opened, the value now, and what the user wants — using the "
                    "`label`. Say plainly that someone else changed it and that "
                    "continuing replaces their value. Ask whether to continue or "
                    "keep theirs. Only if the user says continue, call "
                    "apply_resource_edit again with confirmed=true. If they want "
                    "to keep the other value, drop that field from `changes` and "
                    "call again without confirmed."
                ),
            },
        }

    # The current values are the base from here on: fields nobody is editing
    # keep whatever they hold now, and the diff shown to the user is measured
    # against what is really there.
    stored_locator = current_locator
    diff = redit.diff_rows(stored_locator, changes)
    if not diff:
        return {
            "status": "no_op",
            "edit_id": edit_id,
            "message": (
                f"**{state['identifier']}** already has those values. Nothing to change."
            ),
        }

    if not confirmed:
        return {
            "status": "needs_confirmation",
            "edit_id": edit_id,
            "resource_name": state["identifier"],
            "placement": {
                "product": state.get("product_name"),
                "environment": state.get("environment"),
                "region": state.get("geo_loc_mst_code"),
            },
            "diff": diff,
            "message": (
                f"About to change **{state['identifier']}** in "
                f"{state.get('environment')}. Confirm?"
            ),
            "next_action": {
                "type": "confirm_with_user",
                "instruction": (
                    "Show `diff` as a short before/after list using each row's "
                    "`label`. Ask the user to confirm. Only if they say yes, call "
                    "apply_resource_edit again with the SAME edit_id and changes "
                    "plus confirmed=true. Never pass confirmed=true on your own."
                ),
            },
        }

    metadata = redit.trigger_metadata(infra_type)
    resource_type = redit.RESOURCE_TYPE_BY_INFRA_TYPE[infra_type]

    # The cached value is the ROW's enum value, not an LLM label, so it is read
    # back directly. _resolve_environment maps user-facing words ("staging") and
    # would reject it.
    try:
        env_enum = EnvironmentEnum(state.get("environment"))
    except ValueError:
        env_enum = _resolve_environment(state.get("environment") or "")
    if env_enum is None:
        return {
            "status": "error",
            "message": (
                f"Cannot read the stored environment for this resource "
                f"({state.get('environment')!r}). Change it in DevLift instead."
            ),
        }

    case_ref_code = metadata["case_ref_code"]
    config = redit.build_update_config(stored_locator, changes)

    async with AsyncSessionLocal() as db:
        try:
            infra_request = InfrastructureCreateRequest(
                code=state["infrastructure_mst_code"],
                infrastructuretype_ref_code=infra_type,
                application_code=state["applications_mst_code"],
                environment=env_enum,
                geo_loc_mst_code=state["geo_loc_mst_code"],
                type_specific_config=config,
                created_by=user_email,
            )
            infra_response = await InfrastructureCreationService(db).create_resource(
                tenant_code=tenant_code,
                user_code=user_code,
                request=infra_request,
                user_email=user_email,
            )

            ticket_service = TicketService(db)
            source_ref_id = f"{user_code}:{case_ref_code}:{state['identifier']}"
            ticket = await ticket_service.get_ticket_by_source_ref(
                source=MCP_SOURCE, source_ref_id=source_ref_id
            )
            if not ticket:
                ticket = await ticket_service.generate_ticket_number(
                    tenants_mst_code=tenant_code,
                    user_mst_code=user_code,
                    name=f"{case_ref_code}: {state['identifier']}",
                    description=f"Edited via DevLift MCP for {state['identifier']}",
                    source=MCP_SOURCE,
                    source_ref_id=source_ref_id,
                )

            queue_item = await TransactionQueueService(db).add_item_to_queue(
                user_code=user_code,
                tenant_code=tenant_code,
                transaction_code=infra_response.code,
                table_name=WorkflowSourceTableEnum.INFRASTRUCTURE,
                config_snapshot={
                    **config,
                    "applications_mst_code": state["applications_mst_code"],
                    "environment": env_enum.value,
                    "geo_loc_mst_code": state["geo_loc_mst_code"],
                    "case_ref_code": case_ref_code,
                    "infrastructuretype_ref_code": infra_type,
                    "infrastructure_mst_code": infra_response.code,
                },
                case_ref_code=case_ref_code,
                ticket_code=ticket.code,
            )
            await db.commit()
        except HTTPException as exc:
            await db.rollback()
            return {
                "status": "error",
                "reason": "rejected",
                "message": str(exc.detail),
            }
        except Exception as exc:
            logger.exception("devlift_mcp resource edit failed")
            try:
                await db.rollback()
            except Exception:
                pass
            return {"status": "error", "message": f"Edit failed: {exc}"}

    draft_id = generate_draft_id()
    await add_draft(
        user_code,
        f"{case_ref_code}:{infra_type}",
        _build_draft_entry(
            draft_id=draft_id,
            project_id=project_id,
            ticket_code=ticket.code,
            queue_code=queue_item.code,
            queue_id=queue_item.id,
            transaction_code=infra_response.code,
            transaction_table=metadata["transaction_table"],
            identifier=state["identifier"],
            resource_type=resource_type,
            deployment_environment=env_enum.value,
            status="pending",
        ),
    )

    await redit.clear_edit_state(user_code, edit_id)

    # Deploy inline rather than handing the LLM a next_action for
    # trigger_resource_deployment: that tool resolves the resource through the
    # metadata registry, which is empty, so it would fail where this cannot.
    # The user has already confirmed the change, so there is nothing left to ask.
    async with AsyncSessionLocal() as db:
        core = await _execute_trigger_core(
            db=db,
            resource_type=resource_type,
            metadata=metadata,
            user_code=user_code,
            tenant_code=tenant_code,
            draft_id=draft_id,
        )
        if not core.get("_ok"):
            core.setdefault("identifier", state["identifier"])
            core["applied"] = diff
            core.setdefault(
                "message",
                f"Saved the change to **{state['identifier']}**, but the deploy "
                f"could not be started.",
            )
            return core

        identifier = state["identifier"]
        if is_paas_tenant(tenant_code):
            response = {
                "status": "success",
                "action": "deployed",
                "resource_type": resource_type,
                "identifier": identifier,
                "draft_id": draft_id,
                "message": (
                    f"'{identifier}' has been submitted for deployment. "
                    f"Your change will be live shortly."
                ),
                "next_action": (
                    "Immediately invoke the `/loop` skill now — do not wait "
                    "for the user. Run: /loop 15s call get_deployment_status "
                    "— show per-resource progress each tick, stop when "
                    "all_completed=true or status=no_deployments."
                ),
            }
        elif core.get("deploy_mode") == "temporal":
            response = _enterprise_queued_response(
                resource_type=resource_type,
                identifier=identifier,
                draft_id=draft_id,
            )
        else:
            pr_url = _extract_pr_url(core["workflow_result"])
            response = {
                "status": "success",
                "action": "pr_raised" if pr_url else "queued",
                "resource_type": resource_type,
                "identifier": identifier,
                "draft_id": draft_id,
                "message": (
                    f"A pull request has been raised at {pr_url}. Your team will "
                    f"manage the deployment of '{identifier}' from there."
                    if pr_url else
                    f"'{identifier}' has been queued for deployment."
                ),
            }

    response["applied"] = diff
    response["placement"] = {
        "product": state.get("product_name"),
        "environment": env_enum.value,
        "region": state["geo_loc_mst_code"],
    }
    return response
