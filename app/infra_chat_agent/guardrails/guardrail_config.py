from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ContentModerationResult(BaseModel):
    is_safe: bool = Field(description="Whether the content is safe")
    violation_category: Literal["none", "hate_speech", "violence", "harassment", "self_harm", "sexual", "other"] = Field(
        description="Category of violation if any"
    )
    confidence: float = Field(description="Confidence score 0-1")
    explanation: str = Field(description="Brief explanation")


class PromptInjectionResult(BaseModel):
    is_injection: bool = Field(description="Whether the input attempts prompt injection")
    injection_type: Literal["none", "instruction_override", "jailbreak", "roleplay_manipulation", "other"] = Field(
        description="Type of injection attempt if any"
    )
    confidence: float = Field(description="Confidence score 0-1")
    explanation: str = Field(description="Brief explanation")


class PIIResult(BaseModel):
    contains_pii: bool = Field(description="Whether PII is present")
    pii_types: List[str] = Field(description="List of detected PII types")
    redacted_text: str = Field(description="Input text with PII redacted")
    confidence: float = Field(description="Confidence score 0-1")
    explanation: str = Field(description="Brief explanation")


@dataclass
class GuardrailConfig:
    """Guardrail configuration for a tenant."""
    llm_model: str = "gpt-4o-mini"
    block_threshold: float = 0.7
    content_moderation_enabled: bool = True
    prompt_injection_enabled: bool = True
    pii_detection_enabled: bool = True


@dataclass
class TenantGuardrailConfig:
    """Guardrail configuration for a tenant."""
    tenant_id: str
    config: GuardrailConfig


class GuardrailConfigUtil:
    """Utility to decide guardrail settings based on tenant."""

    _tenant_configs: Dict[str, TenantGuardrailConfig] = {}

    @classmethod
    def register_tenant_config(cls, config: TenantGuardrailConfig) -> None:
        """Register guardrail configuration for a tenant."""
        cls._tenant_configs[config.tenant_id] = config

    @classmethod
    def get_config(
        cls,
        tenant_id: str,
        default: Optional[GuardrailConfig] = None
    ) -> GuardrailConfig:
        """Get guardrail config for a tenant (fallback to default)."""
        tenant_config = cls._tenant_configs.get(tenant_id)
        if tenant_config:
            return tenant_config.config
        return default or GuardrailConfig()

    @classmethod
    def initialize_default_configs(cls) -> None:
        """Initialize default guardrail configs."""
        cls.register_tenant_config(
            TenantGuardrailConfig(
                tenant_id="default",
                config=GuardrailConfig()
            )
        )


GuardrailConfigUtil.initialize_default_configs()
