"""
Infrastructure Transaction Factory

Factory functions to transform database rows to API response format.
Matches frontend TypeScript interfaces for transactions.
"""
from typing import Optional, Dict, Any
from datetime import datetime


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Format datetime to ISO string for JSON response."""
    if dt is None:
        return None
    return dt.isoformat()


def make_gitops_detail(row: Any) -> Optional[Dict[str, Any]]:
    """
    Transform gitops workflow row fields to GitopsWorkflowDetailResponse format.

    Args:
        row: Database row with gitops workflow fields (prefixed with workflow_)

    Returns:
        Dict matching GitopsWorkflowDetailResponse or None if no workflow linked
    """
    # Check if gitops workflow is linked
    workflow_id = getattr(row, 'workflow_id', None) or getattr(row, 'gitops_workflow_id', None)
    if not workflow_id:
        return None

    return {
        "id": getattr(row, 'workflow_id', None),
        "code": getattr(row, 'workflow_code', None),
        "name": getattr(row, 'workflow_name', None),
        "gitRepository": getattr(row, 'git_repository', None),
        "gitBranch": getattr(row, 'git_branch', None),
        "gitCommitSha": getattr(row, 'git_commit_sha', None),
        "prNumber": getattr(row, 'pr_number', None),
        "prUrl": getattr(row, 'pr_url', None),
        "workflowRunId": getattr(row, 'workflow_run_id', None),
        "workflowRunUrl": getattr(row, 'workflow_run_url', None),
        "workflowRunOutputs": getattr(row, 'workflow_run_outputs', None),
        "runInitiatedAt": _format_datetime(getattr(row, 'run_initiated_at', None)),
        "runCompletedAt": _format_datetime(getattr(row, 'run_completed_at', None)),
    }


def make_infrastructure_transaction(row: Any) -> Dict[str, Any]:
    """
    Transform infrastructure_mst row to InfrastructureTransactionResponse format.

    Args:
        row: Database row from infrastructure_mst with joined gitops_workflow_detail

    Returns:
        Dict matching InfrastructureTransactionResponse schema
    """
    # Get status value (may be an enum)
    status = getattr(row, 'infra_status', None)
    if status and hasattr(status, 'value'):
        status = status.value

    # Get environment value (may be an enum)
    environment = getattr(row, 'environments_enum', None)
    if environment and hasattr(environment, 'value'):
        environment = environment.value

    return {
        # Base fields
        "id": str(row.id),
        "type": "infrastructure",
        "code": row.code,
        "name": row.name,
        "createdAt": _format_datetime(row.created_at),
        "updatedAt": _format_datetime(getattr(row, 'updated_at', None)),
        "isActive": getattr(row, 'is_active', None),
        "isDeleted": getattr(row, 'is_deleted', None),
        "status": status,
        "statusUpdatedBy": getattr(row, 'infra_status_updated_by', None),
        "statusUpdatedAt": _format_datetime(getattr(row, 'infra_status_updated_at', None)),
        "resourceIdentifier": getattr(row, 'resource_identifier', None),
        "gitopsWorkflowId": getattr(row, 'gitops_workflow_id', None),
        "gitopsDetails": make_gitops_detail(row),
        # Infrastructure-specific fields
        "infrastructureTypeRefCode": row.infrastructuretype_ref_code,
        "infrastructureTypeName": getattr(row, 'infrastructure_type_name', None),
        "infraVendorAccountsCode": row.infra_vendor_accounts_mst_code,
        "resourceGroupCode": getattr(row, 'resource_group_mst_code', None),
        "resourceGroupName": getattr(row, 'resource_group_name', None),
        "tenantCode": row.tenants_mst_code,
        "applicationsCode": row.applications_mst_code,
        "applicationName": getattr(row, 'application_name', None),
        "environment": environment,
        "locator": getattr(row, 'locator', None),
        "description": getattr(row, 'description', None),
    }


def make_kong_route_transaction(row: Any) -> Dict[str, Any]:
    """
    Transform kong_route_configs row to KongRouteTransactionResponse format.

    Args:
        row: Database row from kong_route_configs with joined gitops_workflow_detail

    Returns:
        Dict matching KongRouteTransactionResponse schema
    """
    # Get status value (may be an enum)
    status = getattr(row, 'creation_status', None)
    if status and hasattr(status, 'value'):
        status = status.value

    return {
        # Base fields
        "id": str(row.id),
        "type": "kong-route",
        "code": row.code,
        "name": row.name,
        "createdAt": _format_datetime(row.created_at),
        "updatedAt": _format_datetime(getattr(row, 'updated_at', None)),
        "isActive": getattr(row, 'is_active', None),
        "isDeleted": getattr(row, 'is_deleted', None),
        "status": status,
        "statusUpdatedBy": getattr(row, 'creation_status_updated_by', None),
        "statusUpdatedAt": _format_datetime(getattr(row, 'creation_status_updated_at', None)),
        "resourceIdentifier": getattr(row, 'resource_identifier', None),
        "gitopsWorkflowId": getattr(row, 'gitops_workflow_id', None),
        "gitopsDetails": make_gitops_detail(row),
        # Kong route-specific fields
        "serviceCode": getattr(row, 'services_mst_code', None),
        "serviceName": getattr(row, 'service_name', None),
        "applicationName": getattr(row, 'application_name', None),
        "apiName": row.api_name,
        "httpMethod": row.http_method,
        "routePath": row.route_path,
        "creationError": getattr(row, 'creation_error', None),
    }


def make_alert_config_transaction(row: Any) -> Dict[str, Any]:
    """
    Transform alert_configs row to AlertConfigTransactionResponse format.

    Args:
        row: Database row from alert_configs with joined gitops_workflow_detail

    Returns:
        Dict matching AlertConfigTransactionResponse schema
    """
    # Get status value (may be an enum)
    status = getattr(row, 'creation_status', None)
    if status and hasattr(status, 'value'):
        status = status.value

    # Get signal_kind value (may be an enum)
    signal_kind = getattr(row, 'signal_kind', None)
    if signal_kind and hasattr(signal_kind, 'value'):
        signal_kind = signal_kind.value

    # Get comparator value (may be an enum)
    comparator = getattr(row, 'comparator', None)
    if comparator and hasattr(comparator, 'value'):
        comparator = comparator.value

    # Get threshold_unit value (may be an enum)
    threshold_unit = getattr(row, 'threshold_unit', None)
    if threshold_unit and hasattr(threshold_unit, 'value'):
        threshold_unit = threshold_unit.value

    # Get severity value (may be an enum)
    severity = getattr(row, 'severity', None)
    if severity and hasattr(severity, 'value'):
        severity = severity.value

    # Get vendor_status value (may be an enum)
    vendor_status = getattr(row, 'vendor_status', None)
    if vendor_status and hasattr(vendor_status, 'value'):
        vendor_status = vendor_status.value

    return {
        # Base fields
        "id": str(row.id),
        "type": "alert-config",
        "code": row.code,
        "name": row.name,
        "createdAt": _format_datetime(row.created_at),
        "updatedAt": _format_datetime(getattr(row, 'updated_at', None)),
        "isActive": getattr(row, 'is_active', None),
        "isDeleted": getattr(row, 'is_deleted', None),
        "status": status,
        "statusUpdatedBy": getattr(row, 'creation_status_updated_by', None),
        "statusUpdatedAt": _format_datetime(getattr(row, 'creation_status_updated_at', None)),
        "resourceIdentifier": getattr(row, 'resource_identifier', None),
        "gitopsWorkflowId": getattr(row, 'gitops_workflow_id', None),
        "gitopsDetails": make_gitops_detail(row),
        # Alert config-specific fields
        "obsVendorAccountsCode": row.obs_vendor_accounts_mst_code,
        "serviceCode": getattr(row, 'services_mst_code', None),
        "serviceName": getattr(row, 'service_name', None),
        "applicationName": getattr(row, 'application_name', None),
        "infrastructureCode": getattr(row, 'infrastructure_mst_code', None),
        "signalKind": signal_kind,
        "comparator": comparator,
        "thresholdValue": float(row.threshold_value) if row.threshold_value else 0,
        "thresholdUnit": threshold_unit,
        "evalWindow": row.eval_window,
        "forDuration": row.for_duration,
        "noData": getattr(row, 'no_data', None),
        "severity": severity,
        "monitorVendorRefId": getattr(row, 'monitoring_policy_defaults_ref_code', None),
        "vendorStatus": vendor_status,
        "vendorMonitorId": getattr(row, 'vendor_monitor_id', None),
        "vendorError": getattr(row, 'vendor_error', None),
    }
