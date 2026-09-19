"""Deployment schemas — /deployments/multiple-deploy (DeploymentOrchestratorWorkflow)."""

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class MultipleDeployVariablesPart(BaseModel):
    """Same shape as POST /project-variables/deploy."""
    transaction_code: str = Field(..., description="Code of the owning resource (service_config code)")
    table_name: Optional[str] = Field(
        default="service_config", description="Owning table, e.g. 'service_config'"
    )
    environment: Optional[str] = Field(
        default=None, description="Optional guard; must match the service_config environment when given"
    )


class MultipleDeployInfraPart(BaseModel):
    """Infra scope for the deploy. Environment/geo/application scoping is
    inherent to the queue items themselves (each row points at one env-specific
    source row), so there is no environment filter: pass item_ids, or omit them
    to deploy ALL of the caller's approved queue items."""
    item_ids: Optional[List[int]] = Field(
        default=None, description="Only deploy these specific (approved) queue item IDs"
    )


class MultipleDeployGatewayPart(BaseModel):
    """Deploy the service's APPROVED gateway change, resolved server-side by
    transaction_code — no author filter, so any can_deploy user ships the
    author's approved routes (parity with variables). The lane lock guarantees
    one approved gateway row per service+env, so the code alone is unambiguous;
    the caller sends the code, not the queue-id (which only its author has)."""
    transaction_code: str = Field(..., description="Owning service_config code")


class MultipleDeployRequest(BaseModel):
    """Combined payload: variables, infra and/or gateway. At least one is required."""
    variables: Optional[MultipleDeployVariablesPart] = None
    infra: Optional[MultipleDeployInfraPart] = None
    gateway: Optional[MultipleDeployGatewayPart] = None
    service_config_code: Optional[str] = Field(
        default=None,
        description=(
            "Owning service_config code, used ONLY to anchor the batch's "
            "run-track history under that service's page when the batch has "
            "no service_config queue item of its own (e.g. a kong-route-only "
            "or kong+s3 batch with no variables deploy). Ignored whenever a "
            "service_config item is present in infra.item_ids, or whenever "
            "'variables' is set (variables.transaction_code wins as the "
            "anchor in that case)."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_part(self):
        if self.variables is None and self.infra is None and self.gateway is None:
            raise ValueError("Provide at least one of 'variables', 'infra' or 'gateway'")
        return self


class MultipleDeployResponse(BaseModel):
    workflow_id: str = Field(
        ...,
        description="DeploymentOrchestratorWorkflow id — poll /deployments/multiple-deploy/status/{workflow_id}",
    )
    status: str = "started"
