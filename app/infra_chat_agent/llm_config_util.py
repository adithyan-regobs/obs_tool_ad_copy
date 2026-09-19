from typing import Optional
from dataclasses import dataclass


@dataclass
class LLMConfig:
    """LLM configuration for a tenant/intent combination."""
    llm_model: str  # Model name (e.g., "gpt-4o-mini", "gpt-4o")


@dataclass
class TenantLLMConfig:
    """LLM configuration for a tenant."""
    tenant_id: str
    intent_llm_config: dict[str, LLMConfig]  # intent -> LLMConfig mapping
    default_llm_config: Optional[LLMConfig] = None


class LLMConfigUtil:
    """Utility to decide which LLM model to use based on tenant and intent."""

    # Tenant-specific LLM configurations
    _tenant_configs: dict[str, TenantLLMConfig] = {}

    @classmethod
    def register_tenant_config(cls, config: TenantLLMConfig) -> None:
        """Register LLM configuration for a tenant."""
        cls._tenant_configs[config.tenant_id] = config

    @classmethod
    def get_llm_model(
        cls,
        tenant_id: str,
        intent: str,
        default_model: str = "gpt-4o-mini"
    ) -> str:
        """
        Get the LLM model to use for a given tenant and intent.

        Args:
            tenant_id: Tenant identifier
            intent: Intent type (e.g., "CREATE", "REFERENCE", "QA")
            default_model: Default model if no config found

        Returns:
            Model name to use
        """
        tenant_config = cls._tenant_configs.get(tenant_id)

        if not tenant_config:
            return default_model

        # Check for intent-specific config
        intent_config = tenant_config.intent_llm_config.get(intent)
        if intent_config:
            return intent_config.llm_model

        # Fall back to tenant default
        if tenant_config.default_llm_config:
            return tenant_config.default_llm_config.llm_model

        return default_model

    @classmethod
    def initialize_default_configs(cls) -> None:
        """Initialize default LLM configurations for tenants."""
        # Default tenant config
        cls.register_tenant_config(
            TenantLLMConfig(
                tenant_id="default",
                intent_llm_config={
                    "CREATE": LLMConfig(llm_model="gpt-4o"),  # gpt-4o for better parameter extraction
                    "REFERENCE": LLMConfig(llm_model="gpt-4o-mini"),
                    "QA": LLMConfig(llm_model="gpt-4o"),
                    "UNSUPPORTED": LLMConfig(llm_model="gpt-4o-mini"),
                },
                default_llm_config=LLMConfig(llm_model="gpt-4o-mini")
            )
        )

        # Fintech-prod tenant config
        cls.register_tenant_config(
            TenantLLMConfig(
                tenant_id="fintech-prod",
                intent_llm_config={
                    "CREATE": LLMConfig(llm_model="gpt-4o"),
                    "REFERENCE": LLMConfig(llm_model="gpt-4o-mini"),
                    "QA": LLMConfig(llm_model="gpt-4o"),
                    "UNSUPPORTED": LLMConfig(llm_model="gpt-4o-mini"),
                },
                default_llm_config=LLMConfig(llm_model="gpt-4o-mini")
            )
        )


# Initialize default configs on import
LLMConfigUtil.initialize_default_configs()
