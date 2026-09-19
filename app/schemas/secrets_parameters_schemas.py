"""
Secrets and Parameters Management Schemas

Generic Pydantic schemas for secrets and parameters management across multiple vendors (AWS, GCP, Azure).
These schemas provide request validation and response serialization for the secrets management API.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


# ==================== Request Schemas ====================

class GetServiceSecretsRequest(BaseModel):
    """Request schema for getting all secrets and parameters for a service"""
    environment: str = Field("prod", description="Environment (dev, staging, prod)")
    resource_type: Optional[str] = Field(None, description="Filter by type (secret or parameter)")


class LinkExistingSecretRequest(BaseModel):
    """Request schema for linking an existing secret from cloud vendor"""
    resource_code: str = Field(..., description="Generic resource code (services_mst.code or infrastructure_mst.code)")
    resource_type_str: str = Field(..., description="Canvas resource type: service, database, bucket, queue, function, etc.")
    application_code: str = Field(..., description="Application code for canvas-scoped variables")
    secret_name: str = Field(
        ...,
        description="Secret name in cloud vendor (will also be used as resource path)"
    )
    environment: str = Field("prod", description="Environment (default: prod)")


class CreateSecretRequest(BaseModel):
    """Request schema for creating a new secret in cloud vendor"""
    resource_code: str = Field(..., description="Generic resource code (services_mst.code or infrastructure_mst.code)")
    resource_type_str: str = Field(..., description="Canvas resource type: service, database, bucket, queue, function, etc.")
    application_code: str = Field(..., description="Application code for canvas-scoped variables")
    secret_name: str = Field(..., description="Secret name (will also be used as resource path)")
    secret_value: Dict[str, Any] = Field(
        ...,
        description="Secret key-value pairs (e.g., {'api_key': 'value', 'api_secret': 'value'}) or string"
    )
    tags: Optional[List[Dict[str, str]]] = Field(
        None,
        description="Optional tags in format [{'Key': 'Environment', 'Value': 'prod'}]"
    )
    environment: str = Field("prod", description="Environment (default: prod)")


class UpdateSecretRequest(BaseModel):
    """Request schema for updating secret values"""
    secret_value: Dict[str, Any] = Field(..., description="Updated secret key-value pairs")


class LinkExistingParameterRequest(BaseModel):
    """Request schema for linking an existing parameter from cloud vendor"""
    resource_code: str = Field(..., description="Generic resource code (services_mst.code or infrastructure_mst.code)")
    resource_type_str: str = Field(..., description="Canvas resource type: service, database, bucket, queue, function, etc.")
    application_code: str = Field(..., description="Application code for canvas-scoped variables")
    parameter_name: str = Field(
        ...,
        description="Full parameter path in AWS (will also be used as resource path)"
    )
    environment: str = Field("prod", description="Environment (default: prod)")


class CreateParameterRequest(BaseModel):
    """Request schema for creating a new parameter in cloud vendor"""
    resource_code: str = Field(..., description="Generic resource code (services_mst.code or infrastructure_mst.code)")
    resource_type_str: str = Field(..., description="Canvas resource type: service, database, bucket, queue, function, etc.")
    application_code: str = Field(..., description="Application code for canvas-scoped variables")
    parameter_name: str = Field(..., description="Parameter name (will also be used as resource path)")
    parameter_value: str = Field(..., description="Parameter value")
    parameter_type: str = Field(
        "String",
        description="Parameter type (AWS: String/StringList/SecureString)"
    )
    tags: Optional[List[Dict[str, str]]] = Field(
        None,
        description="Optional tags"
    )
    environment: str = Field("prod", description="Environment (default: prod)")


class UpdateParameterRequest(BaseModel):
    """Request schema for updating parameter value"""
    parameter_value: str = Field(..., description="Updated parameter value")
    description: Optional[str] = Field(None, description="Optional updated description")


# ==================== Response Item Schemas ====================

class SecretResourceItem(BaseModel):
    """Individual secret resource metadata"""
    id: int = Field(..., description="Database record ID")
    code: str = Field(..., description="Unique record code")
    name: Optional[str] = Field(None, description="Display name")
    type: str = Field(..., description="Resource type (secret)")
    vendor: str = Field(..., description="Cloud vendor (aws, gcp, azure)")
    resource_path: str = Field(..., description="Full vendor-specific path")
    resource_identifier: str = Field(..., description="Vendor-specific identifier (ARN, resource name, URI)")
    environment: str = Field(..., description="Environment (dev, staging, prod)")
    is_deleted: bool = Field(..., description="True if deleted in vendor but tracked in DB")
    created_at: Optional[str] = Field(None, description="Creation timestamp (ISO format)")
    updated_at: Optional[str] = Field(None, description="Last update timestamp (ISO format)")
    description: Optional[str] = Field(None, description="Description")


class ParameterResourceItem(BaseModel):
    """Individual parameter resource metadata"""
    id: int = Field(..., description="Database record ID")
    code: str = Field(..., description="Unique record code")
    name: Optional[str] = Field(None, description="Display name")
    type: str = Field(..., description="Resource type (parameter)")
    vendor: str = Field(..., description="Cloud vendor (aws, gcp, azure)")
    parameter_type: Optional[str] = Field(None, description="Parameter type (String, StringList, SecureString)")
    resource_path: str = Field(..., description="Full vendor-specific path")
    resource_identifier: str = Field(..., description="Vendor-specific identifier (ARN, resource name, URI)")
    environment: str = Field(..., description="Environment (dev, staging, prod)")
    is_deleted: bool = Field(..., description="True if deleted in vendor but tracked in DB")
    created_at: Optional[str] = Field(None, description="Creation timestamp (ISO format)")
    updated_at: Optional[str] = Field(None, description="Last update timestamp (ISO format)")
    description: Optional[str] = Field(None, description="Description")


class ServiceInfo(BaseModel):
    """Resource information in response"""
    resource_code: str = Field(..., description="Generic resource code")
    resource_type_str: str = Field(..., description="Canvas resource type")
    name: str = Field(..., description="Resource name")
    vendor: str = Field(..., description="Cloud vendor")
    tenant_code: str = Field(..., description="Tenant code")
    tenant_name: Optional[str] = Field(None, description="Tenant name")
    application_code: str = Field(..., description="Application code")
    environment: str = Field(..., description="Environment")
    region: str = Field(..., description="Region")


# ==================== Response Schemas ====================

class GetServiceSecretsResponse(BaseModel):
    """Response schema for getting all secrets and parameters for a service"""
    status: str = Field(..., description="Response status (success, error)")
    service: ServiceInfo = Field(..., description="Service information")
    secrets: List[SecretResourceItem] = Field(default=[], description="List of secret resources")
    parameters: List[ParameterResourceItem] = Field(default=[], description="List of parameter resources")
    has_configuration: bool = Field(..., description="True if any secrets/parameters configured")
    total_secrets: int = Field(..., description="Total number of secrets")
    total_parameters: int = Field(..., description="Total number of parameters")


class LinkExistingSecretResponse(BaseModel):
    """Response schema for linking existing secret"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    secret: SecretResourceItem = Field(..., description="Linked secret metadata")


