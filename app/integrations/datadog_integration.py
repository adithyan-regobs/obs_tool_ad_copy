import httpx
import re
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Default timeout for Datadog API calls (in seconds)
DEFAULT_TIMEOUT = 300.0


class DatadogIntegration:
    """Integration class for Datadog monitoring API"""

    # Comparator mapping for Datadog queries
    COMPARATOR_MAP = {
        "gt": ">",
        "lt": "<",
        "gte": ">=",
        "lte": "<=",
        "eq": "=="
    }

    # Signal kind mapping to Datadog monitor types
    SIGNAL_KIND_MAP = {
        "metric": "metric alert",
        "log": "log alert",
        "trace": "trace-analytics alert"
    }

    @staticmethod
    def render_query(
        query_template: str,
        eval_window: int,
        service_name: str,
        operator: str,
        threshold: float,
        **kwargs
    ) -> str:
        """
        Render Datadog query from template with runtime substitution.

        Args:
            query_template: Query template with placeholders (supports {{placeholder}} or {placeholder})
            eval_window: Time window in minutes (e.g., 5 for last_5m)
            service_name: Service name for scoping query
            operator: Comparison operator (>, <, >=, <=, ==)
            threshold: Threshold value
            **kwargs: Additional parameters for substitution

        Returns:
            Fully rendered Datadog query string

        Example:
            template = "avg(last_{{window}}m):avg:aws.ec2.cpuutilization{{service:{{service_name}}}} {{operator}} {{threshold}}"
            query = render_query(template, eval_window=5, service_name='s1', operator='>', threshold=90)
            # Returns: "avg(last_5m):avg:aws.ec2.cpuutilization{service:s1} > 90"
        """
        # Build parameter mapping
        # Map method parameters to template placeholder names used in query templates
        params = {
            # Time window parameters
            'window': eval_window,
            'change_window': f'{eval_window * 2}m',  # For change() function: 10m format (no quotes)
            'rollup_seconds': eval_window * 60,  # Convert minutes to seconds

            # Service/Resource identifiers
            'service_name': service_name,
            'service': service_name,
            'container_name': service_name,  # Use service name as container name by default
            'cluster_name': kwargs.get('cluster_name', '*'),  # Wildcard for cluster by default
            'instance_id': kwargs.get('instance_id', '*'),  # Wildcard for instance by default
            'subnet_id': kwargs.get('subnet_id', '*'),  # Wildcard for subnet by default

            # Alert threshold parameters
            'operator': operator,
            'threshold': threshold,

            # Logging parameters
            'log_index': kwargs.get('log_index', 'main'),  # Default log index

            **kwargs  # Allow additional custom parameters to override defaults
        }

        # Replace {{placeholder}} with actual values using regex
        # This preserves Datadog's {tag:value} syntax while replacing our {{placeholders}}
        def replace_placeholder(match):
            placeholder = match.group(1)
            if placeholder in params:
                return str(params[placeholder])
            else:
                raise KeyError(f"Missing template parameter: {placeholder}")

        # Use regex to find and replace {{placeholder}} patterns
        rendered_query = re.sub(r'\{\{(\w+)\}\}', replace_placeholder, query_template)

        return rendered_query

    @staticmethod
    def build_payload(
        monitoring_policy,
        alert_data,
        service,
        query_template: str
    ) -> Dict[str, Any]:
        """
        Build Datadog monitor payload from monitoring policy and alert data

        Args:
            monitoring_policy: MonitoringPolicyDefaultsRef object
            alert_data: CreateAlert schema
            service: ServicesMstModel object - required for service-specific queries
            query_template: Query template string - required for building query

        Returns:
            Dict containing complete Datadog monitor payload

        Raises:
            ValueError: If service or query_template is None
        """
        # Validate required parameters
        if not service:
            raise ValueError("Service is required to build Datadog payload")
        if not query_template:
            raise ValueError("Query template is required to build Datadog payload")

        # Get Datadog operator
        operator = DatadogIntegration.COMPARATOR_MAP.get(alert_data.comparator.value, ">")

        # Map signal kind to Datadog monitor type
        monitor_type = DatadogIntegration.SIGNAL_KIND_MAP.get(monitoring_policy.signal_kind.value, "metric alert")

        # Build query string using template rendering
        query = DatadogIntegration.render_query(
            query_template=query_template,
            eval_window=alert_data.eval_window,
            service_name=service.name,  # Using service name for query
            operator=operator,
            threshold=alert_data.threshold_value
        )

        # Build complete payload
        payload = {
            "name": f"{monitoring_policy.name} - {alert_data.severity.value}",
            "type": monitor_type,
            "query": query,
            "message": f"Alert triggered for {monitoring_policy.alerttype_ref_code}. Severity: {alert_data.severity.value}",
            "options": {
                "thresholds": {
                    "critical": float(alert_data.threshold_value)
                },
                "notify_audit": True,
                "include_tags": True,
                "evaluation_delay": alert_data.for_duration * 60,  # Convert minutes to seconds
            },
            "tags": [
                f"resource_kind:{monitoring_policy.infrastructuretype_ref_code}",
                f"severity:{alert_data.severity.value}",
                f"source:devlift",
            ],
        }

        return payload

    @staticmethod
    async def create_monitor(auth_config: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a monitor in Datadog via API call

        Args:
            auth_config: Dict containing api_key, app_key, and base_url
            payload: Complete Datadog monitor payload

        Returns:
            Dict with Datadog API response

        Raises:
            Exception: If API call fails
        """
        base_url = auth_config.get("base_url", "https://api.datadoghq.com/api/v1/monitor")

        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": auth_config.get("api_key"),
            "DD-APPLICATION-KEY": auth_config.get("app_key"),
        }

        # Make API request
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(base_url, headers=headers, json=payload)

        if response.status_code == 200:
            logger.info(f"Successfully created Datadog monitor: {payload.get('name')}")
            return response.json()
        else:
            error_msg = f"Failed to create Datadog monitor: {response.status_code} - {response.text}"
            logger.error(error_msg)
            raise Exception(error_msg)

    @staticmethod
    async def update_monitor(auth_config: Dict[str, str], monitor_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing monitor in Datadog via API call

        Args:
            auth_config: Dict containing api_key, app_key, and base_url
            monitor_id: Existing Datadog monitor ID to update
            payload: Complete Datadog monitor payload with updated configuration

        Returns:
            Dict with Datadog API response

        Raises:
            Exception: If API call fails
        """
        base_url = auth_config.get("base_url", "https://api.datadoghq.com/api/v1/monitor")
        update_url = f"{base_url}/{monitor_id}"

        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": auth_config.get("api_key"),
            "DD-APPLICATION-KEY": auth_config.get("app_key"),
        }

        # Make API request
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.put(update_url, headers=headers, json=payload)

        if response.status_code == 200:
            logger.info(f"Successfully updated Datadog monitor: {monitor_id}")
            return response.json()
        else:
            error_msg = f"Failed to update Datadog monitor: {response.status_code} - {response.text}"
            logger.error(error_msg)
            raise Exception(error_msg)
