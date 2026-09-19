"""
Schemas for the env-variable clone listing flow (pull model: the user sits in
the TARGET service and picks a SOURCE to clone from).

Three endpoints share these models:
  - POST /resource-variable/clone/source-services  (paginated picker + preloaded recommendation)
  - GET  /resource-variable/clone/source-options   (env/region options for a selected source service)
  - GET  /resource-variable/clone/source-variables (deployed variables of a source combo)
"""

from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.enum import EnvironmentEnum


class CloneSourceServiceItem(BaseModel):
    service_code: str
    service_name: str
    application_code: str
    application_name: str
    has_source_configs: bool = Field(
        ...,
        description="False when the service has no service_config to clone from (not selectable)",
    )


class CloneRegionOption(BaseModel):
    geo_loc_code: str
    geo_loc_name: str
    cloud_region: Optional[str] = Field(
        default=None,
        description="Concrete cloud region (e.g. ap-south-1) when known from the deploy config",
    )
    config_code: Optional[str] = Field(
        default=None,
        description="service_config code of this env/region/cluster combo — the clone source_transaction_code",
    )
    cluster_code: Optional[str] = Field(
        default=None,
        description="infrastructure_mst code of the cluster this config lives on",
    )
    cluster_name: Optional[str] = Field(
        default=None,
        description="Friendly cluster name (shown when a region holds more than one config)",
    )
    suggested: bool = False
    auto_select: bool = False


class CloneEnvironmentOption(BaseModel):
    environment: EnvironmentEnum
    suggested: bool = False
    auto_select: bool = False
    regions: List[CloneRegionOption] = Field(default_factory=list)


class CloneRecommendation(BaseModel):
    service: CloneSourceServiceItem
    environment: Optional[EnvironmentEnum] = None
    geo_loc_code: Optional[str] = None
    environments: List[CloneEnvironmentOption] = Field(
        default_factory=list,
        description="Full env/region options for the recommended service — saves a follow-up call",
    )


class CloneSourceServicesRequest(BaseModel):
    target_service_code: str = Field(..., description="Service the variables are cloned INTO")
    target_environment: EnvironmentEnum
    target_geo_loc_code: Optional[str] = None
    search_query: Optional[str] = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    include_recommendation: bool = Field(
        default=True,
        description="Compute the default same-service selection (set false for search/pagination calls)",
    )
    restrict_infra_type_ref_code: Optional[str] = Field(
        default=None,
        description=(
            "When set (settings clone), only source services/combos whose "
            "infrastructuretype_ref_code matches are returned. Omitted for the "
            "variable clone, which is infra-type agnostic."
        ),
    )


class CloneSourceServicesResponse(BaseModel):
    total: int
    skip: int
    limit: int
    services: List[CloneSourceServiceItem] = Field(default_factory=list)
    recommendation: Optional[CloneRecommendation] = None


class CloneSourceOptionsResponse(BaseModel):
    service_code: str
    environments: List[CloneEnvironmentOption] = Field(default_factory=list)


class CloneSourceVariableItem(BaseModel):
    key: str
    secret: bool = Field(..., description="True for Secrets Manager entries, False for SSM variables")
    value: Optional[str] = Field(
        default=None,
        description="Live AWS value for plain variables; null for secrets (masked in the picker)",
    )


class CloneSourceVariablesResponse(BaseModel):
    config_code: str = Field(..., description="service_config code the variables belong to")
    variables: List[CloneSourceVariableItem] = Field(
        default_factory=list,
        description="Deployed variables only — un-deployed drafts are private and not clonable",
    )
