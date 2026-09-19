from datetime import datetime
from typing import Optional
from uuid import uuid4
from app.schemas.alert_schemas import CreateAlert
from app.core.enum import IntegrationStatusEnum


def make_alert_configs(
    data: CreateAlert,
    vendor_account_code: str,
    monitoring_policy_code: Optional[str] = None,
    vendor_monitor_id: Optional[str] = None,
    monitor_vendor_reference_identifier: Optional[str] = None,
    vendor_status: IntegrationStatusEnum = IntegrationStatusEnum.SUCCESS,
    vendor_error: Optional[str] = None
) -> dict:
    """
    Factory to build an AlertConfig domain object from validated CreateAlert data.

    Args:
        data: CreateAlert schema with user input
        vendor_account_code: Code from obs_vendor_accounts_mst table
        monitoring_policy_code: Optional code from monitoring_policy_defaults_ref table
        vendor_monitor_id: Optional vendor monitor ID from API response
        monitor_vendor_reference_identifier: Internal identifier for terragrunt/IaC (e.g., "service-policy-severity")
        vendor_status: Status of vendor integration (active, pending, failed)
        vendor_error: Error message if vendor integration failed

    This isolates object creation logic (like defaults, normalization, and derived fields)
    from the repository and service layers.
    """
    # Generate unique code for this alert config
    alert_code = f"AC_{uuid4().hex[:8].upper()}"

    # Return dictionary for repository create method
    alert_data = {
        "code": alert_code,
        "name": f"Alert - {data.services_code} - {data.severity.value}",
        "description": f"Alert configuration for service {data.services_code}",
        "monitoring_policy_defaults_ref_code": monitoring_policy_code,
        "obs_vendor_accounts_mst_code": vendor_account_code,
        "services_mst_code": data.services_code,
        "infrastructure_mst_code": None,
        "comparator": data.comparator,
        "threshold_value": data.threshold_value,
        "threshold_unit": data.threshold_unit,
        "eval_window": data.eval_window,
        "for_duration": data.for_duration,
        "severity": data.severity,
        "signal_kind": "metric",
        # Vendor sync tracking
        "vendor_status": vendor_status,
        "vendor_monitor_id": vendor_monitor_id,
        "monitor_vendor_reference_identifier": monitor_vendor_reference_identifier,
        "vendor_error": vendor_error,
        "vendor_status_updated_at": datetime.utcnow(),
        "vendor_last_sync_attempt": datetime.utcnow(),
        "vendor_retry_count": 0,
    }

    return alert_data
