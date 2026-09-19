from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.v1.router import api_router
from app.core.config import settings, ALLOWED_ORIGINS, SUBDOMAIN_ORIGIN_REGEX
from app.middleware.audit_middleware import AuditMiddleware
from app.core.authz.security import AuthTripwire, public_surface
from app.core.logging_config import setup_logging
import asyncio
from datetime import timedelta
import logging
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import time

# Import all models to ensure they're registered with SQLAlchemy before any queries
import app.db.models  # noqa: F401

import contextlib
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_onboarding_sse import create_mcp_server
from app.mcp_servers.devlift_mcp.server import create_devlift_mcp_server

# Initialize logging configuration with values from Settings
# This must happen AFTER settings is loaded (imported above)
setup_logging(
    log_level=settings.log_level,
    log_file=settings.log_file if settings.log_file else None,
    retention_days=settings.log_retention_days
)

logger = logging.getLogger(__name__)

# Create MCP server with debugging
mcp = create_mcp_server()
logger.info("=" * 80)
logger.info("MCP Server initialized")
logger.info(f"MCP Server name: {mcp.name}")
logger.info("=" * 80)

# Create DevLift MCP server (developer-facing — provisions S3, DynamoDB, k8s_postgres, EKS service)
devlift_mcp = create_devlift_mcp_server()
logger.info("=" * 80)
logger.info("DevLift MCP Server initialized")
logger.info(f"DevLift MCP Server name: {devlift_mcp.name}")
logger.info("=" * 80)

# Global Slack handler instance
slack_handler = None


