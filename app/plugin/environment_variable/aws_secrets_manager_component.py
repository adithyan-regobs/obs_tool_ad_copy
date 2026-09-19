"""
AWS Secrets Manager environment-variable component.

Consolidated storage model: ONE Secrets Manager secret per service, holding
all its secret keys as a JSON key/value map. ``identifier`` is the secret's
name (path) or ARN.
"""

import json
import logging
from typing import Dict, Optional, Tuple

from app.core.enum import SecretProviderEnum
from app.integrations.aws_integration import AWSIntegration
from app.plugin.environment_variable.base_environment_variable_component import (
    BaseEnvironmentVariableComponent,
)

logger = logging.getLogger(__name__)


class AwsSecretsManagerComponent(BaseEnvironmentVariableComponent):

    provider = SecretProviderEnum.AWS_SECRETS_MANAGER

    async def get_value(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> Optional[str]:
        try:
            secret = await AWSIntegration.get_secret(
                auth_config=auth_config,
                secret_name=identifier,
            )
        except Exception as exc:
            logger.info("No secret at '%s' (%s)", identifier, exc)
            return None
        value = secret.get("value")
        if key is not None:
            if not isinstance(value, dict):
                # Same legacy plain-string wrapping as get_map, so key lookups
                # behave identically to the consolidated-map read
                value = {"value": value} if value is not None else {}
            return value.get(key)
        return json.dumps(value) if isinstance(value, dict) else value

    async def get_map(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        try:
            secret = await AWSIntegration.get_secret(
                auth_config=auth_config,
                secret_name=identifier,
            )
        except Exception as exc:
            logger.info("No secret at '%s' yet (%s)", identifier, exc)
            return None, None
        value = secret.get("value")
        if not isinstance(value, dict):
            # Legacy/plain-string secret — wrap so a merge keeps it visible
            value = {"value": value} if value is not None else {}
        return value, secret.get("arn")

    async def upsert_key(
        self,
        auth_config: dict,
        identifier: str,
        key: str,
        value: str,
        *,
        drop_key: Optional[str] = None,
        description: Optional[str] = None,
        create_identifier: Optional[str] = None,
    ) -> str:
        current, arn = await self.get_map(auth_config, identifier)
        merged = dict(current or {})
        if drop_key:
            # Rename: the old key is dropped and the new one set in ONE write
            merged.pop(drop_key, None)
        merged[key] = value
        if arn:
            response = await AWSIntegration.update_secret(
                auth_config=auth_config,
                secret_name=arn,
                secret_value=merged,
            )
        else:
            response = await AWSIntegration.create_secret(
                auth_config=auth_config,
                secret_name=create_identifier or identifier,
                secret_value=merged,
                description=description,
            )
        return response.get("arn") or create_identifier or identifier

    async def delete_key(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> None:
        if key is None:
            logger.warning(
                "Whole-secret deletion is not supported for '%s' — skipped", identifier
            )
            return
        current, arn = await self.get_map(auth_config, identifier)
        if not current or key not in current:
            return
        current.pop(key, None)
        await AWSIntegration.update_secret(
            auth_config=auth_config,
            secret_name=arn or identifier,
            secret_value=current,
        )
