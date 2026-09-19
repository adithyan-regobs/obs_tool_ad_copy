"""
Service layer for VPC Discovery operations.
Discovers VPCs, subnets, and resources across AWS accounts.
"""
import os
import re
import logging
import asyncio
from typing import Dict, Any, List, Tuple, Optional

import aioboto3
from botocore.exceptions import ClientError
from botocore.config import Config

from app.integrations.aws_integration import AWSIntegration
from app.schemas.vpc_discovery_schemas import (
    RouteInfo,
    RouteTableInfo,
    ResourceInfo,
    RegionalResourceInfo,
    SubnetInfo,
    SubnetsGrouped,
    SecurityGroupRuleInbound,
    SecurityGroupRuleOutbound,
    SecurityGroupInfo,
    NatGatewayInfo,
    InternetGatewayInfo,
    VPCInfo,
    AWSAccountInfo,
    AWSAccountSummary,
    AccountsListResponse,
    MultiAccountResponse,
    CanvasGeoLocation,
    CanvasAccount,
    CanvasCloudRegion,
    CanvasVpc,
    CanvasSubnet,
    CanvasNode,
    CanvasApiResponse,
    SingleVPCResponse,
)

logger = logging.getLogger(__name__)


class VPCDiscoveryService:
    """
    Service for discovering VPC infrastructure including subnets,
    resources, security groups, and network topology.
    """

    # Batch sizes for multi-level parallelism control
    # (prevents OOM and AWS API rate limiting)
    # Can be overridden via environment variables
    DEFAULT_VPC_BATCH_SIZE = 5         # VPCs per batch in Phase 2
    DEFAULT_SUBNET_BATCH_SIZE = 10     # Subnets per batch within each VPC
    DEFAULT_REGION_BATCH_SIZE = 5      # Regions per batch in Phase 3
    DEFAULT_PHASE1_BATCH_SIZE = 50     # Max concurrent VPC location searches in Phase 1

    def __init__(self, auth_config: Dict[str, Any]):
        """
        Initialize with AWS authentication configuration.

        Args:
            auth_config: AWS auth config from infra_vendor_accounts_mst.auth_config
        """
        self.auth_config = auth_config

    def _get_session_kwargs(self, region: str, account_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Get kwargs for creating aioboto3 session.
        For Lambda compatibility, accepts account_config as parameter.

        Args:
            region: AWS region
            account_config: Account-specific auth config (for multi-account/Lambda)

        Returns:
            Dict of session kwargs for aioboto3.Session()
        """
        # Use provided account_config or fall back to self.auth_config
        if account_config:
            # Multi-account format: extract credentials from account_config
            auth_type = account_config.get("authentication_type", "access_key")

            if auth_type == "iam_role":
                # IAM role assumption - minimal kwargs, will use assume role later
                return {"region_name": region}
            elif auth_type == "access_key":
                # Access key authentication (permanent or temporary credentials)
                kwargs = {
                    "aws_access_key_id": account_config.get("aws_access_key_id"),
                    "aws_secret_access_key": account_config.get("aws_secret_access_key"),
                    "region_name": region,
                }
                # Add session token if present (for temporary credentials)
                if "aws_session_token" in account_config:
                    kwargs["aws_session_token"] = account_config["aws_session_token"]
                return kwargs
            else:
                raise ValueError(f"Unsupported authentication_type: {auth_type}")
        else:
            # Legacy format: use self.auth_config
            config = {**self.auth_config, "region": region}
            config.pop("assume_role_arn", None)

            kwargs = {
                "aws_access_key_id": config.get("aws_access_key_id"),
                "aws_secret_access_key": config.get("aws_secret_access_key"),
                "region_name": region,
            }
            if "aws_session_token" in config:
                kwargs["aws_session_token"] = config["aws_session_token"]
            return kwargs

    def _get_client_kwargs(self, region: str) -> Dict[str, Any]:
        """
        Get kwargs for creating aioboto3 clients.

        Args:
            region: AWS region

        Returns:
            Dict of client kwargs
        """
        return {
            "region_name": region,
            "config": Config(
                signature_version='s3v4',
                retries={
                    'max_attempts': 10,
                    'mode': 'adaptive'
                }
            ),
        }

    def _get_session_kwargs_for_account(
        self, account_id: str, region: str, role_name: str
    ) -> Dict[str, Any]:
        """
        Get session kwargs for cross-account access via AssumeRole.
        Note: AssumeRole is currently disabled - using direct credentials.

        Args:
            account_id: Target AWS account ID
            region: AWS region
            role_name: IAM role name to assume

        Returns:
            Dict of session kwargs for aioboto3.Session()
        """
        # Build assume role ARN (currently unused)
        # assume_role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

        # TODO: Re-enable cross-account AssumeRole when trust policy is configured
        # For now, use direct credentials (works for current account only)
        return self._get_session_kwargs(region)

    @staticmethod
    def _extract_vpc_id(vpc_identifier: str) -> str:
        """
        Extract VPC ID from ARN or return as-is if already an ID.

        Args:
            vpc_identifier: VPC ID (vpc-xxx) or VPC ARN

        Returns:
            VPC ID string

        Raises:
            ValueError: If identifier format is invalid
        """
        if vpc_identifier.startswith("arn:aws:ec2:"):
            match = re.search(r"vpc/(vpc-[a-z0-9]+)", vpc_identifier)
            if match:
                return match.group(1)
            raise ValueError(f"Invalid VPC ARN format: {vpc_identifier}")
        elif vpc_identifier.startswith("vpc-"):
            return vpc_identifier
        else:
            raise ValueError(f"Invalid VPC identifier: {vpc_identifier}")

    @staticmethod
    def _get_name_tag(tags: Optional[List[Dict]]) -> str:
        """
        Extract Name tag from resource tags.

        Args:
            tags: List of AWS tag dicts

        Returns:
            Name tag value or empty string
        """
        if not tags:
            return ""
        for tag in tags:
            if tag.get("Key") == "Name":
                return tag.get("Value", "")
        return ""

    def _is_public_subnet(
        self, subnet_id: str, route_tables: List[Dict]
    ) -> Tuple[bool, Dict]:
        """
        Determine if a subnet is public based on its route table.
        PUBLIC = has route to Internet Gateway (igw-*).

        Args:
            subnet_id: Subnet ID to check
            route_tables: List of route tables in VPC

        Returns:
            Tuple of (is_public, associated_route_table)
        """
        main_rt = None

        for rt in route_tables:
            for assoc in rt.get("Associations", []):
                if assoc.get("SubnetId") == subnet_id:
                    # Found explicit association
                    for route in rt.get("Routes", []):
                        gateway_id = route.get("GatewayId", "")
                        if gateway_id.startswith("igw-"):
                            return True, rt
                    return False, rt
                if assoc.get("Main", False):
                    main_rt = rt

        # Use main route table if no explicit association
        if main_rt:
            for route in main_rt.get("Routes", []):
                gateway_id = route.get("GatewayId", "")
                if gateway_id.startswith("igw-"):
                    return True, main_rt
            return False, main_rt

        return False, {}

    def _format_route_table(self, rt: Dict) -> RouteTableInfo:
        """
        Format route table - only include non-local routes.

        Args:
            rt: Route table dict from AWS API

        Returns:
            RouteTableInfo schema
        """
        routes = []
        for route in rt.get("Routes", []):
            dest = route.get("DestinationCidrBlock") or route.get(
                "DestinationPrefixListId", ""
            )

            # Skip local routes
            if route.get("GatewayId") == "local":
                continue

            # Determine target
            target = ""
            if route.get("GatewayId"):
                target = route["GatewayId"]
            elif route.get("NatGatewayId"):
                target = route["NatGatewayId"]
            elif route.get("TransitGatewayId"):
                target = route["TransitGatewayId"]
            elif route.get("VpcPeeringConnectionId"):
                target = route["VpcPeeringConnectionId"]
            elif route.get("NetworkInterfaceId"):
                target = route["NetworkInterfaceId"]
            else:
                continue

            routes.append(RouteInfo(destination=dest, target=target))

        return RouteTableInfo(rt_id=rt.get("RouteTableId", ""), routes=routes)

    # ==================================================================================
    # Resource Discovery Helper Methods (Parallelized)
    # ==================================================================================

    def _extract_eks_metadata(self, tags: List[Dict]) -> Dict[str, Any]:
        """
        Extract EKS cluster metadata from EC2 instance tags.

        AWS automatically tags EKS-managed nodes with:
        - kubernetes.io/cluster/<cluster-name>: owned
        - eks:cluster-name: <cluster-name>
        - eks:nodegroup-name: <nodegroup-name>

        Args:
            tags: List of EC2 instance tags

        Returns:
            Dict with cluster metadata if this is an EKS node, empty dict otherwise
        """
        settings = {}

        for tag in tags:
            key = tag.get("Key", "")
            value = tag.get("Value", "")

            # Check for Kubernetes cluster tag (standard K8s tag)
            if key.startswith("kubernetes.io/cluster/"):
                cluster_name = key.replace("kubernetes.io/cluster/", "")
                settings["clusterType"] = "eks"
                settings["clusterName"] = cluster_name

            # Alternative: eks:cluster-name tag (AWS EKS specific)
            elif key == "eks:cluster-name":
                settings["clusterType"] = "eks"
                settings["clusterName"] = value

            # Node group name (for grouping nodes within a cluster)
            elif key == "eks:nodegroup-name":
                settings["nodeGroupName"] = value

        return settings

    async def _get_ec2_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get EC2 instances and NAT Gateways in subnet."""
        resources = []
        try:
            # Fetch valid EKS clusters to validate against (prevent ghost clusters)
            valid_eks_clusters = set()
            try:
                async with session.client("eks", **client_kwargs) as eks_client:
                    clusters_response = await eks_client.list_clusters()
                    valid_eks_clusters = set(clusters_response.get("clusters", []))
                    logger.debug(f"Found {len(valid_eks_clusters)} valid EKS clusters in region")
            except ClientError as e:
                logger.warning(f"Failed to fetch EKS clusters for validation: {e}")

            # Build ECS container instance map: { ec2_instance_id -> { clusterName, clusterArn } }
            # list_clusters is lightweight (ARNs only) — O(1) lookup per EC2 instance after this
            # _get_ecs_resources handles service discovery independently (single responsibility)
            ecs_container_instance_map: Dict[str, Dict[str, str]] = {}
            try:
                async with session.client("ecs", **client_kwargs) as ecs_client:
                    clusters_response = await ecs_client.list_clusters()
                    for cluster_arn in clusters_response.get("clusterArns", []):
                        cluster_name = cluster_arn.split("/")[-1] if "/" in cluster_arn else cluster_arn
                        try:
                            ci_arns: List[str] = []
                            paginator = ecs_client.get_paginator("list_container_instances")
                            async for page in paginator.paginate(cluster=cluster_arn):
                                ci_arns.extend(page.get("containerInstanceArns", []))
                            for i in range(0, len(ci_arns), 100):
                                ci_details = await ecs_client.describe_container_instances(
                                    cluster=cluster_arn, containerInstances=ci_arns[i:i + 100]
                                )
                                for ci in ci_details.get("containerInstances", []):
                                    ec2_id = ci.get("ec2InstanceId", "")
                                    if ec2_id:
                                        ecs_container_instance_map[ec2_id] = {
                                            "clusterName": cluster_name,
                                            "clusterArn": cluster_arn,
                                        }
                        except ClientError:
                            continue
                logger.debug(f"Built ECS container instance map: {len(ecs_container_instance_map)} entries")
            except ClientError as e:
                logger.warning(f"Failed to build ECS container instance map: {e}")

            async with session.client("ec2", **client_kwargs) as ec2_client:
                # EC2 Instances
                instances = await ec2_client.describe_instances(
                    Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]
                )
                for reservation in instances.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        if instance.get("State", {}).get("Name") not in ["terminated", "shutting-down"]:
                            tags = instance.get("Tags", [])
                            instance_id = instance["InstanceId"]

                            # Extract EKS cluster metadata from tags
                            eks_metadata = self._extract_eks_metadata(tags)

                            # Validate EKS cluster actually exists (prevent ghost clusters)
                            if eks_metadata and eks_metadata.get("clusterName"):
                                cluster_name = eks_metadata["clusterName"]
                                if cluster_name not in valid_eks_clusters:
                                    logger.info(f"Filtering out ghost EKS cluster '{cluster_name}' from EC2 instance {instance_id}")
                                    eks_metadata = {}  # Clear stale EKS metadata

                            # Build settings — EKS takes priority over ECS
                            settings = eks_metadata if eks_metadata else {}
                            settings["resourceSubtype"] = "ec2"
                            settings["aws_status"] = instance.get("State", {}).get("Name", "running")
                            settings["instanceId"] = instance_id
                            asg_name = next((t["Value"] for t in tags if t.get("Key") == "aws:autoscaling:groupName"), None)
                            if asg_name:
                                settings["asgName"] = asg_name

                            if eks_metadata:
                                settings["vpcId"] = vpc_id
                            else:
                                # Check if this EC2 is an ECS container instance
                                ecs_info = ecs_container_instance_map.get(instance_id)
                                if ecs_info:
                                    settings["ecsClusterName"] = ecs_info["clusterName"]
                                    settings["ecsClusterArn"] = ecs_info["clusterArn"]
                                    settings["vpcId"] = vpc_id

                            resources.append(
                                ResourceInfo(
                                    type="ec2",
                                    resource_id=instance_id,
                                    name=self._get_name_tag(tags),
                                    security_groups=[sg["GroupId"] for sg in instance.get("SecurityGroups", [])],
                                    settings=settings
                                )
                            )

                # NAT Gateways
                nat_gateways = await ec2_client.describe_nat_gateways(
                    Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]
                )
                for nat in nat_gateways.get("NatGateways", []):
                    if nat.get("State") in ["available", "pending"]:
                        resources.append(
                            ResourceInfo(
                                type="nat",
                                resource_id=nat["NatGatewayId"],
                                name=self._get_name_tag(nat.get("Tags", [])) or "NAT Gateway",
                                security_groups=[],
                                settings={"aws_status": nat.get("State", "available")}
                            )
                        )
        except ClientError as e:
            logger.warning(f"Failed to get EC2/NAT resources for subnet {subnet_id}: {e}")
        return resources

    async def _get_rds_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get RDS instances in subnet."""
        resources = []
        try:
            async with session.client("rds", **client_kwargs) as rds_client:
                response = await rds_client.describe_db_instances()
                for db_instance in response.get("DBInstances", []):
                    db_subnet_group = db_instance.get("DBSubnetGroup", {})
                    subnets = db_subnet_group.get("Subnets", [])
                    subnet_ids = [s["SubnetIdentifier"] for s in subnets]

                    if subnet_id in subnet_ids and db_subnet_group.get("VpcId") == vpc_id:
                        resources.append(
                            ResourceInfo(
                                type="rds",
                                resource_id=db_instance["DBInstanceIdentifier"],
                                name=db_instance.get("DBInstanceIdentifier", ""),
                                security_groups=[sg["VpcSecurityGroupId"] for sg in db_instance.get("VpcSecurityGroups", [])],
                                settings={
                                    "databaseType": db_instance.get("Engine", "rds"),
                                    "aws_status": db_instance.get("DBInstanceStatus", "available"),
                                }
                            )
                        )
        except ClientError as e:
            logger.warning(f"Failed to get RDS instances for subnet {subnet_id}: {e}")
        return resources

    async def _get_elb_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get Load Balancers in subnet."""
        resources = []
        try:
            async with session.client("elbv2", **client_kwargs) as elbv2_client:
                lbs_response = await elbv2_client.describe_load_balancers()
                for lb in lbs_response.get("LoadBalancers", []):
                    lb_subnet_ids = [az["SubnetId"] for az in lb.get("AvailabilityZones", [])]

                    if subnet_id in lb_subnet_ids and lb.get("VpcId") == vpc_id:
                        lb_type = lb.get("Type", "application")
                        resource_type = "elb_application" if lb_type == "application" else "elb_network"
                        sgs = lb.get("SecurityGroups", []) if lb_type == "application" else []

                        resources.append(
                            ResourceInfo(
                                type=resource_type,
                                resource_id=lb["LoadBalancerArn"],
                                name=lb.get("LoadBalancerName", ""),
                                security_groups=sgs,
                                settings={"aws_status": lb.get("State", {}).get("Code", "active")}
                            )
                        )
        except ClientError as e:
            logger.warning(f"Failed to get Load Balancers for subnet {subnet_id}: {e}")
        return resources

    async def _get_lambda_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get Lambda functions in subnet."""
        resources = []
        try:
            async with session.client("lambda", **client_kwargs) as lambda_client:
                paginator = lambda_client.get_paginator("list_functions")
                async for page in paginator.paginate():
                    for func in page.get("Functions", []):
                        func_name = func["FunctionName"]
                        func_arn = func["FunctionArn"]

                        try:
                            func_details = await lambda_client.get_function(FunctionName=func_name)
                            vpc_config = func_details.get("Configuration", {}).get("VpcConfig", {})

                            if vpc_config.get("VpcId") == vpc_id and subnet_id in vpc_config.get("SubnetIds", []):
                                resources.append(
                                    ResourceInfo(
                                        type="lambda",
                                        resource_id=func_arn,
                                        name=func_name,
                                        security_groups=vpc_config.get("SecurityGroupIds", []),
                                        settings={}
                                    )
                                )
                        except ClientError:
                            continue
        except ClientError as e:
            logger.warning(f"Failed to get Lambda functions for subnet {subnet_id}: {e}")
        return resources

    async def _get_elasticache_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get ElastiCache Redis/Memcached clusters in subnet."""
        resources = []
        try:
            async with session.client("elasticache", **client_kwargs) as elasticache_client:
                # Get cache clusters
                response = await elasticache_client.describe_cache_clusters()
                for cluster in response.get("CacheClusters", []):
                    # Get cache subnet group details
                    cache_subnet_group_name = cluster.get("CacheSubnetGroupName")
                    if cache_subnet_group_name:
                        subnet_response = await elasticache_client.describe_cache_subnet_groups(
                            CacheSubnetGroupName=cache_subnet_group_name
                        )
                        for subnet_group in subnet_response.get("CacheSubnetGroups", []):
                            subnets = subnet_group.get("Subnets", [])
                            subnet_ids = [s["SubnetIdentifier"] for s in subnets]

                            if subnet_id in subnet_ids and subnet_group.get("VpcId") == vpc_id:
                                # Get security groups
                                security_groups = []
                                if cluster.get("SecurityGroups"):
                                    security_groups = [sg["SecurityGroupId"] for sg in cluster.get("SecurityGroups", [])]

                                resources.append(
                                    ResourceInfo(
                                        type="elasticache",
                                        resource_id=cluster["CacheClusterId"],
                                        name=cluster.get("CacheClusterId", ""),
                                        security_groups=security_groups,
                                        settings={
                                            "databaseType": cluster.get("Engine", "redis"),
                                            "engine": cluster.get("Engine", "redis"),
                                            "engineVersion": cluster.get("EngineVersion", ""),
                                            "cacheNodeType": cluster.get("CacheNodeType", ""),
                                            "aws_status": cluster.get("CacheClusterStatus", "available"),
                                        }
                                    )
                                )
        except ClientError as e:
            logger.warning(f"Failed to get ElastiCache clusters for subnet {subnet_id}: {e}")
        return resources

    async def _get_msk_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """Get MSK (Managed Streaming for Kafka) clusters in subnet."""
        resources = []
        try:
            async with session.client("kafka", **client_kwargs) as kafka_client:
                # List all MSK clusters - list_clusters_v2 returns full cluster info
                response = await kafka_client.list_clusters_v2()
                for cluster_info in response.get("ClusterInfoList", []):
                    cluster_arn = cluster_info.get("ClusterArn")
                    if not cluster_arn:
                        continue

                    # Use cluster_info directly from list_clusters_v2 response
                    # No need for describe_cluster_v2 call which was causing InvalidSignatureException
                    cluster = cluster_info

                    # Initialize subnet and security group lists
                    client_subnets = []
                    security_groups = []
                    cluster_type = cluster.get("ClusterType", "PROVISIONED")

                    # Check if cluster is in the target VPC/subnet
                    # Handle both PROVISIONED and SERVERLESS cluster types
                    if cluster_type == "PROVISIONED":
                        provisioned = cluster.get("Provisioned", {})
                        broker_node_group = provisioned.get("BrokerNodeGroupInfo", {})
                        client_subnets = broker_node_group.get("ClientSubnets", [])
                        security_groups = broker_node_group.get("SecurityGroups", [])
                    elif cluster_type == "SERVERLESS":
                        serverless = cluster.get("Serverless", {})
                        vpc_configs = serverless.get("VpcConfigs", [])
                        # Serverless can have multiple VPC configs, collect all subnets and security groups
                        for vpc_config in vpc_configs:
                            client_subnets.extend(vpc_config.get("SubnetIds", []))
                            security_groups.extend(vpc_config.get("SecurityGroupIds", []))

                    # Check if subnet matches (subnets are unique to VPC, so subnet match = VPC match)
                    if subnet_id in client_subnets:
                        cluster_name = cluster.get("ClusterName", "")

                        # Build settings based on cluster type
                        settings = {
                            "clusterType": cluster_type,
                            "aws_status": cluster.get("State", "ACTIVE"),
                        }

                        if cluster_type == "PROVISIONED":
                            provisioned = cluster.get("Provisioned", {})
                            settings.update({
                                "kafkaVersion": provisioned.get("KafkaVersion", ""),
                                "numberOfBrokerNodes": provisioned.get("NumberOfBrokerNodes", 0),
                            })
                        elif cluster_type == "SERVERLESS":
                            serverless = cluster.get("Serverless", {})
                            client_auth = serverless.get("ClientAuthentication", {})
                            settings.update({
                                "clientAuthentication": list(client_auth.keys()) if client_auth else [],
                            })

                        resources.append(
                            ResourceInfo(
                                type="msk",
                                resource_id=cluster_arn.split("/")[-1] if "/" in cluster_arn else cluster_arn,
                                name=cluster_name,
                                security_groups=security_groups,
                                settings=settings
                            )
                        )
        except ClientError as e:
            logger.warning(f"Failed to get MSK clusters for subnet {subnet_id}: {e}")
        return resources

    async def _get_ecs_resources(
        self, session: aioboto3.Session, client_kwargs: Dict, subnet_id: str, region: str
    ) -> List[ResourceInfo]:
        """Get ECS services in subnet."""
        resources = []
        try:
            async with session.client("ecs", **client_kwargs) as ecs_client:
                clusters_response = await ecs_client.list_clusters()
                clusters = clusters_response.get("clusterArns", [])

                for cluster_arn in clusters:
                    service_arns = []
                    paginator = ecs_client.get_paginator("list_services")
                    async for page in paginator.paginate(cluster=cluster_arn):
                        service_arns.extend(page.get("serviceArns", []))

                    if service_arns:
                        for i in range(0, len(service_arns), 10):
                            batch = service_arns[i:i+10]
                            services_details = await ecs_client.describe_services(cluster=cluster_arn, services=batch)

                            for service in services_details.get("services", []):
                                network_config = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
                                service_subnets = network_config.get("subnets", [])

                                if subnet_id in service_subnets:
                                    cluster_name = cluster_arn.split("/")[-1] if "/" in cluster_arn else cluster_arn
                                    service_name = service.get("serviceName", "")
                                    running_count = service.get("runningCount", 0)

                                    # Resolve launch type — services using capacityProviderStrategy omit launchType
                                    _lt = service.get("launchType", "")
                                    if not _lt:
                                        _cp_names = {cp.get("capacityProvider", "") for cp in service.get("capacityProviderStrategy", [])}
                                        _lt = "EC2" if (_cp_names - {"FARGATE", "FARGATE_SPOT"}) else "FARGATE"

                                    resources.append(
                                        ResourceInfo(
                                            type="ecs_service",
                                            resource_id=f"{cluster_name}/{service_name}",
                                            name=service_name,
                                            security_groups=network_config.get("securityGroups", []),
                                            settings={
                                                "clusterName": cluster_name,
                                                "clusterArn": cluster_arn,
                                                "resourceSubtype": "ecs_service",
                                                "launchType": _lt,
                                                "runningCount": running_count,
                                                "desiredCount": service.get("desiredCount", 0),
                                                "aws_status": "running" if running_count > 0 else "stopped",
                                            }
                                        )
                                    )
        except ClientError as e:
            logger.warning(f"Failed to get ECS services for subnet {subnet_id}: {e}")
        return resources

    async def _get_subnet_resources(
        self, session: aioboto3.Session, region: str, subnet_id: str, vpc_id: str
    ) -> List[ResourceInfo]:
        """
        Get all resources in a subnet (EC2, RDS, ELB, Lambda, ECS, ElastiCache, MSK).
        Queries all services in parallel using asyncio.gather for better performance.

        Args:
            session: aioboto3 Session
            region: AWS region
            subnet_id: Subnet ID
            vpc_id: VPC ID

        Returns:
            List of ResourceInfo
        """
        client_kwargs = self._get_client_kwargs(region)

        # Query all 7 AWS services in parallel
        (
            ec2_resources,
            rds_resources,
            elb_resources,
            lambda_resources,
            ecs_resources,
            elasticache_resources,
            msk_resources,
        ) = await asyncio.gather(
            self._get_ec2_resources(session, client_kwargs, subnet_id, vpc_id),
            self._get_rds_resources(session, client_kwargs, subnet_id, vpc_id),
            self._get_elb_resources(session, client_kwargs, subnet_id, vpc_id),
            self._get_lambda_resources(session, client_kwargs, subnet_id, vpc_id),
            self._get_ecs_resources(session, client_kwargs, subnet_id, region),
            self._get_elasticache_resources(session, client_kwargs, subnet_id, vpc_id),
            self._get_msk_resources(session, client_kwargs, subnet_id, vpc_id),
        )

        # Combine all resources
        return (
            ec2_resources + rds_resources + elb_resources + lambda_resources +
            ecs_resources + elasticache_resources + msk_resources
        )

    # ==================================================================================
    # Regional Resource Discovery Methods (Non-VPC Resources)
    # ==================================================================================

    async def _get_s3_buckets_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """
        Get all S3 buckets in a specific region.

        Note: S3 bucket list API is global, but we filter by region using
        GetBucketLocation to find buckets in this specific region.
        """
        from app.schemas.vpc_discovery_schemas import RegionalResourceInfo

        resources = []
        try:
            client_kwargs = self._get_client_kwargs(region)

            async with session.client("s3", **client_kwargs) as s3_client:
                response = await s3_client.list_buckets()
                buckets = response.get("Buckets", [])

                for bucket in buckets:
                    bucket_name = bucket["Name"]
                    try:
                        location_response = await s3_client.get_bucket_location(
                            Bucket=bucket_name
                        )
                        bucket_region = location_response.get("LocationConstraint")

                        # S3 returns None for us-east-1
                        if bucket_region is None:
                            bucket_region = "us-east-1"

                        if bucket_region == region:
                            resources.append(
                                RegionalResourceInfo(
                                    type="s3",
                                    resource_id=f"arn:aws:s3:::{bucket_name}",
                                    name=bucket_name,
                                    region=region,
                                    settings={}
                                )
                            )
                    except ClientError:
                        continue

        except ClientError as e:
            logger.warning(f"Failed to get S3 buckets in {region}: {e}")

        return resources

    async def _get_dynamodb_tables_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """Get all DynamoDB tables in a specific region."""
        from app.schemas.vpc_discovery_schemas import RegionalResourceInfo

        resources = []
        try:
            client_kwargs = self._get_client_kwargs(region)

            async with session.client("dynamodb", **client_kwargs) as dynamodb_client:
                paginator = dynamodb_client.get_paginator("list_tables")
                async for page in paginator.paginate():
                    for table_name in page.get("TableNames", []):
                        try:
                            table_response = await dynamodb_client.describe_table(
                                TableName=table_name
                            )
                            table_info = table_response.get("Table", {})
                            table_arn = table_info.get("TableArn", "")

                            resources.append(
                                RegionalResourceInfo(
                                    type="dynamodb",
                                    resource_id=table_arn,
                                    name=table_name,
                                    region=region,
                                    settings={
                                        "databaseType": "dynamodb",
                                        "aws_status": table_info.get("TableStatus", "ACTIVE"),
                                    }
                                )
                            )
                        except ClientError:
                            continue

        except ClientError as e:
            logger.warning(f"Failed to get DynamoDB tables in {region}: {e}")

        return resources

    async def _get_lambda_functions_without_vpc_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """
        Get Lambda functions WITHOUT VPC configuration in a specific region.

        Note: Lambda functions WITH VPC are discovered via _get_lambda_resources().
        This method only finds Lambda functions NOT in a VPC.
        """
        from app.schemas.vpc_discovery_schemas import RegionalResourceInfo

        resources = []
        try:
            client_kwargs = self._get_client_kwargs(region)

            async with session.client("lambda", **client_kwargs) as lambda_client:
                paginator = lambda_client.get_paginator("list_functions")
                async for page in paginator.paginate():
                    for func in page.get("Functions", []):
                        func_name = func["FunctionName"]
                        func_arn = func["FunctionArn"]

                        # Only include functions WITHOUT VPC configuration
                        vpc_config = func.get("VpcConfig", {})
                        if not vpc_config.get("VpcId"):
                            resources.append(
                                RegionalResourceInfo(
                                    type="lambda_regional",
                                    resource_id=func_arn,
                                    name=func_name,
                                    region=region,
                                    settings={"aws_status": func.get("State", "Active")}
                                )
                            )

        except ClientError as e:
            logger.warning(f"Failed to get Lambda functions in {region}: {e}")

        return resources

    async def _get_sqs_queues_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """Get all SQS queues in a specific region."""
        from app.schemas.vpc_discovery_schemas import RegionalResourceInfo

        resources = []
        try:
            client_kwargs = self._get_client_kwargs(region)

            async with session.client("sqs", **client_kwargs) as sqs_client:
                paginator = sqs_client.get_paginator("list_queues")
                async for page in paginator.paginate():
                    for queue_url in page.get("QueueUrls", []):
                        try:
                            attrs_response = await sqs_client.get_queue_attributes(
                                QueueUrl=queue_url,
                                AttributeNames=["QueueArn"]
                            )
                            queue_arn = attrs_response.get("Attributes", {}).get("QueueArn", "")
                            queue_name = queue_url.split("/")[-1]

                            resources.append(
                                RegionalResourceInfo(
                                    type="sqs",
                                    resource_id=queue_arn,
                                    name=queue_name,
                                    region=region,
                                    settings={}
                                )
                            )
                        except ClientError:
                            continue

        except ClientError as e:
            logger.warning(f"Failed to get SQS queues in {region}: {e}")

        return resources

    async def _get_cloudfront_distributions_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """
        Get all CloudFront distributions.

        Note: CloudFront is a global service, but we query it once per region
        to avoid duplicate results. Distributions are only listed when querying
        the us-east-1 region.
        """
        from app.schemas.vpc_discovery_schemas import RegionalResourceInfo

        resources = []

        # CloudFront is global, only query from us-east-1 to avoid duplicates
        if region != "us-east-1":
            return resources

        try:
            # CloudFront API is only available in us-east-1
            client_kwargs = self._get_client_kwargs("us-east-1")

            async with session.client("cloudfront", **client_kwargs) as cf_client:
                paginator = cf_client.get_paginator("list_distributions")
                async for page in paginator.paginate():
                    distribution_list = page.get("DistributionList", {})
                    for dist in distribution_list.get("Items", []):
                        dist_id = dist.get("Id", "")
                        dist_domain = dist.get("DomainName", "")

                        # Get aliases (CNAMEs)
                        aliases = dist.get("Aliases", {}).get("Items", [])
                        alias_str = ", ".join(aliases) if aliases else dist_domain

                        resources.append(
                            RegionalResourceInfo(
                                type="cloudfront",
                                resource_id=dist_id,
                                name=alias_str,
                                region="us-east-1",
                                settings={
                                    "domainName": dist_domain,
                                    "enabled": dist.get("Enabled", False),
                                    "aws_status": dist.get("Status", "Deployed"),
                                    "priceClass": dist.get("PriceClass", "PriceClass_All"),
                                }
                            )
                        )

        except ClientError as e:
            logger.warning(f"Failed to get CloudFront distributions: {e}")

        return resources

    async def _get_regional_resources_in_region(
        self, session: aioboto3.Session, region: str
    ) -> List:
        """
        Get all regional resources (S3, DynamoDB, Lambda without VPC, SQS, CloudFront) in a region.
        Queries all 5 services in parallel using asyncio.gather for performance.
        """
        # Query all 5 AWS services in parallel
        (
            s3_resources,
            dynamodb_resources,
            lambda_resources,
            sqs_resources,
            cloudfront_resources,
        ) = await asyncio.gather(
            self._get_s3_buckets_in_region(session, region),
            self._get_dynamodb_tables_in_region(session, region),
            self._get_lambda_functions_without_vpc_in_region(session, region),
            self._get_sqs_queues_in_region(session, region),
            self._get_cloudfront_distributions_in_region(session, region),
            return_exceptions=True
        )

        # Handle errors gracefully and combine results
        all_resources = []
        for resource_list in [s3_resources, dynamodb_resources, lambda_resources, sqs_resources, cloudfront_resources]:
            if isinstance(resource_list, list):
                all_resources.extend(resource_list)
            elif isinstance(resource_list, Exception):
                logger.warning(f"Error fetching regional resources in {region}: {resource_list}")

        return all_resources

    def _format_security_groups(
        self, sgs: List[Dict]
    ) -> List[SecurityGroupInfo]:
        """
        Format security groups to match expected output.

        Args:
            sgs: List of security group dicts from AWS API

        Returns:
            List of SecurityGroupInfo schemas
        """
        result = []

        for sg in sgs:
            inbound = []
            outbound = []

            # Process inbound rules
            for rule in sg.get("IpPermissions", []):
                protocol = rule.get("IpProtocol", "-1")
                from_port = rule.get("FromPort", 0)

                for ip_range in rule.get("IpRanges", []):
                    inbound.append(
                        SecurityGroupRuleInbound(
                            protocol=protocol,
                            port=from_port,
                            source=ip_range.get("CidrIp"),
                        )
                    )

                for sg_pair in rule.get("UserIdGroupPairs", []):
                    inbound.append(
                        SecurityGroupRuleInbound(
                            protocol=protocol,
                            port=from_port,
                            source_security_group=sg_pair.get("GroupId"),
                        )
                    )

            # Process outbound rules
            for rule in sg.get("IpPermissionsEgress", []):
                protocol = rule.get("IpProtocol", "-1")

                for ip_range in rule.get("IpRanges", []):
                    outbound.append(
                        SecurityGroupRuleOutbound(
                            protocol=protocol,
                            destination=ip_range.get("CidrIp"),
                        )
                    )

            result.append(
                SecurityGroupInfo(
                    sg_id=sg["GroupId"],
                    name=sg.get("GroupName", ""),
                    inbound=inbound,
                    outbound=outbound,
                )
            )

        return result

    async def _get_vpc_info(
        self, session: aioboto3.Session, vpc_id: str, region: str
    ) -> VPCInfo:
        """
        Get complete VPC details including subnets and resources.

        Args:
            session: aioboto3 Session
            vpc_id: VPC ID
            region: AWS region

        Returns:
            VPCInfo schema

        Raises:
            Exception: If VPC not found
        """
        client_kwargs = self._get_client_kwargs(region)

        async with session.client("ec2", **client_kwargs) as ec2_client:
            # Query all VPC resources in parallel for better performance
            (
                vpc_response,
                route_tables_response,
                igws_response,
                nats_response,
                subnets_response,
                sgs_response,
            ) = await asyncio.gather(
                ec2_client.describe_vpcs(VpcIds=[vpc_id]),
                ec2_client.describe_route_tables(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                ),
                ec2_client.describe_internet_gateways(
                    Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
                ),
                ec2_client.describe_nat_gateways(
                    Filters=[
                        {"Name": "vpc-id", "Values": [vpc_id]},
                        {"Name": "state", "Values": ["available"]},
                    ]
                ),
                ec2_client.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                ),
                ec2_client.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                ),
            )

            # Validate VPC exists
            if not vpc_response.get("Vpcs"):
                raise Exception(f"VPC {vpc_id} not found")
            vpc = vpc_response["Vpcs"][0]

            # Process route tables
            route_tables = route_tables_response.get("RouteTables", [])

            # Process Internet Gateway
            igw_info = None
            igws = igws_response.get("InternetGateways", [])
            if igws:
                igw_info = InternetGatewayInfo(
                    igw_id=igws[0]["InternetGatewayId"], attached=True
                )

            # Process NAT Gateways
            nat_gateways = []
            nats = nats_response.get("NatGateways", [])
            for nat in nats:
                elastic_ip = None
                for addr in nat.get("NatGatewayAddresses", []):
                    elastic_ip = addr.get("PublicIp")
                nat_gateways.append(
                    NatGatewayInfo(
                        nat_id=nat["NatGatewayId"],
                        subnet_id=nat.get("SubnetId", ""),
                        elastic_ip=elastic_ip,
                    )
                )

            # Process Security Groups
            sgs = sgs_response.get("SecurityGroups", [])

        # Process subnets in batches (outside ec2 context manager)
        # Batching prevents OOM and API throttling with large subnet counts
        SUBNET_BATCH_SIZE = int(os.environ.get('SUBNET_BATCH_SIZE', self.DEFAULT_SUBNET_BATCH_SIZE))

        all_subnets = subnets_response.get("Subnets", [])
        subnet_metadata = []  # Store subnet info for processing
        subnet_resources_list = []  # Store all subnet resources

        # Prepare subnet metadata
        for subnet in all_subnets:
            subnet_id = subnet["SubnetId"]
            is_public, route_table = self._is_public_subnet(subnet_id, route_tables)

            subnet_metadata.append({
                "subnet": subnet,
                "subnet_id": subnet_id,
                "is_public": is_public,
                "route_table": route_table,
            })

        # Process subnets in batches
        total_subnet_batches = (len(all_subnets) + SUBNET_BATCH_SIZE - 1) // SUBNET_BATCH_SIZE
        logger.info(f"Processing {len(all_subnets)} subnets in {total_subnet_batches} batches (batch size: {SUBNET_BATCH_SIZE})")

        for batch_idx in range(0, len(all_subnets), SUBNET_BATCH_SIZE):
            batch = all_subnets[batch_idx:batch_idx + SUBNET_BATCH_SIZE]
            batch_num = (batch_idx // SUBNET_BATCH_SIZE) + 1

            # Create tasks for this batch of subnets
            subnet_tasks = [
                self._get_subnet_resources(session, region, subnet["SubnetId"], vpc_id)
                for subnet in batch
            ]

            # Fetch resources for subnets in this batch
            batch_resources = await asyncio.gather(*subnet_tasks, return_exceptions=True)
            subnet_resources_list.extend(batch_resources)

            logger.debug(f"Subnet batch {batch_num}/{total_subnet_batches} complete")

        # Build subnet info objects from results
        public_subnets = []
        private_subnets = []

        for metadata, resources in zip(subnet_metadata, subnet_resources_list):
            # Handle errors gracefully
            if isinstance(resources, Exception):
                logger.warning(f"Error fetching resources for subnet {metadata['subnet_id']}: {resources}")
                resources = []

            # Extract subnet name from tags
            subnet_name = self._get_name_tag(metadata["subnet"].get("Tags", []))

            subnet_info = SubnetInfo(
                subnet_id=metadata["subnet_id"],
                name=subnet_name,
                az=metadata["subnet"].get("AvailabilityZone", ""),
                cidr=metadata["subnet"].get("CidrBlock", ""),
                route_table=self._format_route_table(metadata["route_table"]),
                resources=resources,
            )

            if metadata["is_public"]:
                public_subnets.append(subnet_info)
            else:
                private_subnets.append(subnet_info)

        # Sort by AZ
        public_subnets.sort(key=lambda x: x.az)
        private_subnets.sort(key=lambda x: x.az)

        return VPCInfo(
            vpc_id=vpc_id,
            region=region,
            cidr=vpc.get("CidrBlock", ""),
            name=self._get_name_tag(vpc.get("Tags", [])),
            internet_gateway=igw_info,
            nat_gateways=nat_gateways,
            subnets=SubnetsGrouped(public=public_subnets, private=private_subnets),
            security_groups=self._format_security_groups(sgs),
        )

    async def get_vpc_details(self, vpc_identifier: str, region: str) -> SingleVPCResponse:
        """
        Get all subnets and resources for a single VPC.

        Args:
            vpc_identifier: VPC ID (vpc-xxx) or VPC ARN
            region: AWS region where the VPC is located

        Returns:
            SingleVPCResponse with complete VPC details

        Raises:
            ValueError: If VPC identifier is invalid
            Exception: If VPC not found or AWS API error
        """
        vpc_id = self._extract_vpc_id(vpc_identifier)
        session_kwargs = self._get_session_kwargs(region)
        session = aioboto3.Session(**session_kwargs)
        vpc_info = await self._get_vpc_info(session, vpc_id, region)
        return SingleVPCResponse(**vpc_info.model_dump())

    async def get_vpc_by_account(
        self,
        account_id: str,
        vpc_id: str,
        region: str,
        role_name: str = "OrganizationAccountAccessRole",
    ) -> SingleVPCResponse:
        """
        Get VPC details from a specific account using cross-account role assumption.

        Args:
            account_id: Target AWS account ID
            vpc_id: VPC ID
            region: AWS region
            role_name: IAM role name for cross-account access

        Returns:
            SingleVPCResponse with VPC details
        """
        # TODO: Re-enable cross-account AssumeRole when trust policy is configured
        # For now, use direct credentials (works for current account only)
        session_kwargs = self._get_session_kwargs_for_account(account_id, region, role_name)
        session = aioboto3.Session(**session_kwargs)
        vpc_info = await self._get_vpc_info(session, vpc_id, region)
        return SingleVPCResponse(**vpc_info.model_dump())

    async def get_all_accounts_vpcs(
        self,
        account_ids: List[str],
        regions: List[str],
        role_name: str = "OrganizationAccountAccessRole",
    ) -> MultiAccountResponse:
        """
        Get all VPCs across multiple accounts and regions.

        Args:
            account_ids: List of AWS Account IDs
            regions: List of AWS regions to scan
            role_name: IAM role name for cross-account access

        Returns:
            MultiAccountResponse with all accounts and their VPCs
        """
        result = []

        for account_id in account_ids:
            account_vpcs = []

            for region in regions:
                try:
                    # TODO: Re-enable cross-account AssumeRole when trust policy is configured
                    # For now, use direct credentials (works for current account only)
                    session_kwargs = self._get_session_kwargs(region)
                    session = aioboto3.Session(**session_kwargs)
                    client_kwargs = self._get_client_kwargs(region)

                    # Get all VPCs in this region
                    async with session.client("ec2", **client_kwargs) as ec2_client:
                        vpcs_response = await ec2_client.describe_vpcs()
                        vpcs = vpcs_response.get("Vpcs", [])

                    for vpc in vpcs:
                        vpc_id = vpc["VpcId"]
                        try:
                            vpc_info = await self._get_vpc_info(session, vpc_id, region)
                            account_vpcs.append(vpc_info)
                        except Exception as e:
                            logger.warning(
                                f"Failed to get VPC {vpc_id} in {account_id}/{region}: {e}"
                            )
                            continue

                except ClientError as e:
                    logger.warning(
                        f"Failed to access account {account_id} in region {region}: {e}"
                    )
                    continue

            # Get account name from Organizations if available
            account_name = f"account-{account_id}"
            try:
                org_region = regions[0] if regions else "us-east-1"
                org_session_kwargs = self._get_session_kwargs(org_region)
                org_session = aioboto3.Session(**org_session_kwargs)
                org_client_kwargs = self._get_client_kwargs(org_region)

                async with org_session.client("organizations", **org_client_kwargs) as org_client:
                    account_info = await org_client.describe_account(AccountId=account_id)
                    account_name = account_info.get("Account", {}).get("Name", account_name)
            except Exception:
                pass

            result.append(
                AWSAccountInfo(
                    account_id=account_id,
                    account_name=account_name,
                    vpcs=account_vpcs,
                )
            )

        return MultiAccountResponse(aws_account=result)

    async def list_accounts(self) -> AccountsListResponse:
        """
        List all AWS accounts from AWS Organizations.

        Returns:
            AccountsListResponse with list of accounts (no VPC details)

        Raises:
            Exception: If not part of AWS Organizations or access denied
        """
        accounts = []

        try:
            # Organizations API is global, use us-east-1
            session_kwargs = self._get_session_kwargs("us-east-1")
            session = aioboto3.Session(**session_kwargs)
            client_kwargs = self._get_client_kwargs("us-east-1")

            async with session.client("organizations", **client_kwargs) as org_client:
                # Paginate through all accounts
                paginator = org_client.get_paginator("list_accounts")
                async for page in paginator.paginate():
                    for account in page.get("Accounts", []):
                        accounts.append(
                            AWSAccountSummary(
                                account_id=account.get("Id", ""),
                                account_name=account.get("Name", ""),
                                email=account.get("Email"),
                                status=account.get("Status"),
                            )
                        )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "AWSOrganizationsNotInUseException":
                raise Exception(
                    "This AWS account is not part of an AWS Organization. "
                    "To list accounts, you must be using AWS Organizations."
                )
            elif error_code == "AccessDeniedException":
                raise Exception(
                    "Access denied to AWS Organizations. "
                    "Ensure your credentials have organizations:ListAccounts permission."
                )
            else:
                logger.error(f"Failed to list accounts: {e}")
                raise Exception(f"Failed to list AWS accounts: {str(e)}")

        return AccountsListResponse(accounts=accounts, total=len(accounts))

    @staticmethod
    def extract_account_id_from_auth_config(auth_config: dict) -> Optional[str]:
        """
        Extract account ID from auth_config.

        Tries:
        1. Direct 'account_id' field
        2. Parse from 'assume_role_arn' (format: arn:aws:iam::ACCOUNT_ID:role/...)

        Args:
            auth_config: Auth config dict from infra_vendor_accounts_mst

        Returns:
            Account ID string or None if not found
        """
        if not auth_config:
            return None

        # Try direct account_id field
        if auth_config.get("account_id"):
            return str(auth_config["account_id"])

        # Try to extract from assume_role_arn
        assume_role_arn = auth_config.get("assume_role_arn")
        if assume_role_arn:
            # ARN format: arn:aws:iam::123456789012:role/RoleName
            match = re.search(r"arn:aws:iam::(\d+):role/", assume_role_arn)
            if match:
                return match.group(1)

        return None

    @staticmethod
    def get_unique_account_ids(
        vendor_accounts: List[Any]
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """
        Get unique account IDs from vendor accounts by extracting from auth_config.

        Args:
            vendor_accounts: List of InfraVendorAccountsMstModel instances

        Returns:
            Tuple of (unique_account_ids, accounts_details)
            where accounts_details contains unique account_id with vendor
        """
        unique_account_ids = set()
        seen_accounts = {}  # Track unique account_id + vendor combinations

        for vendor_account in vendor_accounts:
            account_id = VPCDiscoveryService.extract_account_id_from_auth_config(
                vendor_account.auth_config
            )
            if account_id:
                unique_account_ids.add(account_id)
                # Only add to accounts_list if we haven't seen this account_id + vendor combo
                key = f"{account_id}_{vendor_account.infra_vendor_enum.value}"
                if key not in seen_accounts:
                    seen_accounts[key] = {
                        "account_id": account_id,
                        "vendor": vendor_account.infra_vendor_enum.value,
                    }

        return list(unique_account_ids), list(seen_accounts.values())

    # ==================================================================================
    # NEW V2 METHODS - Proper Authentication with Priority Chain
    # ==================================================================================
    # Authentication Priority:
    # 1. IAM Role + AssumeRole (if assume_role_arn exists)
    # 2. IAM Role (if authentication_type = "iam_role")
    # 3. Access Keys (fallback if authentication_type = "access_key")
    # ==================================================================================

    async def _get_session_kwargs_v2(self, region: str, account_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Get session kwargs using proper authentication priority (V2).
        For Lambda compatibility, accepts account_config as parameter.

        This method uses AWSIntegration's authentication logic which follows:
        1. IAM Role + AssumeRole (if assume_role_arn in config)
        2. IAM Role (if authentication_type = "iam_role")
        3. Access Keys (fallback if authentication_type = "access_key")

        Args:
            region: AWS region
            account_config: Account-specific auth config (for multi-account/Lambda)

        Returns:
            Dict of session kwargs for aioboto3.Session()
        """
        # Use provided account_config or fall back to self.auth_config
        if account_config:
            # Multi-account format: pass account_config with region
            config_with_region = {**account_config, "region": region}
        else:
            # Legacy format: use self.auth_config
            config_with_region = {**self.auth_config, "region": region}

        return await AWSIntegration._get_client_kwargs(config_with_region)

    async def _get_enabled_regions(self, account_config: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        Get all enabled AWS regions for the current account.
        For Lambda compatibility, accepts account_config as parameter.

        Dynamically fetches regions using describe_regions API instead of
        hardcoding. This ensures all enabled regions are checked and
        automatically includes new AWS regions as they launch.

        Args:
            account_config: Account-specific auth config (for multi-account/Lambda)

        Returns:
            List of region names (e.g., ['us-east-1', 'eu-west-1', ...])
        """
        try:
            # Use us-east-1 as entry point (AWS best practice for global operations)
            session_kwargs = await self._get_session_kwargs_v2("us-east-1", account_config)
            session = aioboto3.Session(**session_kwargs)
            client_kwargs = self._get_client_kwargs("us-east-1")

            async with session.client("ec2", **client_kwargs) as ec2_client:
                # Get only enabled regions (AllRegions=False)
                # This returns regions with OptInStatus: 'opt-in-not-required' or 'opted-in'
                response = await ec2_client.describe_regions(AllRegions=False)

                # Extract region names from response
                regions = [
                    region["RegionName"]
                    for region in response.get("Regions", [])
                ]

                logger.info(f"Discovered {len(regions)} enabled regions for account")
                return regions

        except Exception as e:
            logger.warning(
                f"Failed to fetch regions dynamically: {e}. "
                f"Falling back to common regions list."
            )
            # Fallback to common regions if describe_regions fails
            return [
                "us-east-1", "us-east-2", "us-west-1", "us-west-2",
                "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2",
                "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
                "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1",
                "ca-central-1", "sa-east-1", "me-central-1"
            ]

    async def _find_vpc_in_region(
        self, vpc_id: str, region: str, session: aioboto3.Session, account_config: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, str]]:
        """
        Lightweight check if a VPC exists in a specific region.
        Uses a shared session to avoid creating multiple boto3 sessions.

        This is a fast check that only calls describe_vpcs to verify existence.
        Does NOT fetch full VPC details (subnets, resources, etc).

        Args:
            vpc_id: VPC ID to search for
            region: AWS region to check
            session: Shared aioboto3 session for this region (prevents OOM)
            account_config: Account-specific auth config (for multi-account/Lambda)

        Returns:
            Dict with vpc_id and region if found, None if not found or error
        """
        try:
            client_kwargs = self._get_client_kwargs(region)

            async with session.client("ec2", **client_kwargs) as ec2_client:
                # Lightweight check - just verify VPC exists
                response = await ec2_client.describe_vpcs(VpcIds=[vpc_id])

                if response.get("Vpcs"):
                    # Found the VPC in this region!
                    logger.info(f"✓ Found VPC {vpc_id} in region {region}")
                    return {
                        "vpc_id": vpc_id,
                        "region": region
                    }

                return None

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "InvalidVpcID.NotFound":
                # VPC not in this region (expected, not an error)
                return None
            else:
                logger.warning(f"Error checking VPC {vpc_id} in {region}: {e}")
                return None
        except Exception as e:
            logger.warning(f"Error scanning region {region} for VPC {vpc_id}: {e}")
            return None

    async def _fetch_vpc_details(
        self, vpc_id: str, region: str, account_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Fetch full VPC details including subnets, resources, and metadata.
        For Lambda compatibility, accepts account_config as parameter.

        This is a heavyweight operation that queries multiple AWS services.
        Should only be called after confirming VPC exists in the region.

        Args:
            vpc_id: VPC ID to fetch details for
            region: AWS region where VPC exists
            account_config: Account-specific auth config (for multi-account/Lambda)

        Returns:
            Dict with vpc_info and account_id
        """
        session_kwargs = await self._get_session_kwargs_v2(region, account_config)
        session = aioboto3.Session(**session_kwargs)
        client_kwargs = self._get_client_kwargs(region)

        # Get full VPC details
        vpc_info = await self._get_vpc_info(session, vpc_id, region)

        # Get account ID
        async with session.client("sts", **client_kwargs) as sts_client:
            identity = await sts_client.get_caller_identity()
            account_id = identity["Account"]

        return {
            "vpc_id": vpc_id,
            "region": region,
            "vpc_info": vpc_info,
            "account_id": account_id
        }

    async def discover_vpcs_multi_account(
        self, auth_config: Dict[str, Any], accounts_vpcs: Dict[str, List[str]], application_code: str
    ) -> MultiAccountResponse:
        """
        Discover VPCs across multiple AWS accounts using per-account assume roles.

        **Multi-Account Architecture:**
        - Lambda IAM Role → AssumeRole(account1_assume_role_arn) → Scan VPCs in account1
        - Lambda IAM Role → AssumeRole(account2_assume_role_arn) → Scan VPCs in account2
        - All accounts are processed in parallel for optimal performance

        **Auth Config Required:**
        ```json
        {
            "authentication_type": "iam_role",
            "accounts": [
                {
                    "account_id": "123",
                    "region": "us-east-1",
                    "assume_role_arn": "arn:aws:iam::123:role/AssumeRole"
                }
            ]
        }
        ```

        **Per-Account Discovery (THREE-PHASE parallel execution):**

        Phase 1 (Lightweight): Find VPC locations
        - Scans all regions concurrently to find where VPCs exist
        - Just checks VPC existence (fast - describe_vpcs only)
        - Cancels remaining tasks as soon as all VPCs are found
        - Expected: ~5-7 seconds per account

        Phase 2 (Heavyweight): Fetch VPC details
        - Fetches full details for found VPCs in parallel
        - Queries subnets, resources, security groups, etc.
        - Expected: ~10-15 seconds per account

        Phase 3: Regional Resources
        - Fetches S3, DynamoDB, Lambda (no VPC), SQS
        - Expected: ~2-3 seconds per account

        Total expected performance: ~17-25 seconds per account (parallelized across accounts)

        Args:
            auth_config: AWS authentication configuration with accounts array
            accounts_vpcs: Dict of account_id -> list of vpc_ids
                Example: {"597189966628": ["vpc-12345"], "123456789012": ["vpc-abc12"]}
            application_code: Application identifier

        Returns:
            MultiAccountResponse with discovered VPC details across all accounts
        """
        logger.info(f"🚀 Starting multi-account VPC discovery for {len(accounts_vpcs)} accounts")

        # Validate auth_config structure
        if "accounts" not in auth_config or not isinstance(auth_config["accounts"], list):
            raise ValueError("auth_config must contain 'accounts' array")

        # Build account config lookup
        account_configs = {acc["account_id"]: acc for acc in auth_config["accounts"]}

        # Process each account in parallel
        account_tasks = []
        for account_id, vpc_ids in accounts_vpcs.items():
            # Find account config
            account_config = account_configs.get(account_id)
            if not account_config:
                logger.warning(f"No auth config found for account {account_id}, skipping")
                continue

            # Create task for this account
            task = self._discover_vpcs_for_account(
                account_id=account_id,
                account_config=account_config,
                vpc_ids=vpc_ids,
                application_code=application_code
            )
            account_tasks.append(task)

        # Wait for all accounts to complete
        logger.info(f"Processing {len(account_tasks)} accounts in parallel")
        account_results = await asyncio.gather(*account_tasks, return_exceptions=True)

        # Collect all successful account results
        all_accounts = []
        for result in account_results:
            if isinstance(result, AWSAccountInfo):
                all_accounts.append(result)
            elif isinstance(result, Exception):
                logger.error(f"Account discovery failed: {result}")

        logger.info(f"✅ Multi-account discovery complete: {len(all_accounts)} accounts processed successfully")

        return MultiAccountResponse(aws_account=all_accounts)

    async def _discover_vpcs_for_account(
        self,
        account_id: str,
        account_config: Dict[str, Any],
        vpc_ids: List[str],
        application_code: str
    ) -> AWSAccountInfo:
        """
        Discover VPCs for a single AWS account (THREE-PHASE execution).

        Args:
            account_id: AWS account ID
            account_config: Account-specific auth config with assume_role_arn
            vpc_ids: List of VPC IDs to discover
            application_code: Application identifier

        Returns:
            AWSAccountInfo with discovered VPCs and regional resources
        """
        logger.info(f"🔍 [{account_id}] Starting discovery for {len(vpc_ids)} VPCs")

        # Dynamically fetch all enabled regions for this account
        # Pass account_config directly for Lambda compatibility
        regions = await self._get_enabled_regions(account_config)

        # ==================================================================================
        # PHASE 1: Find VPCs (Session-Reuse Architecture)
        # ==================================================================================
        # OPTIMIZATION: Create ONE session per region, reuse for all VPC searches
        # This reduces memory from 425 sessions to 17 sessions (95% reduction!)
        found_vpcs = []  # List of {vpc_id, region}
        remaining_vpc_ids = set(vpc_ids)

        total_searches = len(vpc_ids) * len(regions)
        logger.info(f"[{account_id}] 🔍 Phase 1: Scanning {len(vpc_ids)} VPCs across {len(regions)} regions ({total_searches} searches)")

        # Step 1: Create one shared session per region (memory-efficient!)
        logger.info(f"[{account_id}] Creating {len(regions)} shared sessions (one per region)...")
        sessions_by_region = {}
        for region in regions:
            session_kwargs = await self._get_session_kwargs_v2(region, account_config)
            sessions_by_region[region] = aioboto3.Session(**session_kwargs)

        logger.info(f"[{account_id}] ✓ Sessions created. Starting VPC discovery...")

        # Step 2: Create tasks that reuse shared sessions
        find_tasks = []
        for region in regions:
            session = sessions_by_region[region]  # Reuse session!
            for vpc_id in vpc_ids:
                task = asyncio.create_task(
                    self._find_vpc_in_region(vpc_id, region, session, account_config)
                )
                find_tasks.append(task)

        # Process find tasks as they complete
        try:
            for completed_task in asyncio.as_completed(find_tasks):
                result = await completed_task

                if result:
                    # Found a VPC location!
                    vpc_id = result["vpc_id"]
                    region = result["region"]
                    found_vpcs.append(result)

                    # Remove from remaining VPCs
                    if vpc_id in remaining_vpc_ids:
                        remaining_vpc_ids.remove(vpc_id)
                        logger.info(
                            f"[{account_id}] Located: {len(found_vpcs)}/{len(vpc_ids)} VPCs. "
                            f"Remaining: {len(remaining_vpc_ids)}"
                        )

                    # 🎯 EARLY EXIT: All VPCs found!
                    if not remaining_vpc_ids:
                        pending_tasks = [t for t in find_tasks if not t.done()]
                        logger.info(
                            f"[{account_id}] ✅ All VPCs located! Cancelling {len(pending_tasks)} remaining scan tasks"
                        )

                        # Cancel all pending tasks
                        for task in pending_tasks:
                            task.cancel()

                        # Break out of loop
                        break

        except asyncio.CancelledError:
            pass

        # Wait for cleanup
        await asyncio.gather(*find_tasks, return_exceptions=True)

        logger.info(f"[{account_id}] Phase 1 complete: Found {len(found_vpcs)} VPCs in {len(set(v['region'] for v in found_vpcs))} regions")

        # ==================================================================================
        # PHASE 2: Fetch VPC Details (Heavyweight - batched parallel fetching)
        # ==================================================================================
        # Process VPCs in batches to:
        # 1. Avoid Lambda OOM errors with large VPC counts
        # 2. Reduce AWS API rate limiting / throttling risks
        # 3. Provide better progress visibility
        BATCH_SIZE = int(os.environ.get('VPC_BATCH_SIZE', self.DEFAULT_VPC_BATCH_SIZE))

        total_batches = (len(found_vpcs) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(f"[{account_id}] 📦 Phase 2: Fetching full details for {len(found_vpcs)} VPCs in {total_batches} batches (batch size: {BATCH_SIZE})")

        discovered_vpcs = []

        # Process VPCs in batches
        for batch_idx in range(0, len(found_vpcs), BATCH_SIZE):
            batch = found_vpcs[batch_idx:batch_idx + BATCH_SIZE]
            batch_num = (batch_idx // BATCH_SIZE) + 1

            logger.info(f"[{account_id}] Processing batch {batch_num}/{total_batches}: {len(batch)} VPCs ({', '.join(v['vpc_id'] for v in batch)})")

            # Fetch details for VPCs in this batch (parallel within batch)
            detail_tasks = [
                self._fetch_vpc_details(vpc["vpc_id"], vpc["region"], account_config)
                for vpc in batch
            ]

            vpc_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

            # Process batch results
            for detail in vpc_details:
                if isinstance(detail, dict) and "vpc_info" in detail:
                    discovered_vpcs.append(detail["vpc_info"])
                elif isinstance(detail, Exception):
                    logger.error(f"[{account_id}] Error fetching VPC details in batch {batch_num}: {detail}")

            logger.info(f"[{account_id}] ✓ Batch {batch_num}/{total_batches} complete: {len(discovered_vpcs)} VPCs discovered so far")

        # Get account name
        account_name = "unknown"
        try:
            session_kwargs = await self._get_session_kwargs_v2("us-east-1", account_config)
            session = aioboto3.Session(**session_kwargs)
            client_kwargs = self._get_client_kwargs("us-east-1")

            async with session.client("organizations", **client_kwargs) as org_client:
                account_info = await org_client.describe_account(AccountId=account_id)
                account_name = account_info.get("Account", {}).get("Name", f"account-{account_id}")
        except Exception:
            account_name = f"account-{account_id}"

        logger.info(f"[{account_id}] 🏁 Discovery complete: Found {len(discovered_vpcs)}/{len(vpc_ids)} VPCs with full details")

        # ==================================================================================
        # PHASE 3: Fetch Regional Resources (S3, DynamoDB, Lambda without VPC, SQS)
        # ==================================================================================
        # Process regions in batches to prevent API throttling
        REGION_BATCH_SIZE = int(os.environ.get('REGION_BATCH_SIZE', self.DEFAULT_REGION_BATCH_SIZE))

        total_region_batches = (len(regions) + REGION_BATCH_SIZE - 1) // REGION_BATCH_SIZE
        logger.info(f"[{account_id}] 🌍 Phase 3: Fetching regional resources across {len(regions)} regions in {total_region_batches} batches (batch size: {REGION_BATCH_SIZE})")

        # Create session for regional resource discovery
        session_kwargs = await self._get_session_kwargs_v2(
            regions[0] if regions else "us-east-1",
            account_config
        )
        session = aioboto3.Session(**session_kwargs)

        # Fetch regional resources in batches
        all_regional_resources = []

        for batch_idx in range(0, len(regions), REGION_BATCH_SIZE):
            batch = regions[batch_idx:batch_idx + REGION_BATCH_SIZE]
            batch_num = (batch_idx // REGION_BATCH_SIZE) + 1

            logger.info(f"[{account_id}] Processing region batch {batch_num}/{total_region_batches}: {', '.join(batch)}")

            # Fetch resources for regions in this batch (parallel within batch)
            regional_tasks = [
                self._get_regional_resources_in_region(session, region)
                for region in batch
            ]

            regional_resources_by_region = await asyncio.gather(*regional_tasks, return_exceptions=True)

            # Flatten results from this batch
            for resources in regional_resources_by_region:
                if isinstance(resources, list):
                    all_regional_resources.extend(resources)
                elif isinstance(resources, Exception):
                    logger.warning(f"[{account_id}] Error fetching regional resources in batch {batch_num}: {resources}")

            logger.info(f"[{account_id}] ✓ Region batch {batch_num}/{total_region_batches} complete: {len(all_regional_resources)} regional resources found so far")

        logger.info(f"[{account_id}] ✅ Phase 3 complete: Found {len(all_regional_resources)} regional resources across {len(regions)} regions")

        return AWSAccountInfo(
            account_id=account_id,
            account_name=account_name,
            vpcs=discovered_vpcs,
            regional_resources=all_regional_resources
        )

    @staticmethod
    def transform_to_canvas_response(multi_account_response: MultiAccountResponse) -> CanvasApiResponse:
        """
        Transform MultiAccountResponse to CanvasApiResponse for frontend visualization.

        Converts nested VPC discovery data into flat, normalized structure with:
        - geoLocations: Geographic regions derived from AWS regions
        - accounts: Cloud provider accounts
        - cloudRegions: Regions within accounts
        - vpcs: VPCs with references to cloud regions
        - subnets: Subnets with references to VPCs
        - nodes: Resources discovered in subnets

        Args:
            multi_account_response: Nested VPC discovery response

        Returns:
            CanvasApiResponse with flat, normalized structure
        """
        # Region to geo location mapping
        REGION_TO_GEO = {
            "us-east-1": "North America",
            "us-east-2": "North America",
            "us-west-1": "North America",
            "us-west-2": "North America",
            "ca-central-1": "North America",
            "sa-east-1": "South America",
            "eu-west-1": "Europe",
            "eu-west-2": "Europe",
            "eu-west-3": "Europe",
            "eu-central-1": "Europe",
            "eu-north-1": "Europe",
            "ap-south-1": "India",
            "ap-south-2": "India",
            "ap-northeast-1": "East Asia",
            "ap-northeast-2": "East Asia",
            "ap-northeast-3": "East Asia",
            "ap-southeast-1": "Southeast Asia",
            "ap-southeast-2": "Australia",
            "me-south-1": "Middle East",
            "me-central-1": "Middle East",
            "af-south-1": "Africa",
        }

        # Region display names
        REGION_DISPLAY_NAMES = {
            "us-east-1": "us-east-1 (N. Virginia)",
            "us-east-2": "us-east-2 (Ohio)",
            "us-west-1": "us-west-1 (N. California)",
            "us-west-2": "us-west-2 (Oregon)",
            "ca-central-1": "ca-central-1 (Canada)",
            "sa-east-1": "sa-east-1 (São Paulo)",
            "eu-west-1": "eu-west-1 (Ireland)",
            "eu-west-2": "eu-west-2 (London)",
            "eu-west-3": "eu-west-3 (Paris)",
            "eu-central-1": "eu-central-1 (Frankfurt)",
            "eu-north-1": "eu-north-1 (Stockholm)",
            "ap-south-1": "ap-south-1 (Mumbai)",
            "ap-south-2": "ap-south-2 (Hyderabad)",
            "ap-northeast-1": "ap-northeast-1 (Tokyo)",
            "ap-northeast-2": "ap-northeast-2 (Seoul)",
            "ap-northeast-3": "ap-northeast-3 (Osaka)",
            "ap-southeast-1": "ap-southeast-1 (Singapore)",
            "ap-southeast-2": "ap-southeast-2 (Sydney)",
            "me-south-1": "me-south-1 (Bahrain)",
            "me-central-1": "me-central-1 (UAE)",
            "af-south-1": "af-south-1 (Cape Town)",
        }

        # Map resource types to frontend types
        RESOURCE_TYPE_MAP = {
            "ec2": "service",
            "rds": "database",
            "elb_application": "elb_application",
            "elb_network": "elb_network",
            "lambda": "function",
            "ecs_service": "service",
            "nat": "nat",
            "elasticache": "database",
            "msk": "stream",
            # Regional resource types
            "s3": "bucket",
            "dynamodb": "database",
            "lambda_regional": "function",
            "sqs": "queue",
            "cloudfront": "cdn",
        }

        # Map AWS raw status → (canvas status, statusText)
        STATUS_MAP = {
            # EC2 instance states
            "running": ("online", "Running"),
            "pending": ("warning", "Pending"),
            "stopping": ("warning", "Stopping"),
            "stopped": ("offline", "Stopped"),
            "terminated": ("offline", "Terminated"),
            # RDS instance status
            "available": ("online", "Available"),
            "backing-up": ("warning", "Backing Up"),
            "creating": ("warning", "Creating"),
            "modifying": ("warning", "Modifying"),
            "rebooting": ("warning", "Rebooting"),
            "starting": ("warning", "Starting"),
            # ECS service status
            "ACTIVE": ("online", "Active"),
            "DRAINING": ("warning", "Draining"),
            "INACTIVE": ("offline", "Inactive"),
            # ELB / NAT state
            "active": ("online", "Active"),
            "provisioning": ("warning", "Provisioning"),
            "failed": ("offline", "Failed"),
            # Lambda function state
            "Active": ("online", "Active"),
            "Inactive": ("offline", "Inactive"),
            "Pending": ("warning", "Pending"),
            "Failed": ("offline", "Failed"),
            # DynamoDB table status
            "ACTIVE": ("online", "Active"),
            "CREATING": ("warning", "Creating"),
            "UPDATING": ("warning", "Updating"),
            "DELETING": ("warning", "Deleting"),
            "ARCHIVING": ("warning", "Archiving"),
            "ARCHIVED": ("offline", "Archived"),
        }

        geo_locations = []
        accounts = []
        cloud_regions = []
        vpcs = []
        subnets = []
        nodes = []

        geo_location_ids = {}
        account_ids_map = {}
        cloud_region_ids_map = {}

        # Track unique geolocations
        seen_geos = set()
        # Track unique account+geo combinations
        seen_account_geos = set()

        for account in multi_account_response.aws_account:
            for vpc in account.vpcs:
                # Determine geo location from region
                geo_name = REGION_TO_GEO.get(vpc.region, "Other")
                geo_id = f"geo-{geo_name.lower().replace(' ', '-')}"

                # Add geo location if not seen
                if geo_id not in seen_geos:
                    seen_geos.add(geo_id)
                    geo_locations.append(CanvasGeoLocation(
                        id=geo_id,
                        name=geo_name,
                        displayName=geo_name,
                        position={"x": len(geo_locations) * 600, "y": 0},
                        settings={}
                    ))
                    geo_location_ids[geo_name] = geo_id

                # Create account instance per geo location (account can span multiple geos)
                account_geo_key = f"{account.account_id}:{geo_id}"
                if account_geo_key not in seen_account_geos:
                    seen_account_geos.add(account_geo_key)
                    account_canvas_id = f"account-{account.account_id}-{geo_id}"
                    account_ids_map[account_geo_key] = account_canvas_id
                    accounts.append(CanvasAccount(
                        id=account_canvas_id,
                        accountId=account.account_id,
                        vendor="AWS",
                        geoLocationId=geo_id,
                        position={"x": 30, "y": 48 + len(accounts) * 300},
                        settings={}
                    ))
                else:
                    account_canvas_id = account_ids_map[account_geo_key]

                # Add cloud region
                cloud_region_id = f"cr-{account.account_id}-{vpc.region}"
                cloud_region_ids_map[f"{account.account_id}:{vpc.region}"] = cloud_region_id

                if cloud_region_id not in [cr.id for cr in cloud_regions]:
                    cloud_regions.append(CanvasCloudRegion(
                        id=cloud_region_id,
                        name=vpc.region,
                        displayName=REGION_DISPLAY_NAMES.get(vpc.region, vpc.region),
                        accountId=account_canvas_id,
                        settings={}
                    ))

                # Add VPC
                vpcs.append(CanvasVpc(
                    id=vpc.vpc_id,
                    name=vpc.name or vpc.vpc_id,
                    cloudRegionId=cloud_region_id,
                    cidr=vpc.cidr,
                    settings={}
                ))

                # Add subnets
                display_order = 0
                for subnet_type, subnet_list in [("public", vpc.subnets.public), ("private", vpc.subnets.private)]:
                    for subnet in subnet_list:
                        # Use actual subnet name if available, otherwise fallback to generated name
                        subnet_name = subnet.name if subnet.name else f"{subnet_type}-{subnet.az}"

                        subnets.append(CanvasSubnet(
                            id=subnet.subnet_id,
                            name=subnet_name,
                            az=subnet.az,
                            type=subnet_type,
                            cidr=subnet.cidr,
                            vpcId=vpc.vpc_id,
                            displayOrder=display_order
                        ))
                        display_order += 1

                        # Add resources as nodes (deduplicate by resource_id)
                        for resource in subnet.resources:
                            resource_type = RESOURCE_TYPE_MAP.get(resource.type, "service")

                            # Check if node already exists
                            existing_node = next((n for n in nodes if n.id == resource.resource_id), None)

                            if existing_node:
                                # Node exists, append subnet_id if not already present
                                if subnet.subnet_id not in existing_node.subnetIds:
                                    existing_node.subnetIds.append(subnet.subnet_id)
                            else:
                                # New node, add it
                                aws_status = resource.settings.get("aws_status", "")
                                canvas_status, canvas_status_text = STATUS_MAP.get(aws_status, ("online", "Online"))
                                nodes.append(CanvasNode(
                                    id=resource.resource_id,
                                    name=resource.name,
                                    resourceType=resource_type,
                                    status=canvas_status,
                                    statusText=canvas_status_text,
                                    vendor="AWS",
                                    cloudRegion=vpc.region,
                                    cloudRegionId=cloud_region_id,
                                    subnetIds=[subnet.subnet_id],
                                    position={"x": 0, "y": 0},
                                    settings=resource.settings  # Include EKS cluster metadata and other settings
                                ))

        # Process regional resources (not VPC-specific)
        for account in multi_account_response.aws_account:
            for regional_resource in account.regional_resources:
                # Determine geo location from region
                geo_name = REGION_TO_GEO.get(regional_resource.region, "Other")
                geo_id = f"geo-{geo_name.lower().replace(' ', '-')}"

                # Ensure geo location exists
                if geo_id not in seen_geos:
                    seen_geos.add(geo_id)
                    geo_locations.append(CanvasGeoLocation(
                        id=geo_id,
                        name=geo_name,
                        displayName=geo_name,
                        position={"x": len(geo_locations) * 600, "y": 0},
                        settings={}
                    ))
                    geo_location_ids[geo_name] = geo_id

                # Ensure account instance exists for this geo
                account_geo_key = f"{account.account_id}:{geo_id}"
                if account_geo_key not in seen_account_geos:
                    seen_account_geos.add(account_geo_key)
                    account_canvas_id = f"account-{account.account_id}-{geo_id}"
                    account_ids_map[account_geo_key] = account_canvas_id
                    accounts.append(CanvasAccount(
                        id=account_canvas_id,
                        accountId=account.account_id,
                        vendor="AWS",
                        geoLocationId=geo_id,
                        position={"x": 30, "y": 48 + len(accounts) * 300},
                        settings={}
                    ))
                else:
                    account_canvas_id = account_ids_map[account_geo_key]

                # Ensure cloud region exists
                cloud_region_id = f"cr-{account.account_id}-{regional_resource.region}"
                cloud_region_key = f"{account.account_id}:{regional_resource.region}"

                if cloud_region_id not in [cr.id for cr in cloud_regions]:
                    cloud_regions.append(CanvasCloudRegion(
                        id=cloud_region_id,
                        name=regional_resource.region,
                        displayName=REGION_DISPLAY_NAMES.get(regional_resource.region, regional_resource.region),
                        accountId=account_canvas_id,
                        settings={}
                    ))
                    cloud_region_ids_map[cloud_region_key] = cloud_region_id

                # Map resource type
                resource_type = RESOURCE_TYPE_MAP.get(regional_resource.type, "service")

                # Create canvas node for regional resource (subnetIds = None)
                nodes.append(CanvasNode(
                    id=regional_resource.resource_id,
                    name=regional_resource.name,
                    resourceType=resource_type,
                    status="online",
                    statusText="Online",
                    vendor="AWS",
                    cloudRegion=regional_resource.region,
                    cloudRegionId=cloud_region_id,
                    subnetIds=None,  # Regional resources have no subnet association
                    position={"x": 0, "y": 0},
                    settings=regional_resource.settings
                ))

        return CanvasApiResponse(
            geoLocations=geo_locations,
            accounts=accounts,
            cloudRegions=cloud_regions,
            vpcs=vpcs,
            subnets=subnets,
            nodes=nodes,
            securityGroupRules=[]
        )
