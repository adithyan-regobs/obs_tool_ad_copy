"""
Pydantic schemas for VPC Discovery API
Provides models for AWS VPC, subnet, and resource discovery responses.
"""
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class RouteInfo(BaseModel):
    """Route entry in a route table"""
    destination: str = Field(..., description="Route destination CIDR block")
    target: str = Field(..., description="Route target (igw, nat, tgw, pcx, etc)")


class RouteTableInfo(BaseModel):
    """Route table with associated routes"""
    rt_id: str = Field(..., description="Route table ID")
    routes: List[RouteInfo] = Field(default=[], description="Non-local routes")


class ResourceInfo(BaseModel):
    """Resource discovered in a subnet"""
    type: str = Field(..., description="Resource type (ec2, nat, elb_application, elb_network, rds, lambda, ecs_task)")
    resource_id: str = Field(..., description="Resource identifier")
    name: str = Field(..., description="Resource name from tags")
    security_groups: List[str] = Field(default=[], description="Attached security group IDs")
    settings: dict = Field(default_factory=dict, description="Resource-specific settings (e.g., EKS cluster metadata)")


class RegionalResourceInfo(BaseModel):
    """Regional resource discovered (not VPC-specific)"""
    type: str = Field(..., description="Resource type (s3, dynamodb, lambda_regional, sqs)")
    resource_id: str = Field(..., description="Resource identifier (ARN)")
    name: str = Field(..., description="Resource name")
    region: str = Field(..., description="AWS region where resource exists")
    settings: dict = Field(default_factory=dict, description="Resource-specific settings")


class SubnetInfo(BaseModel):
    """Subnet details with route table and resources"""
    subnet_id: str = Field(..., description="Subnet ID")
    name: str = Field(..., description="Subnet name from tags")
    az: str = Field(..., description="Availability zone")
    cidr: str = Field(..., description="Subnet CIDR block")
    route_table: RouteTableInfo = Field(..., description="Associated route table")
    resources: List[ResourceInfo] = Field(default=[], description="Resources in this subnet")


class SubnetsGrouped(BaseModel):
    """Subnets grouped by public/private classification"""
    public: List[SubnetInfo] = Field(default=[], description="Public subnets (have IGW route)")
    private: List[SubnetInfo] = Field(default=[], description="Private subnets (no IGW route)")


class SecurityGroupRuleInbound(BaseModel):
    """Inbound security group rule"""
    protocol: str = Field(..., description="Protocol (tcp, udp, icmp, -1 for all)")
    port: int = Field(..., description="Port number (0 for all)")
    source: Optional[str] = Field(None, description="Source CIDR block")
    source_security_group: Optional[str] = Field(None, description="Source security group ID")


class SecurityGroupRuleOutbound(BaseModel):
    """Outbound security group rule"""
    protocol: str = Field(..., description="Protocol (tcp, udp, icmp, -1 for all)")
    destination: Optional[str] = Field(None, description="Destination CIDR block")


class SecurityGroupInfo(BaseModel):
    """Security group with inbound and outbound rules"""
    sg_id: str = Field(..., description="Security group ID")
    name: str = Field(..., description="Security group name")
    inbound: List[SecurityGroupRuleInbound] = Field(default=[], description="Inbound rules")
    outbound: List[SecurityGroupRuleOutbound] = Field(default=[], description="Outbound rules")


class NatGatewayInfo(BaseModel):
    """NAT Gateway details"""
    nat_id: str = Field(..., description="NAT Gateway ID")
    subnet_id: str = Field(..., description="Subnet where NAT Gateway resides")
    elastic_ip: Optional[str] = Field(None, description="Elastic IP address")


class InternetGatewayInfo(BaseModel):
    """Internet Gateway details"""
    igw_id: str = Field(..., description="Internet Gateway ID")
    attached: bool = Field(..., description="Whether attached to VPC")


