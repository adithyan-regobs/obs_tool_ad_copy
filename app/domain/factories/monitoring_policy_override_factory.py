"""
Factory for creating MonitoringPolicyOverride domain objects
"""

from uuid import uuid4
from typing import Dict, Any
from app.schemas.monitoring_policy_override_schemas import CreateMonitoringPolicyOverrideRequest


def make_monitoring_policy_override(
    request: CreateMonitoringPolicyOverrideRequest,
    monitoring_policy_defaults_ref_code: str,
    tenant_code: str
) -> Dict[str, Any]:
    """
    Factory function to build a MonitoringPolicyOverride domain object.

    Args:
        request: Create override request from API
        monitoring_policy_defaults_ref_code: Code of the default policy (found by service)
        tenant_code: Tenant code from JWT authentication (not from request)

    Returns:
        Dictionary containing all fields needed to create the override model
    """
    # Generate unique code
    override_code = f"POL_OVR_{uuid4().hex[:8].upper()}"

    return {
        "code": override_code,
        "name": request.name,
        "description": request.description,
        "monitoring_policy_defaults_ref_code": monitoring_policy_defaults_ref_code,
        "alerttype_ref_code": request.alerttype_ref_code,
        "tenants_mst_code": tenant_code,  # From JWT authentication, not from request
        "applications_mst_code": request.applications_mst_code,
        "resource_group_mst_code": request.resource_group_mst_code,
        "infrastructuretype_ref_code": request.infrastructuretype_ref_code,
        "comparator": request.comparator,
        "threshold_value": request.threshold_value,
        "threshold_unit": request.threshold_unit,
        "eval_window": request.eval_window,
        "for_duration": request.for_duration,
        "no_data": request.no_data,
        "severity": request.severity,
        "is_active": request.is_active
    }