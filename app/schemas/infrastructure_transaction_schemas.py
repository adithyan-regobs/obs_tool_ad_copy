"""
Infrastructure Transaction Schemas

Pydantic models for infrastructure transactions API matching frontend TypeScript interfaces.
Supports unified view of infrastructure_mst, kong_route_configs, and alert_configs.
"""
from typing import Optional, List, Union, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


# ============================================================================
# Enums
# ============================================================================

class TransactionTypeEnum(str, Enum):
    """Transaction type enum matching frontend"""
    INFRASTRUCTURE = "infrastructure"
    KONG_ROUTE = "kong-route"
    ALERT_CONFIG = "alert-config"


class TransactionTabEnum(str, Enum):
    """Tab filter enum for API requests"""
    ALL = "all"
    INFRASTRUCTURE = "infrastructure"
    KONG_ROUTE = "kong_route"
    ALERT_CONFIG = "alert_config"


class DeploymentStatusEnum(str, Enum):
    """Deployment status enum"""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"
    PR_CREATED = "PR_CREATED"
    INITIATED = "INITIATED"


# ============================================================================
# Request Schemas
# ============================================================================

class GetInfraTransactionsRequest(BaseModel):
    """
    Request schema for fetching infrastructure transactions.

    Supports tab-based filtering and pagination.
    """
    tab: TransactionTabEnum = Field(
        default=TransactionTabEnum.ALL,
        description="Tab filter: all, infrastructure, kong_route, alert_config"
    )
    status: Optional[str] = Field(
        None,
        description="Filter by deployment status (SUCCESS, FAILED, IN_PROGRESS, etc.)"
    )
    skip: int = Field(
        default=0,
        ge=0,
        description="Pagination offset"
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Page size (max 100)"
    )
    sort_order: str = Field(
        default="desc",
        description="Sort order: 'asc' or 'desc' (by created_at)"
    )


# ============================================================================
# GitOps Workflow Detail Schema
# ============================================================================

class GitopsWorkflowDetailResponse(BaseModel):
    """
    GitOps workflow detail response matching frontend GitopsWorkflowDetail interface.
    """
    id: int
    code: str
    name: str
    gitRepository: Optional[str] = None
    gitBranch: Optional[str] = None
    gitCommitSha: Optional[str] = None
    prNumber: Optional[int] = None
    prUrl: Optional[str] = None
    workflowRunId: Optional[str] = None
    workflowRunUrl: Optional[str] = None
    workflowRunOutputs: Optional[Dict[str, Any]] = None
    runInitiatedAt: Optional[str] = None
    runCompletedAt: Optional[str] = None

    class Config:
        from_attributes = True


# ============================================================================
# Transaction Response Schemas
# ============================================================================

class BaseTransactionResponse(BaseModel):
    """
    Base transaction response matching frontend BaseTransaction interface.
    """
    id: str
    type: TransactionTypeEnum
    code: str
    name: str
    createdAt: str
    updatedAt: Optional[str] = None
    isActive: Optional[bool] = None
    isDeleted: Optional[bool] = None
    status: Optional[str] = None
    statusUpdatedBy: Optional[str] = None
    statusUpdatedAt: Optional[str] = None
    resourceIdentifier: Optional[str] = None
    gitopsWorkflowId: Optional[int] = None
    gitopsDetails: Optional[GitopsWorkflowDetailResponse] = None

    class Config:
        from_attributes = True


class InfrastructureTransactionResponse(BaseTransactionResponse):
    """
    Infrastructure transaction response matching frontend InfrastructureTransaction.
    Covers SQS and S3 resources from infrastructure_mst table.
    """
    type: TransactionTypeEnum = TransactionTypeEnum.INFRASTRUCTURE
    infrastructureTypeRefCode: str
    infrastructureTypeName: Optional[str] = None
    infraVendorAccountsCode: str
    resourceGroupCode: Optional[str] = None
    resourceGroupName: Optional[str] = None
    tenantCode: str
    applicationsCode: str
    applicationName: Optional[str] = None
    environment: str  # "dev" | "staging" | "production"
    locator: Optional[Dict[str, Any]] = None
    description: Optional[str] = None


class KongRouteTransactionResponse(BaseTransactionResponse):
    """
    Kong route transaction response matching frontend KongRouteTransaction.
    Covers Kong Gateway routes from kong_route_configs table.
    """
    type: TransactionTypeEnum = TransactionTypeEnum.KONG_ROUTE
    serviceCode: Optional[str] = None
    serviceName: Optional[str] = None
    applicationName: Optional[str] = None
    apiName: str
    httpMethod: str  # "GET" | "POST" | "PUT" | "DELETE" | etc.
    routePath: str
    creationError: Optional[str] = None


class AlertConfigTransactionResponse(BaseTransactionResponse):
    """
    Alert config transaction response matching frontend AlertConfigTransaction.
    Covers Datadog alerts from alert_configs table.
    """
    type: TransactionTypeEnum = TransactionTypeEnum.ALERT_CONFIG
    obsVendorAccountsCode: str
    serviceCode: Optional[str] = None
    serviceName: Optional[str] = None
    applicationName: Optional[str] = None
    infrastructureCode: Optional[str] = None
    signalKind: str
    comparator: str
    thresholdValue: float
    thresholdUnit: str
    evalWindow: int
    forDuration: int
    noData: Optional[bool] = None
    severity: str  # "critical" | "high" | "medium" | "low"
    monitorVendorRefId: Optional[str] = None
    vendorStatus: Optional[str] = None
    vendorMonitorId: Optional[str] = None
    vendorError: Optional[str] = None


# Union type for any transaction
TransactionResponse = Union[
    InfrastructureTransactionResponse,
    KongRouteTransactionResponse,
    AlertConfigTransactionResponse
]


# ============================================================================
# List Response Schema
# ============================================================================

class InfraTransactionsListResponse(BaseModel):
    """
    Paginated list response for infrastructure transactions.
    """
    success: bool = True
    tab: str = Field(..., description="Current tab filter")
    total: int = Field(..., description="Total number of matching records")
    count: int = Field(..., description="Number of records in this response")
    skip: int = Field(..., description="Pagination offset")
    limit: int = Field(..., description="Page size")
    data: List[Union[
        InfrastructureTransactionResponse,
        KongRouteTransactionResponse,
        AlertConfigTransactionResponse
    ]] = Field(default=[], description="List of transactions")

    class Config:
        from_attributes = True