class VPCInfo(BaseModel):
    """Complete VPC details including subnets and resources"""
    vpc_id: str = Field(..., description="VPC ID")
    region: str = Field(..., description="AWS region")
    cidr: str = Field(..., description="VPC CIDR block")
    name: str = Field(..., description="VPC name from tags")
    internet_gateway: Optional[InternetGatewayInfo] = Field(None, description="Attached Internet Gateway")
    nat_gateways: List[NatGatewayInfo] = Field(default=[], description="NAT Gateways in VPC")
    subnets: SubnetsGrouped = Field(..., description="Subnets grouped by public/private")
    security_groups: List[SecurityGroupInfo] = Field(default=[], description="Security groups in VPC")


class AWSAccountSummary(BaseModel):
    """AWS account summary (lightweight - no VPC details)"""
    account_id: str = Field(..., description="AWS Account ID")
    account_name: str = Field(..., description="AWS Account name")
    email: Optional[str] = Field(None, description="Account email")
    status: Optional[str] = Field(None, description="Account status (ACTIVE, SUSPENDED)")


class AccountsListResponse(BaseModel):
    """Response for listing AWS accounts"""
    accounts: List[AWSAccountSummary] = Field(default=[], description="List of AWS accounts")
    total: int = Field(..., description="Total number of accounts")


class AWSAccountInfo(BaseModel):
    """AWS account with its VPCs (full details)"""
    account_id: str = Field(..., description="AWS Account ID")
    account_name: str = Field(..., description="AWS Account name")
    vpcs: List[VPCInfo] = Field(default=[], description="VPCs in this account")
    regional_resources: List[RegionalResourceInfo] = Field(default=[], description="Regional resources (not VPC-specific)")
    eks_workloads: Dict[str, Dict] = Field(default_factory=dict, description="EKS workloads by cluster name (pods, deployments, services, etc.)")


class MultiAccountResponse(BaseModel):
    """Response for multi-account VPC discovery"""
    aws_account: List[AWSAccountInfo] = Field(default=[], description="List of AWS accounts with VPCs")


class SingleVPCResponse(VPCInfo):
    """Response for single VPC discovery - same as VPCInfo"""
    pass


class AccountIdInfo(BaseModel):
    """Account ID extracted from vendor account configuration"""
    account_id: str = Field(..., description="Cloud provider account ID")
    vendor: str = Field(..., description="Infrastructure vendor (aws, azure, gcp)")


class AccountIdsResponse(BaseModel):
    """Response for listing unique account IDs"""
    account_ids: List[str] = Field(default=[], description="List of unique account IDs")
    accounts: List[AccountIdInfo] = Field(default=[], description="Account details with vendor and environment")
    total: int = Field(..., description="Total number of unique account IDs")


# ==================================================================================
# Canvas API Response Schemas (for frontend visualization)
# ==================================================================================


class CanvasGeoLocation(BaseModel):
    """Geographic location for canvas visualization"""
    id: str = Field(..., description="Unique geo location ID")
    name: str = Field(..., description="Geo location name (e.g., India, Europe)")
    displayName: str = Field(..., description="Display name for geo location")
    position: dict = Field(default_factory=lambda: {"x": 0, "y": 0}, description="Canvas position")
    settings: dict = Field(default_factory=dict, description="Additional settings")


class CanvasAccount(BaseModel):
    """Account for canvas visualization"""
    id: str = Field(..., description="Unique account ID for canvas")
    accountId: str = Field(..., description="Cloud provider account ID")
    vendor: str = Field(..., description="Vendor (AWS, GCP, Azure)")
    geoLocationId: str = Field(..., description="Reference to geo location")
    position: dict = Field(default_factory=lambda: {"x": 30, "y": 48}, description="Canvas position")
    settings: dict = Field(default_factory=dict, description="Additional settings")


class CanvasCloudRegion(BaseModel):
    """Cloud region for canvas visualization"""
    id: str = Field(..., description="Unique cloud region ID for canvas")
    name: str = Field(..., description="Region name (e.g., ap-south-1)")
    displayName: str = Field(..., description="Display name (e.g., ap-south-1 (Mumbai))")
    accountId: str = Field(..., description="Reference to account ID")
    settings: dict = Field(default_factory=dict, description="Additional settings")