# ============================================================
# MCP DEBUG MIDDLEWARE
# ============================================================
class MCPDebugMiddleware(BaseHTTPMiddleware):
    """Middleware to debug MCP requests."""

    async def dispatch(self, request: Request, call_next):
        # Only log MCP routes
        if request.url.path.startswith("/mcp"):
            start_time = time.time()

            logger.info("=" * 80)
            logger.info(f"MCP REQUEST: {request.method} {request.url.path}")
            logger.info(f"Query params: {dict(request.query_params)}")
            logger.info(f"Headers: {dict(request.headers)}")

            # Read and log body for non-SSE requests
            if request.headers.get("accept") != "text/event-stream":
                try:
                    body = await request.body()
                    logger.info(f"Body: {body[:500]}...")  # First 500 chars
                except Exception as e:
                    logger.info(f"Could not read body: {e}")

            response = await call_next(request)

            process_time = time.time() - start_time
            logger.info(f"MCP RESPONSE: Status {response.status_code} in {process_time:.3f}s")
            logger.info("=" * 80)

            return response

        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown."""
    global slack_handler

    # ============================================================
    # STARTUP
    # ============================================================

    # Initialize Redis in background (if enabled)
    if settings.redis_enabled:
        asyncio.create_task(_init_redis_background())
    else:
        logger.info("Redis is disabled (REDIS_ENABLED=false) - using in-memory state (single worker only)")

    # Initialize LangGraph checkpointer in background
    asyncio.create_task(_init_checkpointer_background())

    # Initialize Temporal client + ensure coordinator workflow is running
    if settings.temporal_enabled:
        asyncio.create_task(_init_temporal_background())
    else:
        logger.info("Temporal is disabled (TEMPORAL_ENABLED=false)")

    # Start Slack Socket Mode in background (if enabled)
    if settings.slack_socket_mode_enabled:
        try:
            logger.info("Slack Socket Mode is enabled - initializing...")
            from app.services.slack.socket_mode_handler import SlackSocketModeHandler

            slack_handler = SlackSocketModeHandler()

            # Start Socket Mode in background task
            asyncio.create_task(slack_handler.start())

            logger.info("Slack Socket Mode started successfully")
        except Exception as e:
            logger.error(f"Failed to start Slack Socket Mode: {str(e)}")
            # Don't crash the app if Slack fails to start
            slack_handler = None
    else:
        logger.info("Slack Socket Mode is disabled (SLACK_SOCKET_MODE_ENABLED=false)")

    # Run both MCP session managers when mounted
    async with mcp.session_manager.run(), devlift_mcp.session_manager.run():
        yield

    # ============================================================
    # SHUTDOWN
    # ============================================================

    # Stop Slack Socket Mode
    if slack_handler:
        try:
            logger.info("Shutting down Slack Socket Mode...")
            await slack_handler.stop()
            logger.info("Slack Socket Mode stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping Slack Socket Mode: {str(e)}")

    # Close LangGraph checkpointer
    try:
        logger.info("Closing LangGraph checkpointer...")
        from app.infra_chat_agent.checkpointer import close_checkpointer
        await close_checkpointer()
        logger.info("LangGraph checkpointer closed successfully")
    except Exception as e:
        logger.error(f"Error closing LangGraph checkpointer: {str(e)}")

    # Close Redis connection pool
    if settings.redis_enabled:
        try:
            logger.info("Closing Redis connection pool...")
            from app.integrations.redis_integration import RedisIntegration

            await RedisIntegration.close()
            logger.info("Redis connection pool closed successfully")
        except Exception as e:
            logger.error(f"Error closing Redis connection: {str(e)}")

app = FastAPI(
    title="ObsTool Monitor API",
    description="API for managing observability alerts and monitoring",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(AuthTripwire)



# Add MCP Debug Middleware (add before CORS to see all requests)
app.add_middleware(MCPDebugMiddleware)

# Configure CORS
# allow_origins handles static origins (localhost, Vercel previews)
# allow_origin_regex handles dynamic tenant subdomains (*.devlift.ai, *.*.devlift.ai)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=SUBDOMAIN_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers
    expose_headers=["Location"],  # Allow frontend to read redirect target
)

# Add Audit Middleware (captures IP, user agent, etc. for audit logging)
app.add_middleware(AuditMiddleware)

# The React console (ui/): serve the production build at /ui when it exists.
_ui_dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _ui_dist.exists():
    app.mount("/ui", StaticFiles(directory=_ui_dist, html=True), name="ui")

# Include API v1 router.
# public_surface here is the tripwire declaration for the LEGACY surface:
# every route on it still authenticates via get_current_user_and_tenant
# exactly as before; routers migrate to SecureRouter cards one by one, and
# their guards' stamps take over route by route.
app.include_router(api_router, prefix="/api/v1", dependencies=[
    public_surface("legacy surface — auth via get_current_user_and_tenant; card migration in progress")
])

# Mount static files
# ************************** To be removed *********************************
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Static files mounted at /static from {static_dir}")
else:
    logger.warning(f"Static directory not found at {static_dir}")


@app.get("/", dependencies=[public_surface("service banner")])
def root():
    return {
        "message": "ObsTool Monitor API running",
        "version": "1.0.0",
        "docs": "/docs"
    }

@app.get("/health", dependencies=[public_surface("liveness probe")])
def health_check():
    return {
        "status": "healthy",
        "database": settings.db_name
    }


@app.get("/health/qdrant", dependencies=[public_surface("health probe")])
async def qdrant_health_check():
    """
    Check Qdrant vector database connectivity and collection status.

    Used to verify vector search functionality is available for Service Sage.
    """
    if not settings.qdrant_enabled:
        return {
            "status": "disabled",
            "qdrant_enabled": False,
            "message": "Qdrant is disabled (QDRANT_ENABLED=false)",
        }

    from app.integrations.qdrant_integration import QdrantIntegration

    is_healthy = await QdrantIntegration.health_check()
    collection_name = settings.qdrant_collection_name
    collection_exists = await QdrantIntegration.collection_exists(collection_name) if is_healthy else False
    collection_info = await QdrantIntegration.get_collection_info(collection_name) if collection_exists else None

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "qdrant_enabled": True,
        "qdrant_host": settings.qdrant_host,
        "qdrant_port": settings.qdrant_port,
        "qdrant_reachable": is_healthy,
        "collection_name": collection_name,
        "collection_exists": collection_exists,
        "vectors_count": collection_info.get("vectors_count") if collection_info else None,
    }


@app.get("/health/redis", dependencies=[public_surface("health probe")])
async def redis_health_check():
    """
    Check Redis cache connectivity.

    Used to verify distributed state management is available for multi-worker deployments.
    """
    from app.integrations.redis_integration import RedisIntegration

    is_healthy = await RedisIntegration.health_check()

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "redis_enabled": settings.redis_enabled,
        "redis_host": settings.redis_host,
        "redis_port": settings.redis_port,
        "redis_reachable": is_healthy,
    }


@app.get("/health/mcp", dependencies=[public_surface("health probe")])
async def mcp_health_check():
    """
    Check MCP server configuration and available tools.

    Provides debugging information about the MCP server setup.
    """
    try:
        # Get MCP server info
        tools_info = []
        if hasattr(mcp, '_tool_manager') and hasattr(mcp._tool_manager, 'tools'):
            for tool_name, tool_info in mcp._tool_manager.tools.items():
                tools_info.append({
                    "name": tool_name,
                    "description": getattr(tool_info, 'description', 'No description'),
                })

        return {
            "status": "configured",
            "mcp_server_name": mcp.name,
            "mcp_endpoint": "/mcp",
            "available_tools": tools_info if tools_info else "Unable to retrieve tools",
            "session_manager": str(type(mcp.session_manager)),
            "instructions": {
                "info": "MCP app is mounted at root with internal route /mcp",
                "endpoint": "POST /mcp - for MCP protocol messages (JSON-RPC 2.0)",
                "note": "The endpoint accepts both SSE and JSON requests based on Accept header",
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "mcp_server_name": getattr(mcp, 'name', 'Unknown'),
        }


# ============================================================
# MCP APP MOUNT (MUST BE LAST - AFTER ALL ROUTES)
# ============================================================

# ── Root-level OAuth discovery routes (RFC 8414 + RFC 9728) ──
# FastMCP's sub-app serves .well-known inside /devlift-mcp/ but
# RFC requires them at the host root. Register them here first.
# Ref: https://github.com/modelcontextprotocol/python-sdk/issues/1751
from starlette.routing import Route as StarletteRoute
from starlette.responses import JSONResponse as StarletteJSONResponse
from mcp.server.auth.routes import create_protected_resource_routes, build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl

_issuer = settings.mcp_oauth_issuer_url
_resource = settings.mcp_oauth_resource_server_url

_as_metadata = build_metadata(
    issuer_url=AnyHttpUrl(_issuer),
    service_documentation_url=None,
    client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[]),
    revocation_options=RevocationOptions(enabled=True),
)

_prm_routes = create_protected_resource_routes(
    resource_url=AnyHttpUrl(_resource),
    authorization_servers=[AnyHttpUrl(_issuer)],
)

async def _as_metadata_handler(request):
    return StarletteJSONResponse(
        content=_as_metadata.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "public, max-age=3600"},
    )

# Insert at front of app.routes so they're matched before sub-app mounts
app.routes.insert(0, StarletteRoute(
    "/.well-known/oauth-authorization-server/devlift-mcp",
    endpoint=cors_middleware(_as_metadata_handler, ["GET", "OPTIONS"]),
    methods=["GET", "OPTIONS"],
))
for r in _prm_routes:
    app.routes.insert(0, r)

logger.info("OAuth .well-known routes registered at root level")

# ── DevLift MCP sub-app ──
# RFC 9728 requires oauth-protected-resource metadata at
# {resource}/.well-known/oauth-protected-resource. Our resource is
# /devlift-mcp/mcp, so the metadata must live at /devlift-mcp/mcp/.well-known/...
# FastMCP only exposes it at the mount root, and Claude Code follows the
# www-authenticate header which uses the spec path — so we add an alias here.
async def _devlift_prm_handler(request):
    return StarletteJSONResponse(
        content={
            "resource": _resource,
            "authorization_servers": [_issuer],
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )

app.routes.insert(0, StarletteRoute(
    "/devlift-mcp/mcp/.well-known/oauth-protected-resource",
    endpoint=cors_middleware(_devlift_prm_handler, ["GET", "OPTIONS"]),
    methods=["GET", "OPTIONS"],
))

# Claude Code's MCP SDK probes OIDC discovery (openid-configuration). Serve a
# minimal OIDC discovery doc that satisfies its schema validator: OAuth AS
# metadata + the three OIDC-required fields (jwks_uri, subject_types_supported,
# id_token_signing_alg_values_supported). We don't issue ID tokens, so the JWKs
# endpoint returns an empty key set — the SDK only validates that it loads.
_devlift_jwks_uri = f"{_issuer}/.well-known/jwks.json"

async def _devlift_oidc_handler(request):
    content = _as_metadata.model_dump(mode="json", exclude_none=True)
    content.update({
        "jwks_uri": _devlift_jwks_uri,
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    })
    return StarletteJSONResponse(
        content=content,
        headers={"Cache-Control": "public, max-age=3600"},
    )

async def _devlift_jwks_handler(request):
    return StarletteJSONResponse(
        content={"keys": []},
        headers={"Cache-Control": "public, max-age=3600"},
    )

for _oidc_path in (
    "/devlift-mcp/.well-known/openid-configuration",
    "/devlift-mcp/mcp/.well-known/openid-configuration",
):
    app.routes.insert(0, StarletteRoute(
        _oidc_path,
        endpoint=cors_middleware(_devlift_oidc_handler, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    ))

app.routes.insert(0, StarletteRoute(
    "/devlift-mcp/.well-known/jwks.json",
    endpoint=cors_middleware(_devlift_jwks_handler, ["GET", "OPTIONS"]),
    methods=["GET", "OPTIONS"],
))

devlift_mcp_app = devlift_mcp.streamable_http_app()
app.mount("/devlift-mcp", devlift_mcp_app)
logger.info("DevLift MCP mounted at /devlift-mcp/mcp")

# Create MCP app with debugging
mcp_app = mcp.streamable_http_app()
logger.info("=" * 80)
logger.info("MCP HTTP App created")
logger.info(f"MCP App type: {type(mcp_app)}")
logger.info(f"MCP App routes: {getattr(mcp_app, 'routes', 'No routes attribute')}")
logger.info("=" * 80)

# Mount MCP app at root
# IMPORTANT: This MUST be done AFTER all FastAPI routes are registered
# The MCP app has an internal route at /mcp, so mounting at "" makes it available at /mcp
# Mounting at root ("") means it acts as a catch-all for unmatched routes
app.mount("", mcp_app)
logger.info("MCP app mounted - MCP endpoint available at /mcp")


# ============================================================
# BACKGROUND INITIALIZATION HELPERS
# ============================================================

async def _init_redis_background() -> None:
    """Initialize Redis in the background — never crashes the server."""
    try:
        logger.info("Redis is enabled - initializing connection pool in background...")

        from app.integrations.redis_integration import RedisIntegration

        redis_client = await RedisIntegration.get_client_async()
        if redis_client:
            logger.info(f"Redis initialized successfully: {settings.redis_host}:{settings.redis_port}")
        else:
            logger.error("Redis initialization failed - client is None")
    except Exception as e:
        logger.error(f"Failed to initialize Redis in background: {e}")


async def _init_checkpointer_background() -> None:
    """Initialize LangGraph checkpointer in the background — never crashes the server."""
    try:
        logger.info("Initializing LangGraph PostgreSQL checkpointer in background...")
        from app.infra_chat_agent.checkpointer import init_checkpointer
        await init_checkpointer()
        logger.info("LangGraph checkpointer initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize LangGraph checkpointer in background: {e}")


async def _init_temporal_background() -> None:
    """
    Connect to Temporal and ensure each tenant has a running coordinator workflow.

    The coordinator is started with workflow_id_reuse_policy=ALLOW_DUPLICATE
    so re-runs on app restart without error even if the workflow is already running
    (Temporal ignores the start if a running workflow with that ID already exists).
    """
    try:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError
        from app.temporal.client import get_temporal_client
        from app.temporal.workflows.coordinator_workflow import TenantCoordinatorWorkflow
        from app.db.session import AsyncSessionLocal
        from app.repository.tenants_mst_repository import TenantsMstRepository

        client = await get_temporal_client()
        logger.info("Temporal client connected")

        for tenant_code in settings.temporal_deploy_tenants_list:
            coordinator_id = f"coordinator-{tenant_code}"
            try:
                await client.start_workflow(
                    TenantCoordinatorWorkflow.run,
                    tenant_code,
                    id=coordinator_id,
                    task_queue=settings.temporal_task_queue,
                    # The default 10s workflow-task timeout is a cold-replay
                    # deadline: a worker that has just restarted must replay
                    # the coordinator's whole history inside it or the server
                    # discards the result and retries, and deploys stall
                    # behind it (~70s per attempt at 39k events on
                    # 2026-09-11). Continue-as-new keeps history near 5k, but
                    # that is still ~7.5s on this worker — too close.
                    task_timeout=timedelta(seconds=60),
                )
                logger.info(f"Coordinator workflow started/confirmed: {coordinator_id}")
            except Exception as e:
                logger.info(f"Coordinator {coordinator_id} already running or start skipped: {e}")

        logger.info("Temporal initialization complete")
    except Exception as e:
        logger.error(f"Failed to initialize Temporal in background: {e}")


