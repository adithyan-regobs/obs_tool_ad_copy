"""
Clerk JWT Token Validation Service

This module handles JWT token validation for Clerk authentication.
It validates tokens using Clerk's JWKS (JSON Web Key Set) and extracts
user and tenant information from the token claims.
"""
import time
import logging
from typing import Dict, Optional, Tuple
from jwt import decode, PyJWKClient, InvalidTokenError

from app.core.config import settings, is_origin_allowed

logger = logging.getLogger(__name__)


class ClerkJWTValidator:
    """Validates Clerk JWT tokens and extracts claims"""

    def __init__(self):
        """Initialize JWT validator with Clerk JWKS client"""
        self.jwk_client = PyJWKClient(settings.clerk_jwks_url)
        self.issuer = settings.clerk_issuer

    def validate_token(self, token: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Validate Clerk JWT token

        Args:
            token: The JWT token string to validate

        Returns:
            Tuple of (is_valid, payload_dict, error_message)
            - is_valid: True if token is valid
            - payload_dict: Token claims if valid, None otherwise
            - error_message: Error description if invalid, None otherwise

        Example:
            >>> validator = ClerkJWTValidator()
            >>> is_valid, payload, error = validator.validate_token(token)
            >>> if is_valid:
            ...     user_id = payload.get("userId")
            ...     subdomain = payload.get("organizationSubdomain")
        """
        try:
            # Get signing key from JWKS
            signing_key = self.jwk_client.get_signing_key_from_jwt(token)

            # Decode and validate token
            # Note: leeway adds tolerance for clock skew (useful for iat/exp validation)
            payload = decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=120,  # 2 minutes tolerance for clock skew
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                }
            )

            # Additional validation: check token expiry with grace period
            current_time = time.time()
            exp = payload.get("exp")

            if exp and exp < (current_time - 30):  # 30 second grace period
                logger.warning(f"Token expired: exp={exp}, current={current_time}")
                return False, None, "Token has expired"

            # Check for future tokens (clock skew tolerance)
            iat = payload.get("iat")
            if iat and iat > (current_time + 600):  # 10 minute tolerance
                logger.warning(f"Token from future: iat={iat}, current={current_time}")
                return False, None, "Token issued in the future"

            # Validate authorized party (azp) - ensures token is from allowed frontend origin
            azp = payload.get("azp")
            if azp and not is_origin_allowed(azp):
                logger.warning(f"Invalid azp (authorized party): {azp} not in allowed origins")
                return False, None, f"Invalid authorized party: {azp}"

            logger.debug(f"JWT token validated successfully for user: {payload.get('userId', 'unknown')}")
            return True, payload, None

        except InvalidTokenError as e:
            logger.error(f"JWT validation failed: {e}")
            return False, None, f"Invalid token: {str(e)}"
        except Exception as e:
            logger.error(f"JWT validation error: {e}")
            return False, None, f"Token validation failed: {str(e)}"

    def extract_user_claims(self, payload: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract user_id and organization_subdomain from JWT payload

        Args:
            payload: Decoded JWT payload dictionary

        Returns:
            Tuple of (user_id, organization_subdomain)
            - user_id: Clerk user ID from "userId" claim
            - organization_subdomain: Organization subdomain from "organizationSubdomain" claim

        Example:
            >>> user_id, subdomain = validator.extract_user_claims(payload)
            >>> print(f"User: {user_id}, Tenant: {subdomain}")
        """

        # Try multiple claim names for user ID (different JWT templates use different names)
        user_id = payload.get("userId") or payload.get("sub")
        organization_subdomain = payload.get("organizationSubdomain")

        if not user_id:
            logger.warning("JWT payload missing 'userId' and 'sub' claims")
            logger.warning(f"[JWT] Payload has these keys: {list(payload.keys())}")

        if not organization_subdomain:
            logger.warning("JWT payload missing 'organizationSubdomain' claim")
            logger.info("[JWT] This is expected during signup (before org is created)")

        logger.debug(f"[JWT] Extracted user_id: {user_id}")
        logger.debug(f"[JWT] Extracted organization_subdomain: {organization_subdomain}")

        return user_id, organization_subdomain

    def validate_internal_token(self, token: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Validate a DevLift-internal HS256 JWT minted by the MCP server.

        Used for service-to-service calls from chat-bot-POC back into obs_tool's
        APIs. The MCP server mints these from its AuthContext (user_code,
        tenant_code, user_email) and the chatbot relays them on outgoing
        validate / dropdown calls.

        Returns the same (is_valid, payload, error) tuple shape as
        validate_token so callers can treat both the same way.
        """
        secret = settings.mcp_internal_jwt_secret
        if not secret:
            return False, None, "Internal JWT secret not configured"
        try:
            payload = decode(
                token,
                secret,
                algorithms=["HS256"],
                issuer="devlift-mcp",
                leeway=30,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                },
            )
        except InvalidTokenError as e:
            return False, None, f"Invalid internal token: {e}"

        if payload.get("type") != "mcp_internal":
            return False, None, "Token is not an MCP internal token"
        if not payload.get("user_code") or not payload.get("tenant_code"):
            return False, None, "Internal token missing user_code/tenant_code claims"
        return True, payload, None

    def extract_azp(self, payload: Dict) -> Optional[str]:
        """
        Extract azp (authorized party) from JWT payload

        The azp claim contains the origin URL of the request
        (e.g., "https://vance.devlift.ai", "https://devlift.ai")

        Args:
            payload: Decoded JWT payload dictionary

        Returns:
            str: The authorized party URL (e.g., "https://devlift.ai")

        Example:
            >>> azp = validator.extract_azp(payload)
            >>> print(f"Request origin: {azp}")
        """
        azp = payload.get("azp")

        if not azp:
            logger.warning("JWT payload missing 'azp' claim")
        else:
            logger.info(f"[JWT] Extracted azp (authorized party): {azp}")

        return azp


# Singleton instance
clerk_jwt_validator = ClerkJWTValidator()