class CanvasVpc(BaseModel):
    """VPC for canvas visualization"""
    id: str = Field(..., description="VPC ID")
    name: str = Field(..., description="VPC name")
    cloudRegionId: str = Field(..., description="Reference to cloud region ID")
    cidr: str = Field(..., description="VPC CIDR block")
    settings: dict = Field(default_factory=dict, description="Additional settings")


class CanvasSubnet(BaseModel):
    """Subnet for canvas visualization"""
    id: str = Field(..., description="Subnet ID")
    name: str = Field(..., description="Subnet name")
    az: str = Field(..., description="Availability zone")
    type: str = Field(..., description="Subnet type (public or private)")
    cidr: str = Field(..., description="Subnet CIDR block")
    vpcId: str = Field(..., description="Reference to VPC ID")
    displayOrder: int = Field(..., description="Display order in canvas")


class CanvasNode(BaseModel):
    """Resource node for canvas visualization"""
    id: str = Field(..., description="Unique node ID")
    name: str = Field(..., description="Resource name")
    resourceType: str = Field(..., description="Resource type (service, database, etc)")
    status: str = Field(default="online", description="Resource status")
    statusText: str = Field(default="Online", description="Status text")
    vendor: str = Field(..., description="Vendor (AWS, GCP, Azure)")
    cloudRegion: str = Field(..., description="Cloud region name")
    cloudRegionId: str = Field(..., description="Reference to cloud region ID")
    subnetIds: Optional[List[str]] = Field(default=None, description="Subnet IDs where resource resides")
    position: dict = Field(default_factory=lambda: {"x": 0, "y": 0}, description="Canvas position")
    settings: dict = Field(default_factory=dict, description="Additional resource settings")


class CanvasEksCluster(BaseModel):
    """EKS cluster extracted as a first-class canvas entity"""
    clusterArn: str = Field(..., description="EKS cluster ARN (unique key)")
    clusterName: str = Field(..., description="EKS cluster name")
    vpcId: str = Field(..., description="VPC the cluster resides in")
    cloudRegionId: str = Field(..., description="Reference to cloud region ID")
    cloudRegion: str = Field(..., description="AWS region name")
    subnetIds: List[str] = Field(default=[], description="Subnet IDs from cluster resourcesVpcConfig")


class CanvasEcsCluster(BaseModel):
    """ECS cluster extracted as a first-class canvas entity"""
    clusterArn: str = Field(..., description="ECS cluster ARN (unique key)")
    clusterName: str = Field(..., description="ECS cluster name")
    vpcId: str = Field(..., description="VPC the cluster resides in")
    cloudRegionId: str = Field(..., description="Reference to cloud region ID")
    cloudRegion: str = Field(..., description="AWS region name")
    subnetIds: List[str] = Field(default=[], description="Subnet IDs aggregated from services")


class CanvasApiResponse(BaseModel):
    """Canvas API response with flat, normalized structure"""
    geoLocations: List[CanvasGeoLocation] = Field(default=[], description="Geographic locations")
    accounts: List[CanvasAccount] = Field(default=[], description="Cloud provider accounts")
    cloudRegions: List[CanvasCloudRegion] = Field(default=[], description="Cloud regions")
    vpcs: List[CanvasVpc] = Field(default=[], description="VPCs")
    subnets: List[CanvasSubnet] = Field(default=[], description="Subnets")
    eksClusters: List[CanvasEksCluster] = Field(default=[], description="EKS clusters")
    ecsClusters: List[CanvasEcsCluster] = Field(default=[], description="ECS clusters")
    nodes: List[CanvasNode] = Field(default=[], description="Resource nodes")
    nodeVariables: dict = Field(default_factory=dict, description="Node variables (empty for now)")
    variableRefs: dict = Field(default_factory=dict, description="Variable references (empty for now)")
    securityGroupRules: List = Field(default=[], description="Security group rules (empty for now)")
