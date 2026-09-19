"""
Service for fetching VPC and resource canvas data.
Provides mock data organized by tenant → application → environment,
mirroring the frontend VPC_SCANNED_DATA structure.
"""
import logging
from typing import Optional, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import AWSResourceTypeEnum, EnvironmentEnum
from app.schemas.vpc_discovery_schemas import (
    CanvasApiResponse,
    CanvasNode,
    CanvasEksCluster,
    CanvasEcsCluster,
    CanvasPlacementContext,
)

logger = logging.getLogger(__name__)
from app.utils.service_routing import display_service_path
from app.utils.service_urls import build_service_url
from app.utils.static_data.vpc_resource_data.aspora import (
    CORE_STAGE,
    CORE_PROD,
    CORE_QA,
    FALCON_STAGE,
    FALCON_PROD,
    OLD_PROD_VPC_DATA,
)
from app.utils.static_data.vpc_resource_data.cozmox import (
    COZMOX_PROD,
    COZMOX_STAGE,
    COZMOX_UAT,
)
from app.utils.static_data.vpc_resource_data.fintech import FINTECH_DEMO
from app.utils.static_data.vpc_resource_data.paas_tenant_placeholder import PAAS_TENANT_PLACEHOLDER, ASPORA_TENANT_PLACEHOLDER
from app.utils.static_data.vpc_resource_data.pred import PRED_UAT
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.repository.service_config_repository import ServiceConfigRepository
from app.repository.geo_loc_mst_repository import GeoLocMstRepository
from app.repository.transaction_queue_repository import TransactionQueueRepository
from app.core.enum import WorkflowSourceTableEnum


def _resource_status_value(record) -> str:
    """Return the raw status string from an ORM row (`record.status`) or dict
    row (`record["resource_status"]`). Enum instances are unwrapped via `.value`."""
    if hasattr(record, "status"):
        raw = record.status
    elif isinstance(record, dict):
        raw = record.get("resource_status")
    else:
        return ""
    if raw is None:
        return ""
    return raw.value if hasattr(raw, "value") else str(raw)


# ─── Mock Data Registry ──────────────────────────────────────────────────────
# Structure mirrors frontend VPC_SCANNED_DATA:
#   tenant_code → application_code → environment → CanvasApiResponse

_MOCK_CANVAS_DATA: dict[str, dict[str, dict[str, dict]]] = {
    "aspora": {
        "d19899af-78e8-44aa-b95f-afd932a019e3": {  # core
            "stage": CORE_STAGE,
            "prod": CORE_PROD,
            "qa": CORE_QA,
        },
        "6800d09d-3532-422e-8de9-ee16c9c08f2e": {  # falcon
            "stage": FALCON_STAGE,
            "prod": FALCON_PROD,
        },
        "oldProd": {
            "prod": OLD_PROD_VPC_DATA,
        },
    },
    "vance": {
        "178d48fc-8b2c-4e79-aca9-e18f089a05f9": {  # core
            "stage": CORE_STAGE,
            "prod": CORE_PROD,
            "qa": CORE_QA,
        },
        "7cfdf597-a879-410c-95b0-fbebfe88abb7": {  # falcon
            "stage": FALCON_STAGE,
            "prod": FALCON_PROD,
        },
        "oldProd": {
            "prod": OLD_PROD_VPC_DATA,
        },
    },
    "fintech": {
        "neoflow": {
            "prod": FINTECH_DEMO,
        },
    },
    "cozmox": {
        "cosmoAi": {
            "stage": COZMOX_STAGE,
            "prod": COZMOX_PROD,
            "uat": COZMOX_UAT,
        },
    },
    "pred": {
        "4ece9bc2-9c0e-41fd-9378-0a3a97bcb378": {  # pred_trail
            "stage": PRED_UAT,
        },
    },
}


# ─── Service ─────────────────────────────────────────────────────────────────

