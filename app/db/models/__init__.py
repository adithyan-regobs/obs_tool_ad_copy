# Import all models to ensure they are registered with SQLAlchemy
# Order matters: import parent models before child models to avoid circular dependency issues

# Base models first
from app.db.models.base_model import Base, BaseModel, AlertBaseConfig

# Reference tables (no dependencies)
from app.db.models.infrastructuretype_ref_model import InfrastructureTypeRefModel
from app.db.models.alerttype_ref_model import AlertTypeRefModel
from app.db.models.region_ref_model import RegionRefModel
from app.db.models.language_ref_model import LanguageRefModel
from app.db.models.case_type_ref_model import CaseTypeRefModel
from app.db.models.case_ref_model import CaseRefModel
from app.db.models.policy_ref_model import PolicyRefModel
from app.db.models.role_type_ref_model import RoleTypeRefModel

# Master tables (hierarchical order: tenant -> application -> service/resource_group)
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.db.models.workspace_mst_model import WorkspaceMstModel
from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.resource_group_mst_model import ResourceGroupMstModel
from app.db.models.user_mst_model import UserMstModel
from app.db.models.invitation_mst_model import InvitationMstModel
from app.db.models.ext_auth_code_model import ExtAuthCodeModel

# Infrastructure and vendor accounts
from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel
from app.db.models.obs_vendor_accounts_mst_model import ObsVendorAccountsMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.namespace_mst_model import NamespaceMstModel
from app.db.models.db_object_mst_model import DbObjectMstModel
from app.db.models.db_permission_mst_model import DbPermissionMstModel
from app.db.models.variable_mst_model import VariableMstModel

# Pipeline tables (order matters: vendor -> pipeline -> run track)
from app.db.models.pipeline_vendor_mst_model import PipelineVendorMstModel
from app.db.models.pipeline_mst_model import PipelineMstModel
from app.db.models.pipeline_run_track_model import PipelineRunTrackModel

# Policy and configuration tables
from app.db.models.monitoring_policy_defaults_ref_model import MonitoringPolicyDefaultsRefModel
from app.db.models.monitoring_policy_overrides_mst_model import MonitoringPolicyOverridesMstModel
from app.db.models.alert_config_model import AlertConfigModel
from app.db.models.sidecar_config_model import SidecarConfigModel
from app.db.models.service_config_model import ServiceConfigModel

# Dependency mapping
from app.db.models.service_dependency_map_model import ServiceDependencyMapModel

# AWS Resources
from app.db.models.aws_secrets_parameters_mst_model import AWSSecretsParametersMstModel
from app.db.models.kong_route_config_model import KongRouteConfigModel
from app.db.models.kong_route_group_model import KongRouteGroupModel
from app.db.models.transaction_queue_workflow_mapping_model import TransactionQueueWorkflowMappingModel
from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.production_deployment_track_model import ProductionDeploymentTrackModel
from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.transaction_queue_model import TransactionQueueModel, GitopsQueueStatusEnum, TransactionQueueStatusEnum

# Chat tables
from app.db.models.chat_info_model import ChatInfoModel
from app.db.models.chat_message_model import ChatMessageModel
from app.db.models.chat_summary_model import ChatSummaryModel
from app.db.models.chat_history_model import ChatHistoryModel
from app.db.models.chat_session_model import ChatSessionModel
from app.db.models.conversation_message_model import ConversationMessageModel

# Ticket tables
from app.db.models.ticket_model import TicketModel

# Sales inquiry
from app.db.models.sales_inquiry_model import SalesInquiryModel

# Model registry (EFS-cached HuggingFace models)
from app.db.models.model_registry_model import ModelRegistryModel

# Audit trail (3 normalized tables: actor -> event -> field_change)
from app.db.models.audit_log_model import AuditActorModel, AuditEventModel, AuditFieldChangeModel

# Permission tables
from app.db.models.service_user_permission_model import ServiceUserPermissionModel
from app.db.models.user_permission_cache_model import UserPermissionCacheModel
from app.db.models.approval_rule_mst_model import ApprovalRuleMstModel

__all__ = [
    "Base",
    "BaseModel",
    "AlertBaseConfig",
    "InfrastructureTypeRefModel",
    "AlertTypeRefModel",
    "RegionRefModel",
    "LanguageRefModel",
    "CaseTypeRefModel",
    "CaseRefModel",
    "TenantsMstModel",
    "GeoLocMstModel",
    "WorkspaceMstModel",
    "WorkspaceUserMapModel",
    "ApplicationsMstModel",
    "ServicesMstModel",
    "ResourceGroupMstModel",
    "UserMstModel",
    "InvitationMstModel",
    "ExtAuthCodeModel",
    "InfraVendorAccountsMstModel",
    "ObsVendorAccountsMstModel",
    "InfrastructureMstModel",
    "PipelineVendorMstModel",
    "PipelineMstModel",
    "PipelineRunTrackModel",
    "MonitoringPolicyDefaultsRefModel",
    "MonitoringPolicyOverridesMstModel",
    "AlertConfigModel",
    "SidecarConfigModel",
    "ServiceConfigModel",
    "ServiceDependencyMapModel",
    "AWSSecretsParametersMstModel",
    "KongRouteConfigModel",
    "KongRouteGroupModel",
    "TransactionQueueWorkflowMappingModel",
    "GitopsWorkflowDetailModel",
    "ProductionDeploymentTrackModel",
    "ServiceConfigDockerfileWorkflowModel",
    "ChatInfoModel",
    "ChatMessageModel",
    "ChatSummaryModel",
    "ChatHistoryModel",
    "ChatSessionModel",
    "ConversationMessageModel",
    "TicketModel",
    "AuditActorModel",
    "AuditEventModel",
    "AuditFieldChangeModel",
    "PolicyRefModel",
    "RoleTypeRefModel",
    "ServiceUserPermissionModel",
    "UserPermissionCacheModel",
    "ApprovalRuleMstModel",
    "NamespaceMstModel",
    "DbObjectMstModel",
    "DbPermissionMstModel",
    "TransactionQueueModel",
    "GitopsQueueStatusEnum",
    "TransactionQueueStatusEnum",
    "SalesInquiryModel",
]
