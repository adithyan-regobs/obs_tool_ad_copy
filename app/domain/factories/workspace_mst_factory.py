from uuid import uuid4
from typing import Any, Dict, Optional

from app.core.enum import WorkspaceRoleEnum, WorkspaceStatusEnum


def make_workspace(
    tenant_code: str,
    workspace_name: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build dict for workspace_mst.create()."""
    return {
        "code": str(uuid4()),
        "name": workspace_name,
        "description": description,
        "tenants_mst_code": tenant_code,
        "status": WorkspaceStatusEnum.active,
        "is_deleted": False,
        "is_active": True,
    }


def make_workspace_user_mapping(
    workspace_code: str,
    workspace_name: str,
    user_mst_code: str,
    user_email: str,
    tenant_code: str,
    role: WorkspaceRoleEnum,
) -> Dict[str, Any]:
    """Build dict for workspace_user_mapping.create()."""
    return {
        "code": str(uuid4()),
        "name": f"{workspace_name} :: {user_email}",
        "description": None,
        "workspace_code": workspace_code,
        "user_mst_code": user_mst_code,
        "tenants_mst_code": tenant_code,
        "role": role,
        "is_deleted": False,
        "is_active": True,
    }
