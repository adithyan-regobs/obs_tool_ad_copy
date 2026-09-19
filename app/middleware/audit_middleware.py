"""
Audit Middleware

Automatically captures and logs ALL HTTP requests to the audit trail.
Developers don't need to manually add audit logging to their endpoints.
"""
import json
import logging
from contextvars import ContextVar
from typing import Optional
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.utils.audit_policy import (
    HIDDEN_PAYLOAD,
    is_sensitive_route,
    sanitize_payload,
)

logger = logging.getLogger(__name__)

# Context variables to store request information across async calls
audit_context_ip: ContextVar[Optional[str]] = ContextVar("audit_context_ip", default=None)
audit_context_user_agent: ContextVar[Optional[str]] = ContextVar("audit_context_user_agent", default=None)
audit_context_request_id: ContextVar[Optional[str]] = ContextVar("audit_context_request_id", default=None)
audit_context_request_method: ContextVar[Optional[str]] = ContextVar("audit_context_request_method", default=None)
audit_context_request_path: ContextVar[Optional[str]] = ContextVar("audit_context_request_path", default=None)


class AuditMiddleware:
    """
    ASGI Middleware that automatically logs ALL HTTP requests to the audit trail.

    This middleware:
    1. Opens the shared audit DB session and captures request context
       (IP, user agent, correlation id)
    2. The auth dependency creates the audit_event once the tenant is known
    3. Finalizes the event after the response (status, resource_id)

    Payloads: request and response bodies ARE stored, because that detail is
    what makes the trail useful. Two filters decide what survives, both in
    app/utils/audit_policy.py — the secret/variable routes store a withheld
    marker instead of their body (they carry plain values), and every other
    body has credential-named fields redacted at any depth.

    No manual audit logging needed in endpoints!
    """

    # Skip audit logging for these paths
    SKIP_PATHS = [
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
        "/api/v1/audit-trail",  # Don't audit the audit trail itself
    ]

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract request info from scope
        request_method = scope["method"]
        request_path = scope["path"]
        headers = Headers(scope=scope)

        # Skip audit logging for certain paths
        if self._should_skip_audit(request_path):
            await self.app(scope, receive, send)
            return

        # Capture the request body into a mutable container, so the auth
        # dependency can read it once the tenant is known (it runs later, and
        # the body has been consumed by then). What is actually STORED is
        # decided in app/utils/audit_policy.py, not here.
        request_body_container = [b""]

        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                request_body_container[0] += message.get("body", b"")
            return message

        # Capture response body and status
        response_body = b""
        response_status = 200

        async def send_wrapper(message: Message):
            nonlocal response_body, response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                response_body += body
            await send(message)

        # Extract context info
        ip_address = self._get_client_ip(scope, headers)
        user_agent = headers.get("User-Agent")

        # Get or generate correlation ID for request tracing
        request_id = headers.get("X-Request-ID")
        if not request_id:
            # Generate a correlation ID if frontend didn't provide one
            import uuid
            request_id = str(uuid.uuid4())

        # Store in context variables
        audit_context_ip.set(ip_address)
        audit_context_user_agent.set(user_agent)
        audit_context_request_id.set(request_id)
        audit_context_request_method.set(request_method)
        audit_context_request_path.set(request_path)

        # Create audit session and prepare audit context BEFORE request processing
        # This is critical for field-level change tracking:
        # 1. We create a database session here
        # 2. Set session variable with event_id
        # 3. Service layers use the SAME session (via get_db dependency)
        # 4. Database triggers can read the session variable
        audit_session = None
        event_id = None
        actor_id = None

        try:
            # Create session early - this will be shared with service layers
            audit_session = AsyncSessionLocal()

            # Store session and request info in scope
            # The JWT dependency will use this to prepare audit context AFTER authentication
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["audit_session"] = audit_session
            scope["state"]["audit_request_method"] = request_method
            scope["state"]["audit_request_path"] = request_path
            # The mutable container, NOT its value: the body is still being
            # received when this runs, and the dependency reads it later.
            scope["state"]["audit_request_body_container"] = request_body_container
            scope["state"]["audit_ip_address"] = ip_address
            scope["state"]["audit_user_agent"] = user_agent
            scope["state"]["audit_request_id"] = request_id

        except Exception as e:
            logger.error(f"Failed to create audit session: {str(e)}", exc_info=True)
            # Close the session if it was created before the exception
            if audit_session:
                try:
                    await audit_session.close()
                except Exception:
                    pass
                audit_session = None
            # Continue even if audit preparation fails

        # Process the request (using the shared audit_session)
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            # After request processing, update audit_event with response and cleanup
            # IMPORTANT: Await finalization to ensure session is properly closed
            # This is critical to prevent connection leaks
            await self._finalize_audit(
                session=audit_session,
                scope=scope,
                request_method=request_method,
                request_path=request_path,
                response_body=response_body,
                response_status=response_status,
            )

    def _get_client_ip(self, scope: Scope, headers: Headers) -> Optional[str]:
        """Extract client IP from scope or headers"""
        # Check forwarded headers first
        forwarded_for = headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
            # ALBs with routing.http.xff_client_port.enabled append ":port",
            # which the INET column rejects. IPv4 has a single colon; bare
            # IPv6 has several and is left alone.
            if client_ip.count(":") == 1:
                client_ip = client_ip.rsplit(":", 1)[0]
            return client_ip

        # Get from scope
        client = scope.get("client")
        if client:
            return client[0]

        return None

    def _should_skip_audit(self, path: str) -> bool:
        """Check if this path should skip audit logging"""
        return any(path.startswith(skip_path) for skip_path in self.SKIP_PATHS)

    async def _finalize_audit(
        self,
        session: Optional[AsyncSession],
        scope: Scope,
        request_method: str,
        request_path: str,
        response_body: bytes,
        response_status: int,
    ):
        """
        Finalize audit event AFTER response is sent.

        This method:
        1. Updates audit_event with the final status and resource_id
        2. Commits final changes
        3. Closes the database session

        Runs as a background task - doesn't block response.
        """
        try:
            if not session:
                return

            from sqlalchemy import text, update
            from app.db.models.audit_log_model import AuditEventModel

            # Get event_id from scope
            state = scope.get("state", {})
            event_id = state.get("audit_event_id")
            tenant_code = state.get("tenant_code")

            if not event_id:
                return

            # Parse once: the resource code is always taken from the response,
            # but whether the body is STORED depends on the policy below.
            parsed_response = self._parse_response_body(response_body)
            resource_id = self._extract_resource_code_from_response(parsed_response)

            # Response body. Withheld entirely on the secret/variable surface —
            # GET .../with-values returns DECRYPTED values — and otherwise
            # stored with credential-named fields redacted at any depth.
            if is_sensitive_route(request_path):
                response_payload = HIDDEN_PAYLOAD
            else:
                response_payload = sanitize_payload(parsed_response)

            # Determine final status
            status = "success" if 200 <= response_status < 300 else "failure"

            # Update audit_event with the final outcome + response payload
            # (withheld marker on the secret/variable surface).
            stmt = (
                update(AuditEventModel)
                .where(AuditEventModel.event_id == event_id)
                .values(
                    resource_id=resource_id,
                    status=status,
                    response_payload=response_payload,
                    tenants_mst_code=tenant_code if tenant_code else "unauthenticated",
                )
            )
            await session.execute(stmt)

            # Final commit
            await session.commit()

            logger.debug(f"Audit finalized: event_id={event_id}, status={status}")

        except Exception as e:
            logger.error(f"Failed to finalize audit: {str(e)}", exc_info=True)
            if session:
                try:
                    await session.rollback()
                except:
                    pass
        finally:
            # Always clear the session variable and close the session
            if session:
                try:
                    # Clear the PostgreSQL session variable to prevent stale values on connection reuse
                    await session.execute(
                        text("SELECT set_config('app.audit_event_id', NULL, false)")
                    )
                except Exception as e:
                    logger.debug(f"Failed to clear audit session variable: {str(e)}")
                try:
                    await session.close()
                except:
                    pass

    def _extract_resource_code_from_response(self, response_payload: Optional[dict]) -> Optional[str]:
        """
        Extract resource code from response payload.

        This method looks for common patterns in API responses to find the resource identifier (code).
        Since all tables have a 'code' column (VARCHAR 100) that serves as the business identifier,
        we extract this from the response rather than parsing URLs.

        Common response patterns:
        1. Direct code field: {"code": "resource_code_123"}
        2. Nested in resource object: {"override": {"code": "policy_code_01"}}
        3. Application creation: {"code": "uuid-string", ...}
        4. Bulk operations: No single resource, returns None

        Args:
            response_payload: Parsed response body as dictionary

        Returns:
            Resource code (string) or None if not found or bulk operation

        Examples:
            {"code": "app_123"} → "app_123"
            {"override": {"code": "policy_01"}} → "policy_01"
            {"policies": [...]} → None (bulk operation)
        """
        if not response_payload or not isinstance(response_payload, dict):
            return None

        # Pattern 1: Direct 'code' field at root level
        if "code" in response_payload:
            return response_payload["code"]

        # Pattern 2: Look for common nested object patterns
        # These are single-resource responses where the resource is nested
        nested_keys = ["override", "application", "service", "policy", "resource_group", "infrastructure"]
        for key in nested_keys:
            if key in response_payload and isinstance(response_payload[key], dict):
                nested_obj = response_payload[key]
                if "code" in nested_obj:
                    return nested_obj["code"]

        # Pattern 3: Some endpoints return {resource}_code directly
        # e.g., "override_code", "application_code", "service_code"
        for key in response_payload.keys():
            if key.endswith("_code") and isinstance(response_payload[key], str):
                return response_payload[key]

        # Pattern 4: Bulk/list operations (contains arrays)
        # These don't have a single resource_id, so return None
        # e.g., {"applications": [...], "total": 10}
        # e.g., {"policies": [...], "skip": 0, "limit": 100}

        return None

    def _parse_response_body(self, body_bytes: bytes) -> Optional[dict]:
        """Parse response body from bytes with size limiting"""
        if not body_bytes:
            return None

        # Limit response body size to 50KB to avoid storing huge responses
        MAX_RESPONSE_SIZE = 50 * 1024  # 50KB

        try:
            # If response is too large, truncate and add indicator
            if len(body_bytes) > MAX_RESPONSE_SIZE:
                payload = {
                    "_truncated": True,
                    "_original_size_bytes": len(body_bytes),
                    "_message": "Response too large to log completely"
                }
                return payload

            # Try to parse as JSON
            payload = json.loads(body_bytes)
            return payload
        except Exception:
            # If not JSON, return None
            return None


def get_audit_context() -> dict:
    """
    Helper function to get current audit context.

    Returns:
        dict: Contains ip_address, user_agent, request_id, request_method, request_path

    Usage:
        from app.middleware.audit_middleware import get_audit_context

        context = get_audit_context()
    """
    return {
        "ip_address": audit_context_ip.get(),
        "user_agent": audit_context_user_agent.get(),
        "request_id": audit_context_request_id.get(),
        "request_method": audit_context_request_method.get(),
        "request_path": audit_context_request_path.get(),
    }
