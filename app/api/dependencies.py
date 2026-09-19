from typing import AsyncGenerator, Optional, Tuple
from fastapi import Header, HTTPException, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import logging

import jwt as _pyjwt

from app.db.session import AsyncSessionLocal
from app.core.auth.clerk_jwt import clerk_jwt_validator
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel


def _extract_email_claim(payload: dict) -> Optional[str]:
    """Email from a Clerk token, tolerating the claim names different JWT
    templates use."""
    for claim in ("email", "emailAddress", "email_address", "primaryEmail"):
        value = payload.get(claim)
        if value:
            return str(value)
    return None


def _is_internal_token(token: str) -> bool:
    """Peek at the JWT header to decide whether to validate as MCP internal
    (HS256) or as a Clerk token (RS256). Unverified read is safe — we only
    use the result to choose which validator to call; both validators verify
    the signature.
    """
    try:
        header = _pyjwt.get_unverified_header(token)
    except Exception:
        return False
    return header.get("alg") == "HS256"


def _looks_like_jwt(token: str) -> bool:
    """True if the token is a parseable JWT (Clerk RS256 or MCP-internal HS256),
    False if it's an opaque string.

    MCP OAuth 2.1 access tokens are opaque random strings (not JWTs) whose
    identity lives in Redis, so they fail to parse here and are routed to the
    Redis-lookup branch instead of signature validation.
    """
    try:
        _pyjwt.get_unverified_header(token)
        return True
    except Exception:
        return False

logger = logging.getLogger(__name__)


