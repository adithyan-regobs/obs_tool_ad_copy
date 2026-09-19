"""
GitHub App Authentication Provider

Provides GitHub App authentication for all GitHub operations.
Supports multiple installations (one per tenant GitHub org) with
per-installation token caching.

Installation tokens are cached until near expiry for efficiency.
"""

import jwt
import time
import httpx
import logging
from typing import Optional, Dict, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# Default timeout for GitHub API calls (in seconds)
DEFAULT_TIMEOUT = 300.0


class GitHubAppAuth:
    """
    GitHub App authentication provider.

    Generates JWT tokens and exchanges them for installation access tokens.
    Supports multiple installations with per-installation token caching.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        base_url: str = "https://api.github.com"
    ):
        """
        Initialize GitHub App auth provider.

        Args:
            app_id: GitHub App ID
            private_key: RSA private key (PEM format)
            base_url: GitHub API base URL
        """
        self.app_id = app_id
        self.private_key = private_key
        self.base_url = base_url.rstrip('/')
        if private_key:
            logger.info(f"GitHubAppAuth initialized: app_id={app_id}, key_length={len(private_key)}, key_starts={private_key[:27]}")
        else:
            logger.error("GitHubAppAuth initialized with EMPTY private key!")
        # Per-installation token cache: {installation_id: (token, expires_at_timestamp)}
        self._tokens: Dict[str, Tuple[str, float]] = {}

    def _generate_jwt(self) -> str:
        """
        Generate JWT for GitHub App authentication.

        Returns:
            JWT token string
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Issued 60 seconds ago (clock skew tolerance)
            "exp": now + (10 * 60),  # Expires in 10 minutes (max allowed)
            "iss": self.app_id
        }
        try:
            return jwt.encode(payload, self.private_key, algorithm="RS256")
        except Exception as e:
            # Log key diagnostics for debugging
            key = self.private_key
            has_begin = "-----BEGIN" in key if key else False
            has_end = "-----END" in key if key else False
            newline_count = key.count("\n") if key else 0
            logger.error(
                f"JWT encode failed: {e} | "
                f"app_id={self.app_id}, key_length={len(key) if key else 0}, "
                f"has_begin={has_begin}, has_end={has_end}, newlines={newline_count}, "
                f"key_repr={repr(key[:80])}..." if key else "key=EMPTY"
            )
            raise

    async def get_installation_token(self, installation_id: str, retry_on_401: bool = True) -> str:
        """
        Get installation access token (cached per installation until near expiry).

        Args:
            installation_id: GitHub App installation ID for the target org
            retry_on_401: If True, retry once on 401 error (JWT may have expired)

        Returns:
            Installation access token

        Raises:
            httpx.HTTPStatusError: If token exchange fails after retry
        """
        # Return cached token if still valid (with 5 min buffer)
        cached = self._tokens.get(installation_id)
        if cached and time.time() < (cached[1] - 300):
            logger.debug(f"Using cached GitHub App token for installation {installation_id}")
            return cached[0]

        logger.info(f"Generating new GitHub App token for installation {installation_id}")

        app_jwt = self._generate_jwt()

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
            )

            # Handle 401 with retry (JWT may have just expired)
            if response.status_code == 401 and retry_on_401:
                logger.warning("Got 401 on token exchange, regenerating JWT and retrying...")
                self._tokens.pop(installation_id, None)
                app_jwt = self._generate_jwt()

                response = await client.post(
                    f"{self.base_url}/app/installations/{installation_id}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"
                    }
                )

        response.raise_for_status()

        data = response.json()
        token = data["token"]

        # Parse expiry: "2024-01-01T00:00:00Z"
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        self._tokens[installation_id] = (token, expires_at.timestamp())

        logger.info(f"GitHub App token obtained for installation {installation_id}, expires at {data['expires_at']}")

        return token

    def clear_cache(self, installation_id: Optional[str] = None) -> None:
        """Clear cached token(s).

        Args:
            installation_id: Clear token for a specific installation.
                             If None, clears all cached tokens.
        """
        if installation_id:
            self._tokens.pop(installation_id, None)
        else:
            self._tokens.clear()
