"""
Service Config Chat Repository

Repository for service config chat operations.
Queries services_mst and service_configs tables.
Does NOT inherit BaseRepository - queries multiple tables.
"""
from typing import List, Optional, Dict, Any
from collections import Counter
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.core.enum import EnvironmentEnum, ServiceTypeEnum


class ServiceConfigChatRepository:
    """
    Repository for service config chat data access.

    Responsibilities:
    - List services for tenant
    - Check if config exists (CREATE vs EDIT mode)
    - Get reference configs from other environments
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_services_by_tenant(
        self,
        tenant_code: str,
        application_code: Optional[str] = None,
        resource_group_code: Optional[str] = None,
    ) -> List[ServicesMstModel]:
        """
        Get all active services for a tenant.

        Args:
            tenant_code: Tenant code
            application_code: Optional filter by application
            resource_group_code: Optional filter by resource group

        Returns:
            List of ServicesMstModel
        """
        filters = [
            ServicesMstModel.tenants_mst_code == tenant_code,
            ServicesMstModel.is_deleted == False,
            ServicesMstModel.is_active == True,
        ]

        if application_code:
            filters.append(ServicesMstModel.applications_mst_code == application_code)

        if resource_group_code:
            filters.append(ServicesMstModel.resource_group_mst_code == resource_group_code)

        stmt = (
            select(ServicesMstModel)
            .where(and_(*filters))
            .order_by(ServicesMstModel.name.asc())
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_service_by_code(
        self,
        service_code: str,
        tenant_code: str,
    ) -> Optional[ServicesMstModel]:
        """
        Get a specific service by code.

        Args:
            service_code: Service code
            tenant_code: Tenant code (for security)

        Returns:
            ServicesMstModel or None
        """
        stmt = (
            select(ServicesMstModel)
            .where(
                and_(
                    ServicesMstModel.code == service_code,
                    ServicesMstModel.tenants_mst_code == tenant_code,
                    ServicesMstModel.is_deleted == False,
                )
            )
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def check_config_exists(
        self,
        service_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        tenant_code: str,
    ) -> bool:
        """
        Check if a service config exists for the given combination.

        Used to determine CREATE vs EDIT mode.

        Args:
            service_code: Service code
            environment: Environment enum
            geo_loc_code: Geographic location code
            tenant_code: Tenant code

        Returns:
            True if config exists, False otherwise
        """
        stmt = (
            select(ServiceConfigModel.id)
            .where(
                and_(
                    ServiceConfigModel.services_mst_code == service_code,
                    ServiceConfigModel.environment == environment,
                    ServiceConfigModel.geo_loc_mst_code == geo_loc_code,
                    ServiceConfigModel.tenant_mst_code == tenant_code,
                    ServiceConfigModel.is_deleted == False,
                )
            )
            .limit(1)
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get_existing_config(
        self,
        service_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        tenant_code: str,
    ) -> Optional[ServiceConfigModel]:
        """
        Get existing config for edit mode.

        Args:
            service_code: Service code
            environment: Environment enum
            geo_loc_code: Geographic location code
            tenant_code: Tenant code

        Returns:
            ServiceConfigModel or None
        """
        stmt = (
            select(ServiceConfigModel)
            .where(
                and_(
                    ServiceConfigModel.services_mst_code == service_code,
                    ServiceConfigModel.environment == environment,
                    ServiceConfigModel.geo_loc_mst_code == geo_loc_code,
                    ServiceConfigModel.tenant_mst_code == tenant_code,
                    ServiceConfigModel.is_deleted == False,
                )
            )
            .order_by(ServiceConfigModel.updated_at.desc())
            .limit(1)
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_reference_configs(
        self,
        service_code: str,
        tenant_code: str,
        exclude_environment: Optional[EnvironmentEnum] = None,
        exclude_geo_loc: Optional[str] = None,
    ) -> List[ServiceConfigModel]:
        """
        Get configs from other environments as reference.

        Excludes the target env+geo_loc combination so user sees
        configs from staging/prod that they can use as template.

        Args:
            service_code: Service code
            tenant_code: Tenant code
            exclude_environment: Environment to exclude (target env)
            exclude_geo_loc: Geo location to exclude (target geo_loc)

        Returns:
            List of ServiceConfigModel from other envs
        """
        filters = [
            ServiceConfigModel.services_mst_code == service_code,
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.is_deleted == False,
        ]

        # Exclude target combination if specified
        if exclude_environment and exclude_geo_loc:
            filters.append(
                ~(
                    (ServiceConfigModel.environment == exclude_environment) &
                    (ServiceConfigModel.geo_loc_mst_code == exclude_geo_loc)
                )
            )

        stmt = (
            select(ServiceConfigModel)
            .where(and_(*filters))
            .order_by(
                ServiceConfigModel.environment.asc(),
                ServiceConfigModel.geo_loc_mst_code.asc()
            )
        )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_services_with_config_status(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
    ) -> List[dict]:
        """
        Get services with flag indicating if config exists for target env+geo_loc.

        Useful for showing which services already have configs.

        Args:
            tenant_code: Tenant code
            environment: Target environment
            geo_loc_code: Target geo location

        Returns:
            List of dicts with service info and has_config flag
        """
        # Get all services
        services = await self.get_services_by_tenant(tenant_code)

        # Get existing configs for target env+geo_loc
        config_stmt = (
            select(ServiceConfigModel.services_mst_code)
            .where(
                and_(
                    ServiceConfigModel.tenant_mst_code == tenant_code,
                    ServiceConfigModel.environment == environment,
                    ServiceConfigModel.geo_loc_mst_code == geo_loc_code,
                    ServiceConfigModel.is_deleted == False,
                )
            )
        )

        config_result = await self.session.execute(config_stmt)
        services_with_config = set(config_result.scalars().all())

        # Build result with config status
        result = []
        for service in services:
            result.append({
                "service_code": service.code,
                "service_name": service.name,
                "service_type": service.service_type.value,
                "has_existing_config": service.code in services_with_config,
            })

        return result

    async def get_services_with_any_config(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
    ) -> List[dict]:
        """
        Get services that have configs in ANY geo_loc for the given environment.

        Useful for reference purposes - user can copy from any region's config.

        Args:
            tenant_code: Tenant code
            environment: Target environment
            infra_vendor: Optional filter by infrastructure vendor (aws, azure, etc.)
            infrastructure_type: Optional filter by hosting type (ECS, Lambda, etc.)

        Returns:
            List of dicts with service info, has_config flag, and available regions
        """
        # Get all services
        services = await self.get_services_by_tenant(tenant_code)

        # Build filters for configs
        config_filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.is_deleted == False,
        ]

        # Add optional infrastructure filters
        if infra_vendor:
            config_filters.append(ServiceConfigModel.infra_vendor_enum == infra_vendor)
        if infrastructure_type:
            config_filters.append(ServiceConfigModel.infrastructuretype_ref_code == infrastructure_type)

        # Get all configs for this tenant+env (any geo_loc) with optional infra filters
        config_stmt = (
            select(
                ServiceConfigModel.services_mst_code,
                ServiceConfigModel.geo_loc_mst_code,
            )
            .where(and_(*config_filters))
        )

        config_result = await self.session.execute(config_stmt)
        configs = config_result.all()

        # Build mapping: service_code -> list of geo_locs with configs
        service_geo_locs = {}
        for service_code, geo_loc in configs:
            if service_code not in service_geo_locs:
                service_geo_locs[service_code] = []
            service_geo_locs[service_code].append(geo_loc)

        # Build result with config status and available regions
        result = []
        for service in services:
            geo_locs = service_geo_locs.get(service.code, [])
            result.append({
                "service_code": service.code,
                "service_name": service.name,
                "service_type": service.service_type.value,
                "has_existing_config": len(geo_locs) > 0,
                "available_geo_locs": geo_locs,
            })

        return result

    async def get_config_with_fallback(
        self,
        service_code: str,
        environment: EnvironmentEnum,
        preferred_geo_loc: str,
        tenant_code: str,
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
        infrastructure_mst_code: Optional[str] = None,
    ) -> tuple[Optional[ServiceConfigModel], Optional[str]]:
        """
        Get config with cascading fallback, preferring matching infrastructure.

        Fallback order:
        1. preferred_geo_loc + same vendor + same type + same cluster
        2. any geo_loc + same vendor + same type + same cluster
        3. preferred_geo_loc + same vendor + same type (any cluster)
        4. any geo_loc + same vendor + same type (any cluster)
        5. preferred_geo_loc (no infra filter)
        6. Last resort: any config

        Args:
            service_code: Service code
            environment: Environment enum
            preferred_geo_loc: Preferred geographic location (target region)
            tenant_code: Tenant code
            infra_vendor: Optional infrastructure vendor filter (aws, azure, etc.)
            infrastructure_type: Optional hosting type filter (ECS, Lambda, etc.)
            infrastructure_mst_code: Optional cluster/instance code filter

        Returns:
            Tuple of (config, actual_geo_loc) - actual_geo_loc indicates source region
        """
        base_filters = [
            ServiceConfigModel.services_mst_code == service_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.is_deleted == False,
        ]

        # Build infrastructure filters (vendor + type)
        infra_filters = []
        if infra_vendor:
            infra_filters.append(ServiceConfigModel.infra_vendor_enum == infra_vendor)
        if infrastructure_type:
            infra_filters.append(ServiceConfigModel.infrastructuretype_ref_code == infrastructure_type)

        # Build cluster filter
        cluster_filter = None
        if infrastructure_mst_code:
            cluster_filter = ServiceConfigModel.infrastructure_mst_code == infrastructure_mst_code

        # Step 1: Try preferred_geo_loc + same infra + same cluster
        if infra_filters and cluster_filter is not None:
            stmt = (
                select(ServiceConfigModel)
                .where(and_(
                    *base_filters,
                    ServiceConfigModel.geo_loc_mst_code == preferred_geo_loc,
                    *infra_filters,
                    cluster_filter,
                ))
                .limit(1)
            )
            result = await self.session.execute(stmt)
            config = result.scalar_one_or_none()
            if config:
                return config, preferred_geo_loc

        # Step 2: Try any geo_loc + same infra + same cluster
        if infra_filters and cluster_filter is not None:
            stmt = (
                select(ServiceConfigModel)
                .where(and_(*base_filters, *infra_filters, cluster_filter))
                .limit(1)
            )
            result = await self.session.execute(stmt)
            config = result.scalar_one_or_none()
            if config:
                return config, config.geo_loc_mst_code

        # Step 3: Try preferred_geo_loc + same infra (any cluster)
        if infra_filters:
            stmt = (
                select(ServiceConfigModel)
                .where(and_(
                    *base_filters,
                    ServiceConfigModel.geo_loc_mst_code == preferred_geo_loc,
                    *infra_filters,
                ))
                .limit(1)
            )
            result = await self.session.execute(stmt)
            config = result.scalar_one_or_none()
            if config:
                return config, preferred_geo_loc

        # Step 4: Try any geo_loc + same infra (any cluster)
        if infra_filters:
            stmt = (
                select(ServiceConfigModel)
                .where(and_(*base_filters, *infra_filters))
                .limit(1)
            )
            result = await self.session.execute(stmt)
            config = result.scalar_one_or_none()
            if config:
                return config, config.geo_loc_mst_code

        # Step 5: Try preferred_geo_loc (no infra filter)
        stmt = (
            select(ServiceConfigModel)
            .where(and_(
                *base_filters,
                ServiceConfigModel.geo_loc_mst_code == preferred_geo_loc,
            ))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        config = result.scalar_one_or_none()
        if config:
            return config, preferred_geo_loc

        # Step 6: Last resort - any config for this service+env
        stmt = (
            select(ServiceConfigModel)
            .where(and_(*base_filters))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        config = result.scalar_one_or_none()
        if config:
            return config, config.geo_loc_mst_code

        return None, None

    async def get_parameter_stats(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        parameter_name: str,
        infra_vendor: Optional[str] = None,
        infrastructure_type: Optional[str] = None,
        geo_loc_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get statistics for a parameter across configs IN THIS ENVIRONMENT ONLY.

        Loads all configs for tenant+env, extracts parameter values, aggregates.

        Args:
            tenant_code: Tenant code
            environment: Environment (scoped to this env only)
            parameter_name: Config parameter name (e.g., "cpu", "ram", "port")
            infra_vendor: Optional filter by infrastructure vendor (aws, azure, etc.)
            infrastructure_type: Optional filter by hosting type (ECS, Lambda, etc.)
            geo_loc_code: Optional filter by geographic location (mumbai, virginia, etc.)

        Returns:
            {
                "values": {"512": 5, "1024": 2},  # value -> count
                "most_common": "512",
                "total_configs": 7,
                "outliers": [{"service": "payment", "value": "2048"}]
            }
        """
        # Build filters for configs
        filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.is_deleted == False,
        ]

        # Add optional infrastructure filters
        if infra_vendor:
            filters.append(ServiceConfigModel.infra_vendor_enum == infra_vendor)
        if infrastructure_type:
            filters.append(ServiceConfigModel.infrastructuretype_ref_code == infrastructure_type)
        if geo_loc_code:
            filters.append(ServiceConfigModel.geo_loc_mst_code == geo_loc_code)

        # Load all configs for tenant+env with optional infra filters
        stmt = (
            select(ServiceConfigModel)
            .where(and_(*filters))
        )

        result = await self.session.execute(stmt)
        configs = list(result.scalars().all())

        if not configs:
            return {
                "values": {},
                "most_common": None,
                "total_configs": 0,
                "outliers": [],
            }

        # Get service names for all service codes in configs
        service_codes = list(set(c.services_mst_code for c in configs))
        service_name_map = await self._get_service_names(service_codes, tenant_code)

        # Extract parameter values from config JSON, sidecar_config, or model columns
        values_with_service = []
        for config in configs:
            config_dict = config.config or {}
            sidecar_config = config.sidecar_config or []

            # Special handling for model column parameters (not in config JSONB)
            if parameter_name == "language":
                value = config.language_ref_code
            else:
                value = self._extract_parameter_value(config_dict, parameter_name, sidecar_config)

            if value is not None:
                service_name = service_name_map.get(
                    config.services_mst_code,
                    config.services_mst_code[:8]  # Fallback to short code
                )
                values_with_service.append({
                    "service": service_name,
                    "value": str(value),
                })

        if not values_with_service:
            return {
                "values": {},
                "most_common": None,
                "total_configs": len(configs),
                "outliers": [],
            }

        # Aggregate values
        value_counts = Counter(item["value"] for item in values_with_service)
        most_common_value, most_common_count = value_counts.most_common(1)[0]

        # Find outliers (values that appear only once and differ from most common)
        outliers = []
        for item in values_with_service:
            if item["value"] != most_common_value and value_counts[item["value"]] == 1:
                outliers.append(item)

        # Count unique services (a service can have multiple configs across geo_locs)
        unique_services = len(set(item["service"] for item in values_with_service))

        return {
            "values": dict(value_counts),
            "most_common": most_common_value,
            "total_services": unique_services,
            "total_configs": len(values_with_service),
            "outliers": outliers[:3],  # Limit to 3 outliers
        }

    # Map canonical parameter names to actual config JSON paths
    # Based on MainConfigSchema, AutoScalingConfigSchema, EbsConfigSchema
    CANONICAL_TO_CONFIG_PATH = {
        # Direct field mappings (canonical → actual key in config JSON)
        "memory": "ram",
        "container_port": "port",
        "health_check_path": "health",
        # Nested under "autoscaling"
        "enable_autoscaling": "autoscaling.enabled",
        "min_task_count": "autoscaling.min",
        "max_task_count": "autoscaling.max",
        "desired_count": "autoscaling.desired",
        # Nested under "ebs"
        "ebs_volume": "ebs.volume",
        "ebs_size": "ebs.size",
        "ebs_type": "ebs.type",
    }

    # Map sidecar parameters to (sidecar_config_code, field_name)
    # sidecar_config is stored as: [{"sidecar_config_code": "datadog", "cpu": "256", "ram": "512"}]
    SIDECAR_PARAMETER_MAP = {
        "enable_datadog_sidecar": ("datadog", None),  # None = check if exists
        "datadog_sidecar_cpu": ("datadog", "cpu"),
        "datadog_sidecar_memory": ("datadog", "ram"),
        "datadog_log_source": ("datadog", "log_source"),
        "enable_otel_sidecar": ("otel", None),
        "otel_sidecar_cpu": ("otel", "cpu"),
        "otel_sidecar_memory": ("otel", "ram"),
    }

    def _extract_parameter_value(
        self,
        config_dict: Dict[str, Any],
        parameter_name: str,
        sidecar_config: List[Dict[str, Any]] = None,
    ) -> Any:
        """
        Extract parameter value from config dict or sidecar_config.

        Handles canonical names, direct fields, nested paths, and sidecar parameters.

        Args:
            config_dict: Config JSON dict
            parameter_name: Canonical parameter name
            sidecar_config: Sidecar config array (optional)

        Returns:
            Parameter value or None
        """
        # Check if this is a sidecar parameter
        if parameter_name in self.SIDECAR_PARAMETER_MAP:
            return self._extract_sidecar_value(parameter_name, sidecar_config)

        # Map canonical name to actual config path
        config_path = self.CANONICAL_TO_CONFIG_PATH.get(parameter_name, parameter_name)

        # Handle nested paths (e.g., "autoscaling.min" -> config["autoscaling"]["min"])
        if "." in config_path:
            parts = config_path.split(".")
            current = config_dict
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None
            return current

        # Direct lookup
        return config_dict.get(config_path)

    def _extract_sidecar_value(
        self,
        parameter_name: str,
        sidecar_config: List[Dict[str, Any]],
    ) -> Any:
        """
        Extract value from sidecar_config array.

        Args:
            parameter_name: Canonical parameter name (e.g., datadog_sidecar_cpu)
            sidecar_config: Sidecar config array

        Returns:
            Parameter value or None
        """
        if not sidecar_config:
            return None

        sidecar_keyword, field_name = self.SIDECAR_PARAMETER_MAP.get(parameter_name, (None, None))
        if not sidecar_keyword:
            return None

        # Find the sidecar entry - check if keyword is contained in code or name
        # sidecar_config_code could be UUID or code like "datadog-agent-v2"
        for sidecar in sidecar_config:
            sidecar_name = (sidecar.get("name") or "").lower()
            sidecar_code = (sidecar.get("sidecar_config_code") or "").lower()
            is_match = sidecar_keyword in sidecar_name or sidecar_keyword in sidecar_code

            if is_match:
                if field_name is None:
                    # enable_* parameter - return True if sidecar exists and is enabled
                    return sidecar.get("enabled", True)
                return sidecar.get(field_name)

        return None

    async def _get_service_names(
        self,
        service_codes: List[str],
        tenant_code: str,
    ) -> Dict[str, str]:
        """
        Get service names for a list of service codes.

        Args:
            service_codes: List of service codes
            tenant_code: Tenant code

        Returns:
            Dict mapping service_code -> service_name
        """
        if not service_codes:
            return {}

        stmt = (
            select(ServicesMstModel.code, ServicesMstModel.name)
            .where(
                and_(
                    ServicesMstModel.code.in_(service_codes),
                    ServicesMstModel.tenants_mst_code == tenant_code,
                )
            )
        )

        result = await self.session.execute(stmt)
        return {row.code: row.name for row in result.all()}

    async def get_parameter_stats_by_service_type(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        parameter_name: str,
        service_type: ServiceTypeEnum,
        geo_loc_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get statistics for a parameter filtered by service type.

        Similar to get_parameter_stats, but filters by service_type (API or BACKGROUND_SERVICE).
        Used for data-driven recommendations.

        Args:
            tenant_code: Tenant code
            environment: Environment (scoped to this env only)
            parameter_name: Config parameter name (e.g., "cpu", "ram", "port")
            service_type: Service type (API or BACKGROUND_SERVICE)
            geo_loc_code: Optional filter by geographic location

        Returns:
            {
                "values": {"512": 5, "1024": 2},  # value -> count
                "most_common": "512",
                "total_configs": 7,
                "total_services": 5,
                "outliers": [{"service": "payment", "value": "2048"}]
            }
        """
        # Build filters for configs
        filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.is_deleted == False,
        ]

        if geo_loc_code:
            filters.append(ServiceConfigModel.geo_loc_mst_code == geo_loc_code)

        # Load configs with a JOIN to services_mst to filter by service_type
        stmt = (
            select(ServiceConfigModel)
            .join(
                ServicesMstModel,
                ServiceConfigModel.services_mst_code == ServicesMstModel.code
            )
            .where(
                and_(
                    *filters,
                    ServicesMstModel.service_type == service_type,
                    ServicesMstModel.is_active == True,
                )
            )
        )

        result = await self.session.execute(stmt)
        configs = list(result.scalars().all())

        if not configs:
            return {
                "values": {},
                "most_common": None,
                "total_configs": 0,
                "total_services": 0,
                "outliers": [],
            }

        # Get service names for all service codes in configs
        service_codes = list(set(c.services_mst_code for c in configs))
        service_name_map = await self._get_service_names(service_codes, tenant_code)

        # Extract parameter values from config JSON, sidecar_config, or model columns
        values_with_service = []
        for config in configs:
            config_dict = config.config or {}
            sidecar_config = config.sidecar_config or []

            # Special handling for model column parameters (not in config JSONB)
            if parameter_name == "language":
                value = config.language_ref_code
            else:
                value = self._extract_parameter_value(config_dict, parameter_name, sidecar_config)

            if value is not None:
                service_name = service_name_map.get(
                    config.services_mst_code,
                    config.services_mst_code[:8]  # Fallback to short code
                )
                values_with_service.append({
                    "service": service_name,
                    "value": str(value),
                })

        if not values_with_service:
            return {
                "values": {},
                "most_common": None,
                "total_configs": len(configs),
                "total_services": len(service_codes),
                "outliers": [],
            }

        # Aggregate values
        value_counts = Counter(item["value"] for item in values_with_service)
        most_common_value, most_common_count = value_counts.most_common(1)[0]

        # Find outliers (values that appear only once and differ from most common)
        outliers = []
        for item in values_with_service:
            if item["value"] != most_common_value and value_counts[item["value"]] == 1:
                outliers.append(item)

        # Count unique services
        unique_services = len(set(item["service"] for item in values_with_service))

        return {
            "values": dict(value_counts),
            "most_common": most_common_value,
            "total_services": unique_services,
            "total_configs": len(values_with_service),
            "outliers": outliers[:3],  # Limit to 3 outliers
        }

    async def get_used_listener_priorities(
        self,
        tenant_code: str,
        environment: EnvironmentEnum,
        geo_loc_code: str,
        exclude_service_code: Optional[str] = None,
    ) -> List[int]:
        """
        Get all listener_rule_priority values currently in use.

        Used to find next available priority and validate uniqueness.

        Args:
            tenant_code: Tenant code
            environment: Environment (dev/staging/prod)
            geo_loc_code: Geographic location code
            exclude_service_code: Optional service to exclude (for updates)

        Returns:
            List of used priority integers, sorted ascending
        """
        filters = [
            ServiceConfigModel.tenant_mst_code == tenant_code,
            ServiceConfigModel.environment == environment,
            ServiceConfigModel.geo_loc_mst_code == geo_loc_code,
            ServiceConfigModel.is_deleted == False,
        ]

        if exclude_service_code:
            filters.append(
                ServiceConfigModel.services_mst_code != exclude_service_code
            )

        stmt = (
            select(ServiceConfigModel.config["listener_rule_priority"])
            .where(and_(*filters))
        )

        result = await self.session.execute(stmt)
        rows = result.fetchall()

        # Extract and filter valid integer priorities
        priorities = []
        for row in rows:
            value = row[0]
            if value is not None:
                try:
                    priorities.append(int(value))
                except (ValueError, TypeError):
                    pass  # Skip non-integer values

        return sorted(priorities)