async def get_db(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
    """
    Database session dependency.

    Provides an async database session to endpoint functions.

    IMPORTANT: For audit trail integration, this dependency checks if middleware
    has already created a session (stored in request.state.audit_session).
    If yes, it uses that session to maintain the same database connection
    (required for session variables to work across middleware and service layers).

    Automatically commits on success and rolls back on exceptions.

    Yields:
        AsyncSession: SQLAlchemy async session
    """
    # Check if middleware already created a session for audit purposes
    if request and hasattr(request.state, "audit_session") and request.state.audit_session:
        # Use the existing session from middleware (same connection!)
        session = request.state.audit_session
        try:
            yield session
            # IMPORTANT: Commit the service layer changes!
            # The middleware created the audit_event and committed it already.
            # Now we need to commit the service layer's changes (e.g., creating tenant/user).
            await session.commit()
        except Exception:
            # Rollback on error
            await session.rollback()
            raise
    else:
        # No audit session - create a new one as usual
        async with AsyncSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()


async def get_current_user_and_tenant(
    authorization: str = Header(..., alias="Authorization"),
    session: AsyncSession = Depends(get_db),
    request: "Request" = None  # FastAPI will inject this
) -> Tuple[UserMstModel, TenantsMstModel]:
    """
    Authentication dependency: Validate JWT and return current user + tenant.

    This dependency:
    1. Extracts JWT token from Authorization header
    2. Validates token using Clerk JWKS
    3. Extracts userId and organizationSubdomain from token
    4. Looks up tenant by subdomain
    5. Looks up user by auth_provider_id (Clerk user ID)
    6. Verifies user belongs to the tenant
    7. Returns (user, tenant) tuple

    Args:
        authorization: Authorization header (e.g., "Bearer <token>")
        session: Database session

    Returns:
        Tuple of (UserMstModel, TenantsMstModel)

    Raises:
        HTTPException 401: If token is invalid or missing
        HTTPException 403: If user/tenant not found or user doesn't belong to tenant

    Example:
        @router.post("/chat")
        async def chat(
            user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant)
        ):
            user, tenant = user_and_tenant
            # User can only access their tenant's data
    """
    # Extract token from Authorization header
    if not authorization.startswith("Bearer "):
        logger.warning("Missing or invalid Authorization header")
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected format: 'Bearer <token>'"
        )

    token = authorization.replace("Bearer ", "", 1).strip()

    # MCP OAuth 2.1 opaque access-token path — the token issued by the DevLift
    # MCP OAuth server (used by the `devlift` CLI and other MCP OAuth clients).
    # Unlike Clerk / MCP-internal tokens it is NOT a JWT: it's an opaque random
    # string whose identity (user_code / tenant_code) lives in Redis. The MCP
    # server already accepts these via its own load_access_token; this branch
    # lets the REST API accept the SAME token by looking it up with the shared
    # resolver, so a single login works across both surfaces.
    if not _looks_like_jwt(token):
        from app.mcp_servers.devlift_mcp.oauth_provider import (
            get_user_from_access_token,
        )

        oauth_user = await get_user_from_access_token(token)
        if not oauth_user:
            # Missing/expired: Redis auto-expires the token at its TTL, so a
            # miss here means the token is invalid or has expired.
            logger.warning("OAuth access token not found or expired")
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired access token",
            )

        user_code = oauth_user["user_code"]
        tenant_code = oauth_user["tenant_code"]

        tenant_stmt = select(TenantsMstModel).where(
            TenantsMstModel.code == tenant_code,
            TenantsMstModel.is_deleted == False,
            TenantsMstModel.is_active == True,
        )
        tenant_result = await session.execute(tenant_stmt)
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"OAuth token: tenant_code not found: {tenant_code}")
            raise HTTPException(
                status_code=403,
                detail=f"Tenant '{tenant_code}' not found or inactive",
            )

        user_stmt = select(UserMstModel).where(
            UserMstModel.code == user_code,
            UserMstModel.is_deleted == False,
            UserMstModel.is_active == True,
        )
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()
        if not user:
            logger.warning(f"OAuth token: user_code not found: {user_code}")
            raise HTTPException(
                status_code=403,
                detail="User not found or inactive (access token).",
            )
        if user.tenants_mst_code != tenant.code:
            logger.error(
                f"OAuth token: user {user.code} tenant {user.tenants_mst_code} "
                f"does not match token tenant {tenant.code}"
            )
            raise HTTPException(
                status_code=403,
                detail="Access denied: user/tenant mismatch in access token",
            )

        if request:
            request.state.user_id = user.id
            request.state.user_email = user.email_id
            request.state.tenant_code = tenant.code
            request.state.user_role = "user"
            if (
                hasattr(request.state, "audit_session")
                and request.state.audit_session
                and not hasattr(request.state, "audit_prepared")
            ):
                await _prepare_audit_context_after_auth(request, session)
                request.state.audit_prepared = True

        logger.debug(
            f"Authenticated via MCP OAuth token: {user.code} for tenant {tenant.code}"
        )
        return user, tenant

    # MCP-internal HS256 path — when chat-bot-POC relays a token minted by
    # the DevLift MCP server. Identity is carried directly in the claims
    # (user_code + tenant_code), so we look up by primary code instead of
    # by Clerk auth_provider_id / organization subdomain.
    if _is_internal_token(token):
        is_valid, payload, error = clerk_jwt_validator.validate_internal_token(token)
        if not is_valid:
            logger.warning(f"Internal JWT validation failed: {error}")
            raise HTTPException(
                status_code=401,
                detail=f"Invalid internal token: {error}",
            )
        user_code = payload["user_code"]
        tenant_code = payload["tenant_code"]

        tenant_stmt = select(TenantsMstModel).where(
            TenantsMstModel.code == tenant_code,
            TenantsMstModel.is_deleted == False,
            TenantsMstModel.is_active == True,
        )
        tenant_result = await session.execute(tenant_stmt)
        tenant = tenant_result.scalar_one_or_none()
        if not tenant:
            logger.warning(f"Internal token: tenant_code not found: {tenant_code}")
            raise HTTPException(
                status_code=403,
                detail=f"Tenant '{tenant_code}' not found or inactive",
            )

        user_stmt = select(UserMstModel).where(
            UserMstModel.code == user_code,
            UserMstModel.is_deleted == False,
            UserMstModel.is_active == True,
        )
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()
        if not user:
            logger.warning(f"Internal token: user_code not found: {user_code}")
            raise HTTPException(
                status_code=403,
                detail="User not found or inactive (internal token).",
            )
        if user.tenants_mst_code != tenant.code:
            logger.error(
                f"Internal token: user {user.code} tenant {user.tenants_mst_code} "
                f"does not match token tenant {tenant.code}"
            )
            raise HTTPException(
                status_code=403,
                detail="Access denied: user/tenant mismatch in internal token",
            )

        if request:
            request.state.user_id = user.id
            request.state.user_email = user.email_id
            request.state.tenant_code = tenant.code
            request.state.user_role = "user"
            if (
                hasattr(request.state, "audit_session")
                and request.state.audit_session
                and not hasattr(request.state, "audit_prepared")
            ):
                await _prepare_audit_context_after_auth(request, session)
                request.state.audit_prepared = True

        logger.debug(
            f"Authenticated via MCP internal token: {user.code} for tenant {tenant.code}"
        )
        return user, tenant

    # Validate JWT token
    is_valid, payload, error = clerk_jwt_validator.validate_token(token)

    if not is_valid:
        logger.warning(f"JWT validation failed: {error}")
        raise HTTPException(
            status_code=401,
            detail=f"Invalid authentication token: {error}"
        )

    # Extract user ID and organization subdomain from JWT
    user_id, organization_subdomain = clerk_jwt_validator.extract_user_claims(payload)

    if not user_id:
        logger.error("JWT payload missing userId claim")
        raise HTTPException(
            status_code=401,
            detail="Invalid token: missing user information"
        )

    if not organization_subdomain:
        logger.error("JWT payload missing organizationSubdomain claim")
        raise HTTPException(
            status_code=401,
            detail="Invalid token: missing organization information"
        )

    # Look up tenant by subdomain
    tenant_stmt = select(TenantsMstModel).where(
        TenantsMstModel.subdomain == organization_subdomain,
        TenantsMstModel.is_deleted == False,
        TenantsMstModel.is_active == True
    )
    tenant_result = await session.execute(tenant_stmt)
    tenant = tenant_result.scalar_one_or_none()

    if not tenant:
        logger.warning(f"Tenant not found for subdomain: {organization_subdomain}")
        raise HTTPException(
            status_code=403,
            detail=f"Organization '{organization_subdomain}' not found or inactive"
        )

    # Look up user by Clerk user ID
    user_stmt = select(UserMstModel).where(
        UserMstModel.auth_provider_id == user_id,
        UserMstModel.is_deleted == False,
        UserMstModel.is_active == True
    )
    user_result = await session.execute(user_stmt)
    user = user_result.scalar_one_or_none()

    if not user:
        # Clerk instance migration: this row still carries the auth_provider_id
        # minted by the previous instance. Re-key it off the token's email,
        # scoped to the tenant we already resolved from the subdomain claim.
        email = _extract_email_claim(payload)
        if email:
            email_stmt = select(UserMstModel).where(
                func.lower(UserMstModel.email_id) == email.lower(),
                UserMstModel.tenants_mst_code == tenant.code,
                UserMstModel.is_deleted == False,
                UserMstModel.is_active == True
            )
            email_result = await session.execute(email_stmt)
            user = email_result.scalar_one_or_none()

            if user:
                previous_id = user.auth_provider_id
                user.auth_provider_id = user_id
                session.add(user)
                logger.warning(
                    f"[AUTH] Re-keyed user {user.code} ({email}) from "
                    f"auth_provider_id {previous_id} to {user_id}"
                )

    if not user:
        logger.warning(f"User not found for auth_provider_id: {user_id}")
        raise HTTPException(
            status_code=403,
            detail="User not found or inactive. Please contact your administrator."
        )

    # Verify user belongs to the tenant
    if user.tenants_mst_code != tenant.code:
        logger.error(
            f"User {user.code} (tenant: {user.tenants_mst_code}) "
            f"attempted to access tenant {tenant.code}"
        )
        raise HTTPException(
            status_code=403,
            detail="Access denied: You do not belong to this organization"
        )

    logger.debug(
        f"Authenticated user: {user.code} ({user.email_id}) "
        f"for tenant: {tenant.code} ({tenant.name})"
    )

    # Set user/tenant info in request.state for audit middleware
    if request:
        request.state.user_id = user.id
        request.state.user_email = user.email_id
        request.state.tenant_code = tenant.code
        request.state.user_role = "user"  # TODO: Add actual role field to user model

        # Prepare audit context NOW that we have tenant info
        # This must happen AFTER authentication but BEFORE service layer
        if hasattr(request.state, "audit_session") and request.state.audit_session and not hasattr(request.state, "audit_prepared"):
            await _prepare_audit_context_after_auth(request, session)
            request.state.audit_prepared = True

    return user, tenant