class CreateSecretResponse(BaseModel):
    """Response schema for creating new secret"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    secret: SecretResourceItem = Field(..., description="Created secret metadata")


class GetSecretValuesResponse(BaseModel):
    """Response schema for getting secret values"""
    status: str = Field(..., description="Response status (success, error)")
    secret: Dict[str, Any] = Field(..., description="Secret metadata and values")


class UpdateSecretResponse(BaseModel):
    """Response schema for updating secret"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    secret: Dict[str, Any] = Field(..., description="Updated secret metadata")


class LinkExistingParameterResponse(BaseModel):
    """Response schema for linking existing parameter"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    parameter: ParameterResourceItem = Field(..., description="Linked parameter metadata")


class CreateParameterResponse(BaseModel):
    """Response schema for creating new parameter"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    parameter: ParameterResourceItem = Field(..., description="Created parameter metadata")


class GetParameterValueResponse(BaseModel):
    """Response schema for getting parameter value"""
    status: str = Field(..., description="Response status (success, error)")
    parameter: Dict[str, Any] = Field(..., description="Parameter metadata and value")


class UpdateParameterResponse(BaseModel):
    """Response schema for updating parameter"""
    status: str = Field(..., description="Response status (success, error)")
    message: str = Field(..., description="Success or error message")
    parameter: Dict[str, Any] = Field(..., description="Updated parameter metadata")


# --- Canvas Variable Schemas ---

class CreateCanvasVariableRequest(BaseModel):
    """Request to create a new canvas variable or variable reference.
    For regular variables: provide variable_value (stored in AWS Secrets Manager).
    For references: provide referenced_variable_id (DB-only, no AWS call)."""
    application_code: str = Field(..., description="Application code for canvas scope")
    environment: str = Field(..., description="Environment: dev, stage, qa, prod")
    transaction_code: str = Field(..., description="Canvas resource code (service_configs.code or infrastructure_mst.code)")
    table_name: str = Field(..., description="Canvas resource type: service, database, bucket, queue, function, etc. Maps to WorkflowSourceTableEnum.")
    resource_name: str = Field("", description="Human-readable resource name for AWS secret path (e.g. service name). Falls back to transaction_code if empty.")
    variable_name: str = Field(..., description="Variable key name, e.g. DATABASE_URL")
    variable_value: Optional[str] = Field(None, description="Variable value — sent to AWS Secrets Manager, not stored in DB. Mutually exclusive with referenced_variable_id.")
    referenced_variable_id: Optional[int] = Field(None, description="DB id of the source variable being referenced. Mutually exclusive with variable_value.")


