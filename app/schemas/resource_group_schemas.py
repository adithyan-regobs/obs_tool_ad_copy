"""Schemas for resource group creation.

A resource group is the unit roles are granted on: `resource_group.admin` /
`approver` in the authorization model, inherited by every service parented
under it. Creating one here writes the app_db row only — the group becomes
visible to OpenFGA the moment any tuple names it (a role grant made in Access
Control, or the parent tuple written when the first service is assigned to it).
"""

from typing import Optional

from pydantic import BaseModel, Field


class CreateResourceGroupRequest(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Display name. Unique within the application.",
    )
    application_code: str = Field(
        ..., description="applications_mst.code the group belongs to"
    )
    description: Optional[str] = Field(
        default=None, max_length=500, description="Optional description"
    )


class ResourceGroupCreateResponse(BaseModel):
    code: str
    name: str
    kind: str
    description: Optional[str] = None
    applications_mst_code: str
    tenants_mst_code: str
    is_active: bool
    message: str