async def _prepare_audit_context_after_auth(request: "Request", session: AsyncSession):
    """
    Prepare audit context AFTER JWT validation.

    This runs after we know the tenant_code, so we can create audit_event.
    """
    try:
        import json
        from sqlalchemy import text
        from app.services.audit_service import AuditService
        from app.core.enum import AuditActionEnum
        from app.utils.audit_policy import (
            HIDDEN_PAYLOAD,
            is_sensitive_route,
            sanitize_payload,
        )

        # Get audit session from request state (created by middleware)
        audit_session = request.state.audit_session

        # Extract request info
        request_method = getattr(request.state, "audit_request_method", request.method)
        request_path = getattr(request.state, "audit_request_path", str(request.url.path))

        # Raw body, captured by the middleware into a mutable container.
        request_body_container = getattr(request.state, "audit_request_body_container", [b""])
        request_body = request_body_container[0]

        ip_address = getattr(request.state, "audit_ip_address", None)
        user_agent = getattr(request.state, "audit_user_agent", None)
        request_id = getattr(request.state, "audit_request_id", None)

        # Get authenticated user info
        user_id = request.state.user_id
        username = request.state.user_email
        tenant_code = request.state.tenant_code
        role = request.state.user_role

        # Determine action type
        method_to_action = {
            "POST": AuditActionEnum.CREATE,
            "GET": AuditActionEnum.READ,
            "PUT": AuditActionEnum.UPDATE,
            "PATCH": AuditActionEnum.UPDATE,
            "DELETE": AuditActionEnum.DELETE,
        }
        action_type = method_to_action.get(request_method, AuditActionEnum.READ)

        # Extract resource type from path - use FULL path after /api/v1/
        # Example: /api/v1/applications/create-application -> "applications/create-application"
        # Example: /api/v1/monitoring-policies/update-policy-override/POL_123 -> "monitoring-policies/update-policy-override/POL_123"
        #
        # "api/v1" is located anywhere in the path rather than assumed at index
        # 0, so a route served under a mount prefix still resolves. Behaviour is
        # unchanged for this service, where api/v1 is already first.
        parts = request_path.strip("/").split("/")
        resource_type = "unknown"
        for i in range(len(parts) - 1):
            if parts[i] == "api" and parts[i + 1] == "v1":
                resource_type = "/".join(parts[i + 2:]) or "unknown"
                break

        # Request body. Stored for every endpoint EXCEPT the secret/variable
        # surface, where it is replaced with a withheld marker, and with
        # credential-named fields redacted at any depth everywhere else.
        # See app/utils/audit_policy.py for both rules and why one alone is
        # not enough. The old code here json.loads()'d the body and checked
        # five exact key names at the TOP LEVEL only, which is how secret
        # values submitted as items[].value reached this column in plaintext.
        request_payload = None
        if request_method in ("POST", "PUT", "PATCH", "DELETE") and request_body:
            if is_sensitive_route(request_path):
                request_payload = HIDDEN_PAYLOAD
            else:
                try:
                    request_payload = sanitize_payload(json.loads(request_body))
                except Exception as exc:
                    logger.debug(f"Audit: request body is not JSON: {exc}")

        audit_service = AuditService(audit_session)

        # Create audit_actor
        actor = await audit_service.actor_repository.get_or_create_actor(
            user_id=user_id,
            username=username,
            role=role,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        # Generate event_id from sequence
        event_id = await audit_service.event_repository.generate_event_id()

        # Create basic audit_event first
        await audit_service.event_repository.create_event_with_id(
            event_id=event_id,
            actor_id=actor.actor_id,
            event_name=action_type,
            resource_type=resource_type,
            resource_id=None,  # Will be updated in finalize
            tenants_mst_code=tenant_code,
            event_source=f"{request_method} {request_path}",
            status="pending",
            correlation_id=request_id,
            request_payload=request_payload,
            response_payload=None,  # Will be updated in finalize
        )

        # CRITICAL: Clear any stale session variable from connection pool reuse FIRST
        await audit_session.execute(
            text("SELECT set_config('app.audit_event_id', NULL, false)")
        )

        # Commit so audit_event exists in database
        await audit_session.commit()

        # Set PostgreSQL session variable AFTER commit
        # This ensures it's set for the NEW transaction that the service layer will use
        # Using 'false' = session-level (persists across transactions on same connection)
        await audit_session.execute(
            text("SELECT set_config('app.audit_event_id', :event_id, false)"),
            {"event_id": str(event_id)}
        )

        # Log to verify variable was set correctly
        logger.debug(f"[AUDIT] Set session variable to event_id={event_id} for {request_method} {request_path}")

        # Store event_id for finalize
        request.state.audit_event_id = event_id

        logger.debug(f"[AUDIT] Audit context prepared: event_id={event_id}, tenant={tenant_code}, user={username}")

    except Exception as e:
        logger.error(f"Failed to prepare audit context after auth: {str(e)}", exc_info=True)
        # Don't fail the request - audit is secondary.
        #
        # But ROLL BACK before continuing: a failed query (e.g. a deadlock
        # against concurrent DDL on the shared stage DB) leaves the transaction
        # aborted, and get_db() hands THIS session to the route handlers — so
        # without the rollback every later query in the request dies with
        # "current transaction is aborted", turning one transient audit hiccup
        # into a fully broken request that reports a misleading error.
        try:
            audit_session = getattr(request.state, "audit_session", None)
            if audit_session is not None:
                await audit_session.rollback()
        except Exception:
            logger.error("Audit-context rollback itself failed", exc_info=True)


async def get_current_user_tenant_and_azp(
    authorization: str = Header(..., alias="Authorization"),
    session: AsyncSession = Depends(get_db),
    request: "Request" = None
) -> Tuple[UserMstModel, TenantsMstModel, str]:
    """
    Authentication dependency: Validate JWT and return user + tenant + azp.

    Same as get_current_user_and_tenant but also returns azp (authorized party).
    The azp claim contains the origin URL (e.g., "https://vance.devlift.ai").

    Use this when you need to know the frontend origin URL (e.g., for invitation links).

    Returns:
        Tuple of (UserMstModel, TenantsMstModel, azp_url)

    Raises:
        HTTPException 401: If azp is missing from token

    Example:
        @router.post("/invitations/send")
        async def send_invitation(
            user_tenant_azp: Tuple = Depends(get_current_user_tenant_and_azp)
        ):
            user, tenant, azp = user_tenant_azp
            # azp contains origin URL like "https://vance.devlift.ai"
    """
    # Reuse existing logic to get user and tenant
    user, tenant = await get_current_user_and_tenant(authorization, session, request)

    # Extract JWT payload again to get azp
    token = authorization.replace("Bearer ", "", 1).strip()
    is_valid, payload, _ = clerk_jwt_validator.validate_token(token)

    azp = None
    if is_valid and payload:
        azp = clerk_jwt_validator.extract_azp(payload)

    if not azp:
        logger.error("JWT payload missing azp (authorized party) claim")
        raise HTTPException(
            status_code=401,
            detail="Invalid token: missing authorized party information"
        )

    logger.info(f"[AUTH] Extracted azp from JWT: {azp}")
    return user, tenant, azp


async def get_clerk_user_id_from_jwt(
    authorization: str = Header(..., alias="Authorization")
) -> str:
    """
    Extract and validate Clerk user ID from JWT token (for signup flow).

    This dependency is used for user signup where user exists in Clerk
    but NOT in our database yet.

    This dependency:
    1. Extracts JWT token from Authorization header
    2. Validates token using Clerk JWKS
    3. Extracts userId (Clerk ID) from token
    4. Returns Clerk user ID (to be stored as auth_provider_id)

    Args:
        authorization: Authorization header (e.g., "Bearer <token>")

    Returns:
        str: Clerk user ID (e.g., "user_xyz123") - to be stored as auth_provider_id

    Raises:
        HTTPException 401: If token is invalid or missing

    Example:
        @router.post("/signup/create-organization")
        async def create_organization(
            clerk_user_id: str = Depends(get_clerk_user_id_from_jwt)
        ):
            # clerk_user_id will be stored as auth_provider_id in user_mst table
            # with auth_provider = AuthProviderEnum.clerk
    """
    # Extract token from Authorization header
    if not authorization.startswith("Bearer "):
        logger.warning("Missing or invalid Authorization header")
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. Expected format: 'Bearer <token>'"
        )

    token = authorization.replace("Bearer ", "", 1).strip()

    # Validate JWT token
    is_valid, payload, error = clerk_jwt_validator.validate_token(token)

    if not is_valid:
        logger.warning(f"JWT validation failed: {error}")
        raise HTTPException(
            status_code=401,
            detail=f"Invalid authentication token: {error}"
        )

    # Extract user ID from JWT (this is the Clerk user ID)
    user_id, _ = clerk_jwt_validator.extract_user_claims(payload)

    if not user_id:
        logger.error("JWT payload missing userId claim")
        raise HTTPException(
            status_code=401,
            detail="Invalid token: missing user information"
        )

    logger.info(f"[SIGNUP] Extracted Clerk user ID from JWT: {user_id}")

    # This user_id will be stored as auth_provider_id in user_mst table
    # with auth_provider = AuthProviderEnum.clerk
    return user_id