class VpcAndResourceDiscoveryService:
    """
    Returns pre-configured canvas data (mock) for a given
    tenant / application / environment combination, enriched with
    matching records from the database.

    Replace or extend _MOCK_CANVAS_DATA entries with real DB/API
    lookups as the backend data layer is built out.
    """

    async def get_canvas_data(
        self,
        tenant_code: str,
        application_code: str,
        environment: str,
        db: AsyncSession,
    ) -> Optional[CanvasApiResponse]:
        """
        Look up canvas data by tenant, application and environment,
        then enrich nodes with matching DB records.

        Returns None when no data is registered for the combination.
        """
        raw = (
            _MOCK_CANVAS_DATA
            .get(tenant_code, {})
            .get(application_code, {})
            .get(environment)
        )
        if raw is None:
            raw = ASPORA_TENANT_PLACEHOLDER if tenant_code == "vance" else PAAS_TENANT_PLACEHOLDER
        canvas = CanvasApiResponse(**raw)
        _is_placeholder = raw is PAAS_TENANT_PLACEHOLDER or raw is ASPORA_TENANT_PLACEHOLDER
        canvas.showEmptyContainers = _is_placeholder
        canvas.forPaas = _is_placeholder
        await self._enrich_canvas_data(canvas, tenant_code, application_code, environment, db)
        return canvas

    async def get_placement_context(
        self,
        tenant_code: str,
        application_code: str,
        environment: str,
        db: AsyncSession,
    ) -> CanvasPlacementContext:
        """Return just `geoLocations / accounts / cloudRegions` for the given
        tenant / application / environment.

        Callers that only need to resolve a single resource's placement
        (e.g. the MCP server resolving cloudRegion+accountId for a new infra
        record) use this instead of `get_canvas_data`, avoiding the full VPC /
        node / cluster enrichment pass.

        geoLocations are still enriched with DB `geoLocCode` (same as the full
        canvas), so the caller's geo_loc_mst_code lookup works.
        """
        raw = (
            _MOCK_CANVAS_DATA
            .get(tenant_code, {})
            .get(application_code, {})
            .get(environment)
        )
        if raw is None:
            raw = ASPORA_TENANT_PLACEHOLDER if tenant_code == "vance" else PAAS_TENANT_PLACEHOLDER

        context = CanvasPlacementContext(
            geoLocations=raw.get("geoLocations", []),
            accounts=raw.get("accounts", []),
            cloudRegions=raw.get("cloudRegions", []),
        )
        await self._enrich_geo_locations(context.geoLocations, tenant_code, db)
        return context

    # ─── Enrichment Orchestrator ─────────────────────────────────────────────

    async def _enrich_canvas_data(
        self,
        canvas_response: CanvasApiResponse,
        tenant_code: str,
        application_code: str,
        environment: str,
        db: AsyncSession,
    ) -> None:
        """Enrich canvas nodes with DB records (infra + services)."""
        try:
            env_enum = EnvironmentEnum(environment)
        except ValueError:
            return  # Unknown environment — skip enrichment

        await self._enrich_geo_locations(canvas_response.geoLocations, tenant_code, db)
        await self._enrich_infra_nodes(
            canvas_response.nodes, tenant_code, application_code, env_enum, db
        )
        await self._append_draft_infra_nodes(
            canvas_response, tenant_code, application_code, env_enum, db
        )
        await self._enrich_service_nodes(
            canvas_response.nodes, tenant_code, application_code, environment, db
        )
        await self._append_draft_service_nodes(
            canvas_response, tenant_code, application_code, environment, db
        )
        await self._enrich_clusters(
            canvas_response, tenant_code, application_code, env_enum, db
        )

    # ─── Geo Location Enrichment ──────────────────────────────────────────────

    async def _enrich_geo_locations(
        self,
        geo_locations: List,
        tenant_code: str,
        db: AsyncSession,
    ) -> None:
        """
        Match each canvas geoLocation to a geo_loc_mst record by name (case-insensitive)
        and attach the DB code as geoLocCode. Sets geoLocCode=None when no match found.
        """
        geo_repo = GeoLocMstRepository(db)
        db_geos = await geo_repo.get_by_tenant(tenant_code)

        # Build lowercase name → code lookup
        name_to_code = {g.name.lower(): g.code for g in db_geos}

        # Canvas geo name → DB geo name aliases (for region/city name mismatches)
        GEO_NAME_ALIASES: dict[str, str] = {
            "india": "mumbai",
            "europe": "london",
            "north america": "canada",
        }

        for geo in geo_locations:
            geo_name_lower = geo.name.lower()
            lookup_name = GEO_NAME_ALIASES.get(geo_name_lower, geo_name_lower)
            geo.geoLocCode = name_to_code.get(lookup_name)

    # ─── S3 / SQS / DynamoDB Enrichment ─────────────────────────────────────

    async def _enrich_infra_nodes(
        self,
        nodes: List[CanvasNode],
        tenant_code: str,
        application_code: str,
        env_enum: EnvironmentEnum,
        db: AsyncSession,
    ) -> None:
        """
        Enrich S3, SQS, DynamoDB, and RDS nodes with matching infrastructure_mst records.

        Adds to settings:
          - resourceDbCode: infrastructure_mst.code
          - resourceDbId:   infrastructure_mst.id
          - managedInDb:    True
        """
        infra_repo = InfrastructureMstRepository(db)

        # Bulk-fetch all types upfront (5 queries)
        s3_records = await infra_repo.list_by_filters(
            tenant_code, "s3_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        sqs_records = await infra_repo.list_by_filters(
            tenant_code, "sqs_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        ddb_records = await infra_repo.list_by_filters(
            tenant_code, "dynamodb_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        aurora_mysql_records = await infra_repo.list_by_filters(
            tenant_code, "aurora_mysql_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        aurora_pg_records = await infra_repo.list_by_filters(
            tenant_code, "aurora_postgres_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )

        # Build name → record lookup dicts
        s3_lookup = {
            r.locator["bucket_name"]: r
            for r in s3_records
            if r.locator and "bucket_name" in r.locator
        }
        sqs_lookup = {
            r.locator["queue_name"]: r
            for r in sqs_records
            if r.locator and "queue_name" in r.locator
        }
        ddb_lookup = {
            r.locator["table_name"]: r
            for r in ddb_records
            if r.locator and "table_name" in r.locator
        }
        aurora_mysql_lookup = {
            r.locator["server_name"]: r
            for r in aurora_mysql_records
            if r.locator and "server_name" in r.locator
        }
        aurora_pg_lookup = {
            r.locator["server_name"]: r
            for r in aurora_pg_records
            if r.locator and "server_name" in r.locator
        }

        for node in nodes:
            record = None
            if node.resourceType == "bucket":
                record = s3_lookup.get(node.name)
            elif node.resourceType == "queue":
                record = sqs_lookup.get(node.name)
            elif (
                node.resourceType == "database"
                and node.settings.get("databaseType") == "dynamodb"
            ):
                record = ddb_lookup.get(node.name)
            elif (
                node.resourceType == "database"
                and node.settings.get("databaseType") in ("mysql", "aurora-mysql")
            ):
                # RDS/Aurora MySQL: scanned name is like
                # {tenant}-{product}-{env}-{geo}-01-{server_name}-db-one
                # DB locator has server_name (e.g. "common-mysql")
                bare_name = node.name
                if "-01-" in node.name:
                    bare_name = node.name.split("-01-", 1)[1]
                # Match: find server_name that is a prefix of bare_name
                for server_name, r in aurora_mysql_lookup.items():
                    if bare_name.startswith(server_name):
                        record = r
                        break
            elif (
                node.resourceType == "database"
                and node.settings.get("databaseType") in ("postgresql", "aurora-postgresql")
            ):
                # Aurora PostgreSQL: same naming convention as MySQL
                # {tenant}-{product}-{env}-{geo}-01-{server_name}-db-one
                bare_name = node.name
                if "-01-" in node.name:
                    bare_name = node.name.split("-01-", 1)[1]
                for server_name, r in aurora_pg_lookup.items():
                    if bare_name.startswith(server_name):
                        record = r
                        break

            if record:
                node.settings["resourceDbCode"] = record.code
                node.settings["resourceDbId"] = record.id
                node.settings["managedInDb"] = True
                node.settings["infraType"] = record.infrastructuretype_ref_code
                node.settings["geoLocCode"] = record.geo_loc_mst_code
                node.settings["locator"] = record.locator
                node.settings["deploymentStatus"] = record.deployment_status.value if record.deployment_status else None

        # Bulk-fetch latest queue status for all matched infra nodes
        matched_codes = [
            node.settings["resourceDbCode"]
            for node in nodes
            if node.settings.get("resourceDbCode") and node.settings.get("managedInDb")
        ]
        if matched_codes:
            queue_repo = TransactionQueueRepository(db)
            infra_queue_map = await queue_repo.get_latest_by_transaction_codes(
                matched_codes, WorkflowSourceTableEnum.INFRASTRUCTURE, tenant_code,
                active_pipeline_only=True,
            )
            for node in nodes:
                db_code = node.settings.get("resourceDbCode")
                if db_code:
                    q = infra_queue_map.get(db_code)
                    if q:
                        node.settings["queueStatus"] = q.status.value if q.status else None
                        node.settings["queueCode"] = q.code

    # ─── EKS / ECS Cluster Enrichment ────────────────────────────────────────

    async def _enrich_clusters(
        self,
        canvas_response: CanvasApiResponse,
        tenant_code: str,
        application_code: str,
        env_enum: EnvironmentEnum,
        db: AsyncSession,
    ) -> None:
        """
        Enrich eksClusters and ecsClusters with matching infrastructure_mst records.

        Matches by clusterArn (from locator["cluster_arn"]) and enriches with:
          - infrastructureMstCode: infrastructure_mst.code
          - infrastructureMstName: infrastructure_mst.name
        """
        infra_repo = InfrastructureMstRepository(db)

        # Bulk-fetch EKS and ECS cluster records (2 queries)
        eks_records = await infra_repo.list_by_filters(
            tenant_code, "eks_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        if not eks_records:
            eks_records = await infra_repo.list_tenant_level_clusters(
                tenant_code, "eks_infrastructuretype_ref", env_enum,
            )

        ecs_records = await infra_repo.list_by_filters(
            tenant_code, "ecs_ec2_infrastructuretype_ref", env_enum,
            applications_mst_code=application_code,
        )
        if not ecs_records:
            ecs_records = await infra_repo.list_tenant_level_clusters(
                tenant_code, "ecs_ec2_infrastructuretype_ref", env_enum,
            )

        # Build clusterArn → record lookup (primary), clusterName → record (fallback)
        eks_arn_lookup = {
            r.locator["cluster_arn"]: r
            for r in eks_records
            if r.locator and "cluster_arn" in r.locator
        }
        eks_name_lookup = {
            r.locator["cluster_name"]: r
            for r in eks_records
            if r.locator and "cluster_name" in r.locator and "cluster_arn" not in r.locator
        }
        ecs_arn_lookup = {
            r.locator["cluster_arn"]: r
            for r in ecs_records
            if r.locator and "cluster_arn" in r.locator
        }
        ecs_name_lookup = {
            r.locator["cluster_name"]: r
            for r in ecs_records
            if r.locator and "cluster_name" in r.locator and "cluster_arn" not in r.locator
        }

        # Clusters flagged with locator["isListed"] == False are excluded from the
        # canvas entirely — the cluster and every service running on it are dropped
        # from the response. An absent flag means listed (no backfill needed).
        def _is_unlisted(rec) -> bool:
            return (rec.locator or {}).get("isListed", True) is False

        unlisted_arns: set[str] = set()
        unlisted_eks_names: set[str] = set()
        unlisted_ecs_names: set[str] = set()
        for records, names in ((eks_records, unlisted_eks_names), (ecs_records, unlisted_ecs_names)):
            for rec in records:
                if not _is_unlisted(rec):
                    continue
                loc = rec.locator or {}
                if loc.get("cluster_arn"):
                    unlisted_arns.add(loc["cluster_arn"])
                name = loc.get("cluster_name") or rec.name
                if name:
                    names.add(name)

        # Track which DB records were matched to a scanned cluster
        matched_eks_codes: set[str] = set()
        matched_ecs_codes: set[str] = set()

        for cluster in canvas_response.eksClusters:
            record = eks_arn_lookup.get(cluster.clusterArn) or eks_name_lookup.get(cluster.clusterName)
            if record:
                cluster.infrastructureMstCode = record.code
                cluster.infrastructureMstName = record.name
                cluster.infraStatus = record.infra_status.value if record.infra_status else None
                cluster.isRegistered = (record.locator or {}).get("isRegistered", False)
                matched_eks_codes.add(record.code)

        for cluster in canvas_response.ecsClusters:
            record = ecs_arn_lookup.get(cluster.clusterArn) or ecs_name_lookup.get(cluster.clusterName)
            if record:
                cluster.infrastructureMstCode = record.code
                cluster.infrastructureMstName = record.name
                cluster.infraStatus = record.infra_status.value if record.infra_status else None
                cluster.isRegistered = (record.locator or {}).get("isRegistered", False)
                matched_ecs_codes.add(record.code)

        # ── Append DB-only EKS clusters (in DB but not reflected in AWS scan) ──
        for record in eks_records:
            # Skip when already represented by a scanned cluster, or hidden via isListed=false
            if record.code in matched_eks_codes or _is_unlisted(record):
                continue
            locator = record.locator or {}
            cluster_arn = locator.get("cluster_arn") or ""
            cluster_name = locator.get("cluster_name") or record.name or ""
            vpc_id = locator.get("vpcId") or ""
            cloud_region_id = locator.get("cloudRegionId") or ""
            cloud_region = locator.get("cloudRegion") or ""
            subnet_ids = locator.get("subnetIds") or []
            if not cluster_name:
                continue
            canvas_response.eksClusters.append(
                CanvasEksCluster(
                    clusterArn=cluster_arn,
                    clusterName=cluster_name,
                    vpcId=vpc_id,
                    cloudRegionId=cloud_region_id,
                    cloudRegion=cloud_region,
                    subnetIds=subnet_ids,
                    infrastructureMstCode=record.code,
                    infrastructureMstName=record.name,
                    infraStatus=record.infra_status.value if record.infra_status else None,
                    isRegistered=locator.get("isRegistered", False),
                )
            )

        # ── Append DB-only ECS clusters (in DB but not reflected in AWS scan) ──
        for record in ecs_records:
            # Skip when already represented by a scanned cluster, or hidden via isListed=false
            if record.code in matched_ecs_codes or _is_unlisted(record):
                continue
            locator = record.locator or {}
            cluster_arn = locator.get("cluster_arn") or ""
            cluster_name = locator.get("cluster_name") or record.name or ""
            vpc_id = locator.get("vpcId") or ""
            cloud_region_id = locator.get("cloudRegionId") or ""
            cloud_region = locator.get("cloudRegion") or ""
            subnet_ids = locator.get("subnetIds") or []
            if not cluster_name:
                continue
            canvas_response.ecsClusters.append(
                CanvasEcsCluster(
                    clusterArn=cluster_arn,
                    clusterName=cluster_name,
                    vpcId=vpc_id,
                    cloudRegionId=cloud_region_id,
                    cloudRegion=cloud_region,
                    subnetIds=subnet_ids,
                    infrastructureMstCode=record.code,
                    infrastructureMstName=record.name,
                    infraStatus=record.infra_status.value if record.infra_status else None,
                    isRegistered=locator.get("isRegistered", False),
                )
            )

        # ── Drop unlisted clusters (isListed=false) and their services ──
        # The DB-only appends above already skip them; this sweep removes the ones
        # that came from the AWS scan, plus every service node sitting on them
        # (scanned and DB-draft nodes both carry cluster identity in settings).
        if unlisted_arns or unlisted_eks_names or unlisted_ecs_names:
            canvas_response.eksClusters = [
                c for c in canvas_response.eksClusters
                if c.clusterArn not in unlisted_arns and c.clusterName not in unlisted_eks_names
            ]
            canvas_response.ecsClusters = [
                c for c in canvas_response.ecsClusters
                if c.clusterArn not in unlisted_arns and c.clusterName not in unlisted_ecs_names
            ]

            def _on_unlisted_cluster(node: CanvasNode) -> bool:
                if node.resourceType not in ("service", "model-serving"):
                    return False
                settings = node.settings or {}
                if settings.get("clusterArn") in unlisted_arns:
                    return True
                cluster_name = settings.get("clusterName") or settings.get("ecsClusterName")
                if not cluster_name:
                    return False
                subtype = settings.get("resourceSubtype", "")
                cluster_type = settings.get("clusterType") or (
                    "eks" if "eks" in subtype else "ecs" if "ecs" in subtype else None
                )
                if cluster_type == "eks":
                    return cluster_name in unlisted_eks_names
                if cluster_type == "ecs":
                    return cluster_name in unlisted_ecs_names
                # Unknown type — hide if the name matches either side
                return cluster_name in unlisted_eks_names or cluster_name in unlisted_ecs_names

            canvas_response.nodes = [n for n in canvas_response.nodes if not _on_unlisted_cluster(n)]

    # ─── EKS / ECS Service Enrichment ────────────────────────────────────────

    async def _enrich_service_nodes(
        self,
        nodes: List[CanvasNode],
        tenant_code: str,
        application_code: str,
        environment: str,
        db: AsyncSession,
    ) -> None:
        """
        Enrich EKS/ECS service nodes with matching services_mst + service_config records.

        Adds to settings:
          - serviceCode:  services_mst.code
          - serviceType:  services_mst.service_type
          - configData:   service_configs.config (JSONB) — when cluster name matches
        """
        services_repo = ServicesMstRepository(db)
        config_repo = ServiceConfigRepository(db)
        infra_repo = InfrastructureMstRepository(db)

        # Fetch all services for this app (1 query).
        # Returns {"total": int, "services": [{"service_name": ..., "service_code": ..., "service_type": ..., ...}]}
        # NOTE: get_all_services defaults to limit=100 (newest by created_at). An
        # application can have more than 100 services; any beyond the limit would be
        # absent from the lookup and the scanned node would never match, so it would
        # be re-emitted as a duplicate DRAFT node. Pass a high limit to load all.
        result = await services_repo.get_all_services(
            tenant_code, application_code=application_code, limit=100_000
        )
        services = result.get("services", [])
        service_lookup = {s["service_name"]: s for s in services}

        # Normalized lookup for EKS matching: lowercase and treat '_' / '-' as
        # equivalent, so case/separator differences don't break the match
        # (e.g. "Canopy", "banking_service" → "canopy", "banking-service").
        def _norm(value: str) -> str:
            return value.lower().replace("_", "-")

        norm_lookup = {_norm(s["service_name"]): s for s in services}

        for node in nodes:
            if node.resourceType != "service":
                continue

            service = service_lookup.get(node.name)

            # Fallback: ECS names follow pattern
            # {tenant}-{productname}-{env}-{geolocname}-01-{service_name}-svc
            # Split on the fixed "-01-" separator to extract the bare service name.
            if not service and "-01-" in node.name:
                after_sep = node.name.split("-01-", 1)[1]
                if after_sep.endswith("-svc"):
                    after_sep = after_sep[:-4]
                service = service_lookup.get(after_sep)
                # Fallback: strip trailing "-service" suffix
                # e.g. extracted "ponzim-service" → try "ponzim"
                if not service and after_sep.endswith("-service"):
                    service = service_lookup.get(after_sep[:-8])

            # Fallback (EKS only): k8s deployments are named after the workload,
            # which often carries a "-service"/"_service" suffix that
            # services_mst.name lacks (scanned "alphadesk-api-service" →
            # services_mst "alphadesk-api"). Match on the normalized name, then
            # retry with the suffix stripped. "_service" → "-service" via _norm.
            if not service and "eks" in node.settings.get("resourceSubtype", ""):
                key = _norm(node.name)
                service = norm_lookup.get(key)
                if not service and key.endswith("-service"):
                    service = norm_lookup.get(key[: -len("-service")])

            if not service:
                continue

            node.settings["serviceCode"] = service["service_code"]
            node.settings["serviceType"] = service.get("service_type")

            # Update resourceType for MODEL_SERVING services so frontend routes correctly
            if service.get("service_type") == "MODEL_SERVING":
                node.resourceType = "model-serving"

            # Resolve cluster name (EKS uses clusterName, ECS uses ecsClusterName)
            cluster_name = (
                node.settings.get("clusterName")
                or node.settings.get("ecsClusterName")
            )
            if not cluster_name:
                continue

            # EKS and ECS clusters can share a cluster_name, so restrict the
            # config match to the node's cluster type ("eks"/"ecs").
            resource_subtype = node.settings.get("resourceSubtype", "")
            node_cluster_type = (
                node.settings.get("clusterType")
                or ("eks" if "eks" in resource_subtype else "ecs" if "ecs" in resource_subtype else None)
            )

            # Single JOIN query: service_configs ⋈ infrastructure_mst ON cluster_name
            config = await config_repo.get_by_service_env_and_cluster_name(
                tenant_code=tenant_code,
                service_code=service["service_code"],
                environment=environment,
                cluster_name=cluster_name,
                cluster_type=node_cluster_type,
            )
            if config:
                node.settings["resourceDbCode"] = config.code
                node.settings["deploymentStatus"] = config.deployment_status.value if config.deployment_status else None
                node.settings["configData"] = config.config
                # The row stores the ALB alone; the canvas shows the service URL.
                if isinstance(config.config, dict) and config.config.get("alb_url"):
                    node.settings["albUrl"] = build_service_url(
                        config.config["alb_url"],
                        display_service_path(config.config),
                    )
                node.settings["infraType"] = config.infrastructuretype_ref_code
                node.settings["environment"] = config.environment
                node.settings["geoLocCode"] = config.geo_loc_mst_code
                node.settings["infraVendor"] = config.infra_vendor_enum
                node.settings["infraCluster"] = config.infrastructure_mst_code
                node.settings["languageRefCode"] = config.language_ref_code
                # Set vpcId from saved config only if scan data didn't already provide it
                if not node.settings.get("vpcId") and isinstance(config.config, dict) and config.config.get("vpc_id"):
                    node.settings["vpcId"] = config.config["vpc_id"]
                # Set clusterType from infra type if not already present in scan data
                if not node.settings.get("clusterType"):
                    infra_type = config.infrastructuretype_ref_code.lower()
                    if "eks" in infra_type:
                        node.settings["clusterType"] = "eks"
                    elif "ecs" in infra_type:
                        node.settings["clusterType"] = "ecs"
            else:
                # Fallback: no saved service config — derive geoLocCode/infraType/infraCluster/infraVendor
                # from InfrastructureMstModel by cluster name (mirrors the frontend settings hook).
                infra_matches = await infra_repo.list_by_filters(
                    tenant_code=tenant_code,
                    cluster_name=cluster_name,
                )
                if infra_matches:
                    resource_subtype = node.settings.get("resourceSubtype", "")
                    if "eks" in resource_subtype:
                        infra = next(
                            (i for i in infra_matches if "eks" in i.infrastructuretype_ref_code.lower()),
                            infra_matches[0],
                        )
                    else:
                        infra = next(
                            (i for i in infra_matches if "ecs" in i.infrastructuretype_ref_code.lower()),
                            infra_matches[0],
                        )
                    node.settings["geoLocCode"] = infra.geo_loc_mst_code
                    node.settings["infraType"] = infra.infrastructuretype_ref_code
                    node.settings["infraCluster"] = infra.code
                    node.settings["infraVendor"] = (
                        infra.infra_vendor_account.infra_vendor_enum.value
                        if infra.infra_vendor_account
                        else "aws"
                    )
                    # Set clusterType from infra type if not already present in scan data
                    if not node.settings.get("clusterType"):
                        infra_type = infra.infrastructuretype_ref_code.lower()
                        if "eks" in infra_type:
                            node.settings["clusterType"] = "eks"
                        elif "ecs" in infra_type:
                            node.settings["clusterType"] = "ecs"

        # Bulk-fetch latest queue status for all matched service nodes
        matched_config_codes = [
            node.settings["resourceDbCode"]
            for node in nodes
            if node.resourceType in ("service", "model-serving") and node.settings.get("resourceDbCode")
        ]
        if matched_config_codes:
            svc_queue_map = await TransactionQueueRepository(db).get_latest_by_transaction_codes(
                matched_config_codes, WorkflowSourceTableEnum.SERVICE_CONFIG, tenant_code,
                active_pipeline_only=True,
            )
            for node in nodes:
                if node.resourceType not in ("service", "model-serving"):
                    continue
                db_code = node.settings.get("resourceDbCode")
                if db_code:
                    q = svc_queue_map.get(db_code)
                    if q:
                        node.settings["queueStatus"] = q.status.value if q.status else None
                        node.settings["queueCode"] = q.code

    # ─── Draft Infra Node Appending ──────────────────────────────────────────

    async def _append_draft_infra_nodes(
        self,
        canvas_response: CanvasApiResponse,
        tenant_code: str,
        application_code: str,
        env_enum: EnvironmentEnum,
        db: AsyncSession,
    ) -> None:
        """
        Append "draft/pending" infra nodes (S3, SQS, DynamoDB) for infrastructure_mst
        records that exist in the DB but were not matched to any scanned node.

        Only records with both 'accountId' and 'cloudRegionId' stored in their locator
        JSONB are included — these are records created via the canvas right-click flow
        and contain the placement context needed to position them correctly.
        """
        infra_repo = InfrastructureMstRepository(db)
        queue_repo = TransactionQueueRepository(db)

        # Collect resource DB codes already matched to a scanned node by _enrich_infra_nodes
        matched_db_codes = {
            node.settings["resourceDbCode"]
            for node in canvas_response.nodes
            if node.settings.get("resourceDbCode")
        }

        # infra_type_code → (resourceType, databaseType or None)
        infra_type_map = {
            "s3_infrastructuretype_ref":                  ("bucket",   None),
            "sqs_infrastructuretype_ref":                 ("queue",    None),
            "dynamodb_infrastructuretype_ref":            ("database", "dynamodb"),
            "elasticache_redis_infrastructuretype_ref":     ("database", "redis"),
            "aurora_postgres_infrastructuretype_ref":       ("database", "aurora-postgresql"),
            "aurora_mysql_infrastructuretype_ref":          ("database", "aurora-mysql"),
            "devlift_k8s_postgres_infrastructuretype_ref": ("database", "native-postgres"),
        }

        for infra_type_code, (resource_type, db_type) in infra_type_map.items():
            records = await infra_repo.list_by_filters(
                tenant_code, infra_type_code, env_enum,
                applications_mst_code=application_code,
            )
            logger.info(f"[CANVAS] _append_draft_infra_nodes: type={infra_type_code}, found {len(records)} records, matched_db_codes={matched_db_codes}")

            # Collect unmatched record codes to bulk-fetch their latest queue status
            unmatched_codes = [r.code for r in records if r.code not in matched_db_codes]
            queue_map = await queue_repo.get_latest_by_transaction_codes(
                unmatched_codes, WorkflowSourceTableEnum.INFRASTRUCTURE, tenant_code,
                active_pipeline_only=True,
            )

            for record in records:
                # Skip if already represented by a scanned node
                if record.code in matched_db_codes:
                    continue

                locator = record.locator or {}

                # Only include records that have placement context (set by canvas right-click).
                # AWS-native resources use accountId; k8s-hosted resources use cluster_name.
                # K8s-hosted resources (devlift_k8s_*) may not have these fields — always show them.
                # Subnet IDs in the locator are also a sufficient placement signal (VPC-scoped).
                is_k8s_resource = infra_type_code.startswith("devlift_k8s_")
                account_id = locator.get("accountId") or locator.get("cluster_name") or ""
                cloud_region_id = locator.get("cloudRegionId") or ""
                subnet_ids = (
                    locator.get("subnet_ids")
                    or locator.get("subnetIds")
                    or locator.get("private_subnet_ids")
                    or []
                )
                has_placement = bool(account_id and cloud_region_id)
                if not is_k8s_resource and not has_placement and not subnet_ids:
                    logger.info(f"[CANVAS] Skipping {record.code}: missing placement context (accountId={account_id}, cloudRegionId={cloud_region_id}, subnet_ids={subnet_ids})")
                    continue

                logger.info(f"[CANVAS] Including draft infra node: code={record.code}, type={infra_type_code}, locator_keys={list(locator.keys())}")
                cloud_region = locator.get("cloudRegion") or ""
                name = locator.get("server_name") or locator.get("redis_cluster_name") or locator.get("identifier") or record.name or record.code

                # Read status directly from the resource record
                node_status = _resource_status_value(record)

                # deployStartedAt powers the canvas elapsed timer. Read from
                # the same status_updated_at column that polling uses so the
                # timer survives canvas refetches without relying on the
                # frontend's preserve-on-rebuild logic.
                deploy_started_at = ""
                rs_updated = record.status_updated_at
                if rs_updated:
                    deploy_started_at = rs_updated.isoformat() if hasattr(rs_updated, "isoformat") else str(rs_updated)

                q = queue_map.get(record.code)
                settings: dict = {
                    **locator,
                    "resourceDbCode": record.code,
                    "resourceDbId": record.id,
                    "managedInDb": True,
                    "infraType": record.infrastructuretype_ref_code,
                    "geoLocCode": record.geo_loc_mst_code,
                    "deploymentStatus": record.deployment_status.value if record.deployment_status else None,
                    "iacLockedAt": record.iac_locked_at.isoformat() if record.iac_locked_at else None,
                    **({"deployStartedAt": deploy_started_at} if deploy_started_at else {}),
                    **({"queueStatus": q.status.value if q.status else None, "queueCode": q.code} if q else {}),
                }
                if db_type:
                    settings["databaseType"] = db_type

                canvas_response.nodes.append(
                    CanvasNode(
                        id=record.code,
                        name=name,
                        resourceType=resource_type,
                        status=node_status,
                        vendor=(
                            record.infra_vendor_account.infra_vendor_enum.value
                            if record.infra_vendor_account
                            else locator.get("vendor") or "AWS"
                        ),
                        cloudRegion=cloud_region,
                        cloudRegionId=cloud_region_id,
                        subnetIds=None,
                        position={"x": 0, "y": 0},
                        settings=settings,
                    )
                )

    # ─── Draft Service Node Appending ────────────────────────────────────────

    async def _append_draft_service_nodes(
        self,
        canvas_response: CanvasApiResponse,
        tenant_code: str,
        application_code: str,
        environment: str,
        db: AsyncSession,
    ) -> None:
        """
        Append "draft/pending" service nodes for EKS/ECS service configs that exist
        in the DB but have no matching node in the AWS scan.

        A config is considered draft when no scanned node was matched to its
        services_mst_code during _enrich_service_nodes (i.e., serviceCode not yet
        set on any canvas node).

        Cluster fields are resolved with priority:
          1. service_config.config JSONB
          2. infrastructure_mst.locator (fallback)
        cloud_region_id is constructed as "cr-{account_id}-{region}" when not
        already stored in config JSONB (account_id sourced from locator).
        """
        config_repo = ServiceConfigRepository(db)
        infra_repo = InfrastructureMstRepository(db)
        queue_repo = TransactionQueueRepository(db)

        # Track (service_code, infra_code, geo_loc_code) triples already represented by a
        # scan node. Using all three allows the same service on different clusters or geo
        # locations to each get their own draft node, while still deduplicating when two
        # service_config rows target the exact same (service, cluster, geo) — e.g. rows
        # that differ only in alb_selection.
        matched_combos: set[tuple[str, str, str]] = {
            (
                node.settings["serviceCode"],
                node.settings.get("infraCluster", ""),
                node.settings.get("geoLocCode", ""),
            )
            for node in canvas_response.nodes
            if node.settings.get("serviceCode")
        }
        # Updated within the loop so later configs for the same triple are skipped.
        appended_combos: set[tuple[str, str, str]] = set()

        eks_ecs_configs = await config_repo.get_eks_ecs_service_configs(
            tenant_code, application_code, environment
        )

        # Build (cluster_name, cluster_type) → existing node for vpc_id/subnetIds fallback
        # Composite key prevents clash when EKS and ECS share the same cluster name
        cluster_to_node: dict[tuple[str, str], CanvasNode] = {}
        for node in canvas_response.nodes:
            cn = node.settings.get("clusterName") or ""
            ct = node.settings.get("clusterType") or ""
            if cn and ct:
                cluster_to_node[(cn, ct)] = node

        # Bulk-fetch unique infra records upfront (one query per unique code)
        unique_infra_codes = {
            cfg["infrastructure_mst_code"]
            for cfg in eks_ecs_configs
            if cfg.get("infrastructure_mst_code")
        }
        infra_by_code: dict[str, object] = {}
        for code in unique_infra_codes:
            record = await infra_repo.get_by_code(code)
            if record:
                infra_by_code[code] = record

        # Bulk-fetch latest queue status for unmatched service configs
        unmatched_config_codes = [
            cfg.get("service_config_code", "")
            for cfg in eks_ecs_configs
            if (
                cfg["services_mst_code"],
                cfg.get("infrastructure_mst_code", ""),
                cfg.get("geo_loc_mst_code", ""),
            ) not in matched_combos
            and cfg.get("service_config_code")
        ]
        queue_map = await queue_repo.get_latest_by_transaction_codes(
            unmatched_config_codes, WorkflowSourceTableEnum.SERVICE_CONFIG, tenant_code,
            active_pipeline_only=True,
        )

        for cfg in eks_ecs_configs:
            service_code = cfg["services_mst_code"]
            infra_code = cfg.get("infrastructure_mst_code") or ""
            geo_loc_code = cfg.get("geo_loc_mst_code") or ""
            combo = (service_code, infra_code, geo_loc_code)

            # Skip if already represented by a scan node or appended earlier in this loop
            if combo in matched_combos or combo in appended_combos:
                continue

            config = cfg.get("config") or {}
            infra_record = infra_by_code.get(infra_code)
            locator = (infra_record.locator or {}) if infra_record else {}

            # Determine cluster type first (needed for node lookup key below)
            infra_type = cfg["infrastructuretype_ref_code"]
            is_eks = "eks" in infra_type.lower()
            cluster_type = "eks" if is_eks else "ecs"

            # Resolve cluster fields: config JSONB → infra.locator (primary sources)
            cluster_name = (
                config.get("cluster_name")
                or config.get("cluster")
                or locator.get("cluster_name")
                or ""
            )
            cluster_arn = config.get("cluster_arn") or locator.get("cluster_arn") or ""
            region = config.get("region") or locator.get("region") or ""

            # Fallback: find an existing scan node with the same (cluster_name, cluster_type)
            ref_node = cluster_to_node.get((cluster_name, cluster_type))

            # Apply ref_node fallback for fields still empty after config/locator
            cluster_name = cluster_name or (ref_node.settings.get("clusterName") if ref_node else "") or ""
            cluster_arn = cluster_arn or (ref_node.settings.get("clusterArn") if ref_node else "") or ""
            region = region or (ref_node.cloudRegion if ref_node else "") or ""
            vpc_id = (
                config.get("vpc_id")
                or locator.get("vpc_id")
                or (ref_node.settings.get("vpcId") if ref_node else "")
                or ""
            )
            subnet_ids = (
                config.get("subnet_ids")
                or locator.get("subnet_ids")
                or (ref_node.subnetIds if ref_node else [])
                or []
            )

            # cloud_region_id: config JSONB → construct from locator account_id + region → ref_node
            account_id = locator.get("account_id") or ""
            cloud_region_id = (
                config.get("cloud_region_id")
                or (f"cr-{account_id}-{region}" if account_id and region else "")
                or (ref_node.cloudRegionId if ref_node else "")
            )

            service_name = cfg["service_name"]

            if not cluster_name or not service_name:
                continue

            resource_subtype = "eks_service" if is_eks else "ecs_service"
            launch_type = "" if is_eks else (
                config.get("launch_type") or locator.get("launch_type") or "EC2"
            )
            namespace = (config.get("namespace") or locator.get("namespace") or "default") if is_eks else ""

            # Read status directly from the service config record
            node_status = _resource_status_value(cfg)
            svc_config_code = cfg.get("service_config_code", "")
            deploy_started_at = ""
            # Use status_updated_at as deploy start time for the elapsed timer
            rs_updated = cfg.get("resource_status_updated_at")
            if rs_updated:
                deploy_started_at = rs_updated.isoformat() if hasattr(rs_updated, "isoformat") else str(rs_updated)

            # Use "model-serving" resource type for MODEL_SERVING services
            svc_type = cfg.get("service_type") or config.get("service_type") or ""
            resource_type = "model-serving" if svc_type == "MODEL_SERVING" else "service"

            q = queue_map.get(svc_config_code)
            appended_combos.add(combo)
            canvas_response.nodes.append(
                CanvasNode(
                    id=f"{cluster_arn or infra_code}/{service_code}/{service_name}",
                    name=service_name,
                    resourceType=resource_type,
                    status=node_status,
                    vendor="AWS",
                    cloudRegion=region,
                    cloudRegionId=cloud_region_id,
                    subnetIds=subnet_ids,
                    position={"x": 0, "y": 0},
                    settings={
                        "clusterName": cluster_name,
                        "clusterArn": cluster_arn,
                        "clusterType": cluster_type,
                        "vpcId": vpc_id,
                        "resourceSubtype": resource_subtype,
                        "launchType": launch_type,
                        **({"namespace": namespace} if is_eks else {}),
                        "runningCount": 0,
                        "desiredCount": 0,
                        "aws_status": "",
                        "serviceCode": service_code,
                        # Service type + ALB selection so the canvas can condition correctly
                        # (e.g. workers → no_alb). Mirrors _enrich_service_nodes for scanned nodes.
                        "serviceType": svc_type,
                        "albSelection": config.get("alb_selection"),
                        "resourceDbCode": svc_config_code,
                        "geoLocCode": cfg["geo_loc_mst_code"],
                        "infraType": infra_type,
                        "infraCluster": infra_code,
                        "deploymentStatus": cfg.get("deployment_status").value if cfg.get("deployment_status") else None,
                        "iacLockedAt": cfg.get("iac_locked_at").isoformat() if cfg.get("iac_locked_at") else None,
                        **({"deployStartedAt": deploy_started_at} if deploy_started_at else {}),
                        **({"queueStatus": q.status.value if q.status else None, "queueCode": q.code} if q else {}),
                    },
                )
            )

