"""
External client auth code service
"""
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
from typing import Optional, Dict, Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.clerk_jwt import clerk_jwt_validator
from app.core.config import settings
from app.repository.ext_auth_code_repository import ExtAuthCodeRepository
from app.repository.user_mst_repository import UserMstRepository
from app.repository.tenants_mst_repository import TenantsMstRepository

logger = logging.getLogger(__name__)


class ExtAuthService:
    """Service for external client browser login auth codes (VSCode, MCP, etc.)."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.auth_code_repo = ExtAuthCodeRepository(session)
        self.user_repo = UserMstRepository(session)
        self.tenant_repo = TenantsMstRepository(session)

    def _hash_code(self, code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    def _validate_client_id(self, client_id: str) -> None:
        allowed = settings.ext_auth_allowed_client_ids_list
        if not allowed:
            logger.error("EXT_AUTH_ALLOWED_CLIENT_IDS is not configured")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="External client allowlist is not configured"
            )
        if client_id not in allowed:
            logger.warning(f"Unauthorized client_id: {client_id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid client_id"
            )

    async def create_auth_code(
        self,
        user,
        tenant,
        client_id: str,
        state: Optional[str],
        clerk_jwt: str
    ) -> str:
        self._validate_client_id(client_id)

        # Validate Clerk JWT
        is_valid, payload, error = clerk_jwt_validator.validate_token(clerk_jwt)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid authentication token: {error}"
            )

        exp = payload.get("exp") if payload else None
        if not exp:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing expiration"
            )

        now = datetime.now(timezone.utc)
        token_expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
        if token_expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication token has expired"
            )

        ttl_seconds = settings.ext_auth_code_ttl_seconds
        if ttl_seconds <= 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth code TTL is not configured"
            )

        code = secrets.token_urlsafe(32)
        code_hash = self._hash_code(code)
        code_prefix = code[:8]
        expires_at = now + timedelta(seconds=ttl_seconds)

        await self.auth_code_repo.create_auth_code(
            code_hash=code_hash,
            code_prefix=code_prefix,
            client_id=client_id,
            user_mst_code=user.code,
            tenants_mst_code=tenant.code,
            clerk_jwt=clerk_jwt,
            token_expires_at=token_expires_at,
            state=state,
            expires_at=expires_at
        )

        # Best-effort cleanup
        await self.auth_code_repo.cleanup_expired(now, settings.ext_auth_code_cleanup_days)

        return code

    async def exchange_code(
        self,
        code: str,
        client_id: str,
        state: Optional[str]
    ) -> Dict[str, Any]:
        self._validate_client_id(client_id)

        if not code or not code.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code is required"
            )

        now = datetime.now(timezone.utc)
        code_hash = self._hash_code(code.strip())

        auth_code = await self.auth_code_repo.get_valid_by_hash(code_hash, client_id, now)
        if not auth_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired authorization code"
            )

        if auth_code.state is not None:
            if not state or state != auth_code.state:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid state parameter"
                )

        if auth_code.token_expires_at and auth_code.token_expires_at <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Stored authentication token has expired"
            )

        await self.auth_code_repo.mark_used(auth_code, now)
        await self.auth_code_repo.cleanup_expired(now, settings.ext_auth_code_cleanup_days)

        user = await self.user_repo.get_by_code(auth_code.user_mst_code)
        tenant = await self.tenant_repo.get_by_code(auth_code.tenants_mst_code)

        if not user or not tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User or tenant not found"
            )

        expires_in = 0
        if auth_code.token_expires_at:
            expires_in = max(0, int((auth_code.token_expires_at - now).total_seconds()))

        return {
            "access_token": auth_code.clerk_jwt,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "user": {
                "user_id": str(user.id),
                "user_code": user.code,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email_id,
                "is_org_owner": user.is_org_owner,
                "auth_provider": user.auth_provider.value if user.auth_provider else "manual"
            },
            "tenant": {
                "tenant_id": str(tenant.id),
                "tenant_code": tenant.code,
                "tenant_name": tenant.name,
                "tenant_subdomain": tenant.subdomain
            }
        }