async def require_organization_owner(
    authorization: str = Header(..., alias="Authorization"),
    session: AsyncSession = Depends(get_db),
    request: "Request" = None
) -> Tuple[UserMstModel, TenantsMstModel]:
    """
    Authorization dependency: Requires user to be organization owner.

    This dependency reuses get_current_user_and_tenant and adds
    an additional check for isOrganizationOwner in the JWT token.

    Only users with isOrganizationOwner: true in their JWT can access
    endpoints protected by this dependency (e.g., audit trail).

    Args:
        authorization: Authorization header (e.g., "Bearer <token>")
        session: Database session
        request: FastAPI request object

    Returns:
        Tuple of (UserMstModel, TenantsMstModel)

    Raises:
        HTTPException 401: If token is invalid
        HTTPException 403: If user is not organization owner

    Example:
        @router.get("/audit-trail")
        async def get_audit_trail(
            user_and_tenant: Tuple = Depends(require_organization_owner)
        ):
            user, tenant = user_and_tenant
            # Only organization owners can reach here
    """
    # First, validate token and extract payload
    token = authorization.replace("Bearer ", "", 1).strip() if authorization.startswith("Bearer ") else ""

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header"
        )

    is_valid, payload, error = clerk_jwt_validator.validate_token(token)

    if not is_valid:
        logger.warning(f"JWT validation failed: {error}")
        raise HTTPException(
            status_code=401,
            detail=f"Invalid authentication token: {error}"
        )

    # Check if user is organization owner
    is_org_owner = payload.get("isOrganizationOwner", False)

    if not is_org_owner:
        logger.warning(f"User attempted to access owner-only resource without permission")
        raise HTTPException(
            status_code=403,
            detail="Access denied: Only organization owners can access this resource"
        )

    # Reuse existing logic to get user and tenant
    user, tenant = await get_current_user_and_tenant(authorization, session, request)

    logger.info(
        f"Org owner authenticated: {user.code} ({user.email_id}) "
        f"for tenant: {tenant.code} ({tenant.name})"
    )

    return user, tenant