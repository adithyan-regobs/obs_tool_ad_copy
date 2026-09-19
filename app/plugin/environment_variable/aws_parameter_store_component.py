"""
AWS SSM Parameter Store environment-variable component.

Per-key storage model: one SSM parameter per variable key. ``identifier`` is
the full parameter path of that key (``get_map`` treats it as a path prefix
and returns all parameters underneath, keyed by leaf name).
"""

import logging
from typing import Dict, Optional, Tuple

from app.core.enum import SecretProviderEnum
from app.integrations.aws_integration import AWSIntegration
from app.plugin.environment_variable.base_environment_variable_component import (
    BaseEnvironmentVariableComponent,
)

logger = logging.getLogger(__name__)


class AwsParameterStoreComponent(BaseEnvironmentVariableComponent):

    provider = SecretProviderEnum.AWS_SSM
    # SSM parameters hold plain config values, not secrets.
    stores_secrets = False

    async def get_value(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> Optional[str]:
        # One parameter per key — `identifier` already addresses the key,
        # so `key` is not needed to narrow the lookup.
        try:
            param = await AWSIntegration.get_parameter(
                auth_config=auth_config,
                parameter_name=identifier,
            )
        except Exception as exc:
            logger.info("No parameter at '%s' (%s)", identifier, exc)
            return None
        return param.get("value")

    async def get_map(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        try:
            params = await AWSIntegration.get_parameters_by_path(
                auth_config=auth_config,
                path=identifier,
            )
        except Exception as exc:
            logger.info("No parameters under '%s' (%s)", identifier, exc)
            return None, None
        values = {
            (p.get("name") or "").rsplit("/", 1)[-1]: p.get("value")
            for p in params
            if p.get("name")
        }
        return values, identifier

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
        # drop_key is a consolidated-store concept; a renamed parameter's old
        # path is removed by the caller via delete_key on its old identifier.
        await AWSIntegration.put_parameter(
            auth_config=auth_config,
            parameter_name=identifier,
            parameter_value=value,
            parameter_type="String",
            description=description,
            overwrite=True,
        )
        return identifier

    async def delete_key(
        self,
        auth_config: dict,
        identifier: str,
        key: Optional[str] = None,
    ) -> None:
        await AWSIntegration.delete_parameter(auth_config, identifier)

    async def list_store_keys(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Optional[Dict[str, str]]:
        # `identifier` is one key's full parameter path; its parent is the
        # resource's config prefix. Enumerate every parameter under that prefix
        # and map each leaf name to its OWN full path (one parameter per key).
        # None (prefix unreadable) is preserved so callers don't treat a failed
        # read as an empty store; {} means the prefix exists but holds no keys.
        prefix = identifier.rsplit("/", 1)[0] if "/" in identifier else identifier
        key_map, _ = await self.get_map(auth_config, prefix)
        if key_map is None:
            return None
        return {leaf: f"{prefix}/{leaf}" for leaf in key_map}

    def enumeration_scope(self, identifier: str) -> str:
        # Every key under a config prefix is discovered by ONE list of that
        # prefix, so each per-key path collapses to its parent prefix.
        return identifier.rsplit("/", 1)[0] if "/" in identifier else identifier

    async def list_store_entries(
        self,
        auth_config: dict,
        identifier: str,
    ) -> Optional[Dict[str, Tuple[str, Optional[str]]]]:
        # Enumerate the parent prefix once; map each leaf to (its full path, value).
        prefix = identifier.rsplit("/", 1)[0] if "/" in identifier else identifier
        key_map, _ = await self.get_map(auth_config, prefix)
        if key_map is None:
            return None
        return {leaf: (f"{prefix}/{leaf}", val) for leaf, val in key_map.items()}
