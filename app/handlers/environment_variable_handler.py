"""
Environment Variable Handler

Dispatch layer for environment-variable / secret storage operations.
Routes to provider-specific components (open/closed — new providers plug in
without touching callers):

- aws_secrets_manager → app/plugin/environment_variable/aws_secrets_manager_component.py
- aws_ssm             → app/plugin/environment_variable/aws_parameter_store_component.py
- (future) gcp_secret_manager, hashicorp_vault, azure_key_vault, ...

This handler does NOT contain business logic. It only resolves the component
for a (tenant, provider) pair — tenant-specific overrides first, then the
"default" map, mirroring the other handlers (file_location, script_gen,
post_action).

Components implement BaseEnvironmentVariableComponent:
  get_value / get_map / upsert_key / delete_key
"""

import logging
from typing import Dict, Type, Union

from app.core.enum import SecretProviderEnum
from app.plugin.environment_variable.base_environment_variable_component import (
    BaseEnvironmentVariableComponent,
)

logger = logging.getLogger(__name__)


class EnvironmentVariableHandler:

    @classmethod
    def _build_map(cls) -> Dict[str, Dict[str, Type]]:
        from app.plugin.environment_variable import (
            AwsParameterStoreComponent,
            AwsSecretsManagerComponent,
        )

        return {
            "default": {
                SecretProviderEnum.AWS_SECRETS_MANAGER.value: AwsSecretsManagerComponent,
                SecretProviderEnum.AWS_SSM.value: AwsParameterStoreComponent,
                # Future providers register here, e.g.:
                # SecretProviderEnum.GCP_SECRET_MANAGER.value: GcpSecretManagerComponent,
                # SecretProviderEnum.HASHICORP_VAULT.value: HashicorpVaultComponent,
                # SecretProviderEnum.AZURE_KEY_VAULT.value: AzureKeyVaultComponent,
            },
        }

    @classmethod
    def get_component(
        cls,
        provider: Union[SecretProviderEnum, str],
        tenant: str = "default",
    ) -> BaseEnvironmentVariableComponent:
        """
        Resolve the storage component for a provider.

        Lookup order: tenant-specific map → "default" map.
        Raises ValueError for providers with no registered component
        (e.g. LOCAL, which has no external store).
        """
        provider_value = (
            provider.value if isinstance(provider, SecretProviderEnum) else str(provider)
        )
        provider_map = cls._build_map()
        component_cls = (
            provider_map.get(tenant, {}).get(provider_value)
            or provider_map.get("default", {}).get(provider_value)
        )
        if component_cls is None:
            raise ValueError(
                f"No environment-variable component registered for provider "
                f"'{provider_value}' (tenant '{tenant}'). "
                f"Registered: {sorted(provider_map.get('default', {}))}"
            )
        return component_cls()