class UpdateCanvasVariableRequest(BaseModel):
    """Request to update an existing canvas variable value in AWS Secrets Manager"""
    variable_value: str = Field(..., description="New variable value — sent to AWS")


class CreateCanvasVariableRefRequest(BaseModel):
    """Request to create a variable reference (interpolation link between canvas nodes)"""
    application_code: str = Field(..., description="Application code for canvas scope")
    environment: str = Field(..., description="Environment: dev, stage, qa, prod")
    target_resource_code: str = Field(..., description="Resource code of the node receiving the ref")
    source_resource_code: str = Field(..., description="Resource code of the node providing the variable")
    source_label: str = Field(..., description="Display name of source node")
    variable_name: str = Field(..., description="Original variable name on source node")
    alias_name: str = Field(..., description="Name used on target node")
    value_template: str = Field(..., description="Interpolation template, e.g. ${postgres-db.HOST}")


class CanvasVariableResponse(BaseModel):
    """Canvas variable metadata from variable_mst — NO value field (value lives in AWS Secrets Manager)"""
    id: int
    code: str
    transaction_code: str
    table_name: str
    name: str = Field(..., description="Variable key name")
    type: str = Field(default="secret", description="'secret' → AWS Secrets Manager, 'variable' → stored in DB directly")
    secret_arn: Optional[str] = Field(None, description="AWS ARN (variable_cloud_identifier from variable_mst)")
    referenced_variable_id: Optional[int] = Field(None, description="DB id of source variable if this is a reference")
    environment: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CanvasVariableWithValueResponse(CanvasVariableResponse):
    """Canvas variable with value fetched on-demand from AWS Secrets Manager"""
    value: str = Field(..., description="Variable value fetched from AWS Secrets Manager")


class CanvasVariableRefResponse(BaseModel):
    """Variable reference (interpolation link between canvas nodes)"""
    id: str
    source_resource_code: str
    source_label: str
    variable_name: str
    alias_name: str
    value_template: str


class BulkCanvasVariablesResponse(BaseModel):
    """Bulk response: all canvas variables for an application + environment, grouped by resource."""
    application_code: str
    environment: str
    variables: List[CanvasVariableResponse] = Field(default=[], description="All canvas variables")
    refs: List[CanvasVariableRefResponse] = Field(default=[], description="All variable references")


class BulkCreateRefItem(BaseModel):
    """Single reference variable in a bulk-create-refs request."""
    target_name: str = Field(..., description="Variable name on the target resource (e.g. 'DB_HOST')")
    referenced_variable_id: int = Field(..., description="DB id of the source variable being referenced")


class BulkCreateRefsRequest(BaseModel):
    """Bulk create referenced variables. Same path/shape as create_variable but
    batched into 1 AWS read + 1 AWS write. Merges new keys into the existing
    consolidated secret — does not remove keys added by previous calls."""
    application_code: str = Field(..., description="Application code for canvas scope")
    environment: str = Field(..., description="Environment: dev, stage, qa, prod")
    transaction_code: str = Field(..., description="Target resource code (service_configs.code or infrastructure_mst.code)")
    table_name: str = Field(..., description="Canvas resource type: service, SERVICE_CONFIG, etc.")
    resource_name: str = Field(..., description="Human-readable resource name for AWS secret path")
    items: List[BulkCreateRefItem] = Field(..., description="List of target_name → referenced_variable_id mappings")


class BulkCreateRefsResponse(BaseModel):
    """Result of bulk create refs."""
    created: int = 0
    variables: List[CanvasVariableResponse] = Field(default=[])


class BulkUpsertVariableItem(BaseModel):
    """Single variable in a bulk upsert request."""
    name: str = Field(..., description="Variable key name")
    value: str = Field(..., description="Variable value")
    type: str = Field(default="secret", description="'secret' → AWS Secrets Manager, 'variable' → stored in DB directly")


class BulkUpsertVariablesRequest(BaseModel):
    """Bulk create/update/delete variables for a single resource. One AWS read + one write.
    Optionally handles reference creation and deletion in the same call."""
    application_code: str
    environment: str
    resource_code: str
    resource_type_str: str
    resource_name: str
    variables: List[BulkUpsertVariableItem] = Field(..., description="Full list of variables — replaces existing set")
    refs_to_add: List[BulkCreateRefItem] = Field(default=[], description="New reference variables to create")
    refs_to_delete: List[int] = Field(default=[], description="DB ids of reference variables to delete")


class BulkUpsertVariablesResponse(BaseModel):
    """Result of bulk upsert."""
    created: int = 0
    updated: int = 0
    deleted: int = 0
    refs_created: int = 0
    refs_deleted: int = 0
    variables: List[CanvasVariableResponse] = Field(default=[])
    ref_variables: List[CanvasVariableResponse] = Field(default=[])
