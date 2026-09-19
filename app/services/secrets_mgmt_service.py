"""
Secrets Management Service

Generic service layer for managing secrets and parameters across multiple cloud vendors.
Supports AWS (Secrets Manager, SSM Parameter Store), with structure to add GCP, Azure, etc.

Architecture:
- Generic public methods that route based on service vendor
- Vendor-specific private methods (e.g., _create_aws_secret, _create_gcp_secret)
- Simple if/else routing in public methods

TODO: This service is commented out during the refactoring of infra_vendor_enum
from services_mst to service_config. The service was using service.infra_vendor_enum
which has been moved to service_config. Re-enable when secrets management is needed
and update to use service_config.infra_vendor_enum instead.
"""

# from typing import Dict, Any, Optional, List
# from sqlalchemy.ext.asyncio import AsyncSession
#
# from app.core.enum import InfraVendorEnum, EnvironmentEnum, AWSResourceTypeEnum
# from app.repository.services_mst_repository import ServicesMstRepository
# from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
# from app.repository.aws_secrets_parameters_mst_repository import AWSSecretsParametersMstRepository
# from app.integrations.aws_integration import AWSIntegration
# from app.domain.factories.aws_secrets_parameters_factory import make_aws_secret_parameter_record
#
#
# class SecretsMgmtService:
#     """
#     Generic secrets management service supporting multiple vendors.
#     Routes to appropriate vendor-specific implementation based on service configuration.
#     """
#
#     def __init__(self, db: AsyncSession):
#         self.db = db
#         self.services_repo = ServicesMstRepository(db)
#         self.vendor_accounts_repo = InfraVendorAccountsMstRepository(db)
#
#     # ==================== PUBLIC METHODS (Generic API) ====================
#
#     async def get_service_secrets_and_parameters(
#         self,
#         service_code: str,
#         environment: str = "prod"
#     ) -> Dict[str, Any]:
#         """
#         Get all secrets and parameters configured for a service.
#         Works with AWS, GCP, Azure - vendor determined from service configuration.
#
#         Args:
#             service_code: Service code
#             environment: Environment (default: prod)
#
#         Returns:
#             Dict with service info, secrets list, parameters list
#
#         Raises:
#             ValueError: If service not found or vendor not supported
#         """
#         # Get service details
#         service = await self.services_repo.get_by_code(service_code)
#         if not service:
#             raise ValueError(f"Service not found: {service_code}")
#
#         vendor = service.infra_vendor_enum
#
#         # Route to vendor-specific implementation
#         if vendor == InfraVendorEnum.aws:
#             return await self._get_aws_secrets_and_parameters(service, environment)
#         elif vendor == InfraVendorEnum.gcp:
#             raise ValueError("GCP secrets management not yet implemented")
#         elif vendor == InfraVendorEnum.azure:
#             raise ValueError("Azure secrets management not yet implemented")
#         else:
#             raise ValueError(f"Vendor not supported: {vendor.value}")
#
#     async def link_existing_secret(
#         self,
#         service_code: str,
#         secret_name: str,
#         environment: str = "prod"
#     ) -> Dict[str, Any]:
#         """
#         Link an existing secret from cloud vendor.
#
#         Args:
#             service_code: Service code
#             secret_name: Secret name in the cloud vendor (will also be used as resource path)
#             environment: Environment (default: prod)
#
#         Returns:
#             Dict with status and linked secret metadata
#
#         Raises:
#             ValueError: If service not found, vendor not supported, or secret not found in vendor
#         """
#         # Get service details
#         service = await self.services_repo.get_by_code(service_code)
#         if not service:
#             raise ValueError(f"Service not found: {service_code}")
#
#         vendor = service.infra_vendor_enum
#
#         # Route to vendor-specific implementation
#         if vendor == InfraVendorEnum.aws:
#             return await self._link_aws_secret(service, secret_name, environment)
#         elif vendor == InfraVendorEnum.gcp:
#             raise ValueError("GCP secrets management not yet implemented")
#         elif vendor == InfraVendorEnum.azure:
#             raise ValueError("Azure secrets management not yet implemented")
#         else:
#             raise ValueError(f"Vendor not supported: {vendor.value}")
#
#     async def create_new_secret(
#         self,
#         service_code: str,
#         secret_name: str,
#         secret_value: Dict[str, Any],
#         tags: Optional[List[Dict[str, str]]] = None,
#         environment: str = "prod"
#     ) -> Dict[str, Any]:
#         """
#         Create a new secret in cloud vendor.
#
#         Args:
#             service_code: Service code
#             secret_name: Secret name (will also be used as resource path)
#             secret_value: Secret key-value pairs or string value
#             tags: Optional tags
#             environment: Environment (default: prod)
#
#         Returns:
#             Dict with status and created secret metadata
#
#         Raises:
#             ValueError: If service not found, vendor not supported, or secret already exists
#         """
#         # Get service details
#         service = await self.services_repo.get_by_code(service_code)
#         if not service:
#             raise ValueError(f"Service not found: {service_code}")
#
#         vendor = service.infra_vendor_enum
#
#         # Route to vendor-specific implementation
#         if vendor == InfraVendorEnum.aws:
#             return await self._create_aws_secret(service, secret_name, secret_value, tags, environment)
#         elif vendor == InfraVendorEnum.gcp:
#             raise ValueError("GCP secrets management not yet implemented")
#         elif vendor == InfraVendorEnum.azure:
#             raise ValueError("Azure secrets management not yet implemented")
#         else:
#             raise ValueError(f"Vendor not supported: {vendor.value}")
#
#     async def get_secret_values(self, secret_id: str) -> Dict[str, Any]:
#         """
#         Get secret values (key-value pairs) for display/edit.
#
#         Args:
#             secret_id: Database record ID or code
#
#         Returns:
#             Dict with secret metadata and values
#
#         Raises:
#             ValueError: If secret not found or deleted
#         """
#         # Try to parse as int (ID) or use as code
#         try:
#             record_id = int(secret_id)
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_id(record_id)
#         except ValueError:
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_code(secret_id)
#
#         if not record:
#             raise ValueError(f"Secret not found: {secret_id}")
#
#         # Check if marked as deleted
#         if record.is_secret_deleted:
#             raise ValueError(f"Secret has been deleted in cloud vendor and is no longer accessible")
#
#         # Route based on vendor (inferred from table)
#         # For now, we only have AWS table
#         return await self._get_aws_secret_values(record)
#
#     async def update_secret_values(
#         self,
#         secret_id: str,
#         secret_value: Dict[str, Any],
#         description: Optional[str] = None
#     ) -> Dict[str, Any]:
#         """
#         Update secret values.
#
#         Args:
#             secret_id: Database record ID or code
#             secret_value: Updated secret key-value pairs
#             description: Optional updated description
#
#         Returns:
#             Dict with status and updated secret metadata
#
#         Raises:
#             ValueError: If secret not found or deleted
#         """
#         # Try to parse as int (ID) or use as code
#         try:
#             record_id = int(secret_id)
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_id(record_id)
#         except ValueError:
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_code(secret_id)
#
#         if not record:
#             raise ValueError(f"Secret not found: {secret_id}")
#
#         # Check if marked as deleted
#         if record.is_secret_deleted:
#             raise ValueError(f"Secret has been deleted in cloud vendor and cannot be updated")
#
#         # Route based on vendor
#         return await self._update_aws_secret(record, secret_value, description)
#
#     async def link_existing_parameter(
#         self,
#         service_code: str,
#         parameter_name: str,
#         environment: str = "prod"
#     ) -> Dict[str, Any]:
#         """
#         Link an existing parameter from cloud vendor.
#
#         Args:
#             service_code: Service code
#             parameter_name: Full parameter path (will also be used as resource path)
#             environment: Environment (default: prod)
#
#         Returns:
#             Dict with status and linked parameter metadata
#
#         Raises:
#             ValueError: If service not found or parameter not found in vendor
#         """
#         service = await self.services_repo.get_by_code(service_code)
#         if not service:
#             raise ValueError(f"Service not found: {service_code}")
#
#         vendor = service.infra_vendor_enum
#
#         if vendor == InfraVendorEnum.aws:
#             return await self._link_aws_parameter(service, parameter_name, environment)
#         elif vendor == InfraVendorEnum.gcp:
#             raise ValueError("GCP parameter management not yet implemented")
#         elif vendor == InfraVendorEnum.azure:
#             raise ValueError("Azure configuration management not yet implemented")
#         else:
#             raise ValueError(f"Vendor not supported: {vendor.value}")
#
#     async def create_new_parameter(
#         self,
#         service_code: str,
#         parameter_name: str,
#         parameter_value: str,
#         parameter_type: str = "String",
#         tags: Optional[List[Dict[str, str]]] = None,
#         environment: str = "prod"
#     ) -> Dict[str, Any]:
#         """
#         Create a new parameter in cloud vendor.
#
#         Args:
#             service_code: Service code
#             parameter_name: Parameter name (will also be used as resource path)
#             parameter_value: Parameter value
#             parameter_type: Parameter type (AWS: String/StringList/SecureString)
#             tags: Optional tags
#             environment: Environment (default: prod)
#
#         Returns:
#             Dict with status and created parameter metadata
#
#         Raises:
#             ValueError: If service not found or parameter already exists
#         """
#         service = await self.services_repo.get_by_code(service_code)
#         if not service:
#             raise ValueError(f"Service not found: {service_code}")
#
#         vendor = service.infra_vendor_enum
#
#         if vendor == InfraVendorEnum.aws:
#             return await self._create_aws_parameter(service, parameter_name, parameter_value, parameter_type, tags, environment)
#         elif vendor == InfraVendorEnum.gcp:
#             raise ValueError("GCP parameter management not yet implemented")
#         elif vendor == InfraVendorEnum.azure:
#             raise ValueError("Azure configuration management not yet implemented")
#         else:
#             raise ValueError(f"Vendor not supported: {vendor.value}")
#
#     async def get_parameter_value(self, parameter_id: str) -> Dict[str, Any]:
#         """
#         Get parameter value.
#
#         Args:
#             parameter_id: Database record ID or code
#
#         Returns:
#             Dict with parameter metadata and value
#
#         Raises:
#             ValueError: If parameter not found or deleted
#         """
#         try:
#             record_id = int(parameter_id)
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_id(record_id)
#         except ValueError:
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_code(parameter_id)
#
#         if not record:
#             raise ValueError(f"Parameter not found: {parameter_id}")
#
#         if record.is_secret_deleted:
#             raise ValueError(f"Parameter has been deleted in cloud vendor and is no longer accessible")
#
#         return await self._get_aws_parameter_value(record)
#
#     async def update_parameter_value(
#         self,
#         parameter_id: str,
#         parameter_value: str,
#         description: Optional[str] = None
#     ) -> Dict[str, Any]:
#         """
#         Update parameter value.
#
#         Args:
#             parameter_id: Database record ID or code
#             parameter_value: Updated parameter value
#             description: Optional updated description
#
#         Returns:
#             Dict with status and updated parameter metadata
#
#         Raises:
#             ValueError: If parameter not found or deleted
#         """
#         try:
#             record_id = int(parameter_id)
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_id(record_id)
#         except ValueError:
#             aws_repo = AWSSecretsParametersMstRepository(self.db)
#             record = await aws_repo.get_by_code(parameter_id)
#
#         if not record:
#             raise ValueError(f"Parameter not found: {parameter_id}")
#
#         if record.is_secret_deleted:
#             raise ValueError(f"Parameter has been deleted in cloud vendor and cannot be updated")
#
#         return await self._update_aws_parameter(record, parameter_value, description)
#
#     # ==================== AWS-SPECIFIC PRIVATE METHODS ====================
#
#     async def _get_aws_secrets_and_parameters(
#         self,
#         service,
#         environment: str
#     ) -> Dict[str, Any]:
#         """Get all AWS secrets and parameters for a service"""
#         aws_repo = AWSSecretsParametersMstRepository(self.db)
#         env_enum = EnvironmentEnum(environment)
#
#         # Get secrets
#         secrets = await aws_repo.get_all_by_service(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             environment=env_enum,
#             resource_type=AWSResourceTypeEnum.secret
#         )
#
#         # Get parameters
#         parameters = await aws_repo.get_all_by_service(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             environment=env_enum,
#             resource_type=AWSResourceTypeEnum.parameter
#         )
#
#         # Get the actual AWS region identifier
#         region_value = service.region_custom if service.region_custom else (
#             service.region_ref.region_identifier if service.region_ref else None
#         )
#
#         return {
#             "status": "success",
#             "service": {
#                 "code": service.code,
#                 "name": service.name,
#                 "vendor": "aws",
#                 "tenant_code": service.tenants_mst_code,
#                 "application_code": service.applications_mst_code,
#                 "environment": environment,
#                 "region": region_value
#             },
#             "secrets": [self._serialize_aws_resource(s, "secret") for s in secrets],
#             "parameters": [self._serialize_aws_resource(p, "parameter") for p in parameters],
#             "has_configuration": len(secrets) + len(parameters) > 0,
#             "total_secrets": len(secrets),
#             "total_parameters": len(parameters)
#         }
#
#     async def _link_aws_secret(
#         self,
#         service,
#         secret_name: str,
#         environment: str
#     ) -> Dict[str, Any]:
#         """Link existing AWS secret by name"""
#         aws_repo = AWSSecretsParametersMstRepository(self.db)
#         env_enum = EnvironmentEnum(environment)
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, environment)
#
#         # Validate secret exists in AWS and get ARN
#         try:
#             aws_secret = AWSIntegration.get_secret(auth_config, secret_name)
#             secret_arn = aws_secret.get("arn")
#             if not secret_arn:
#                 raise ValueError("Secret ARN not found in AWS response")
#         except Exception as e:
#             raise ValueError(f"Secret not found in AWS or not accessible: {str(e)}")
#
#         # Use secret_name as resource_path
#         resource_path = secret_name
#
#         # Check if already linked
#         existing = await aws_repo.get_by_service_resource_path(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_path=resource_path,
#             environment=env_enum
#         )
#         if existing:
#             raise ValueError(f"Secret already linked: {resource_path}")
#
#         # Save to database
#         record_data = make_aws_secret_parameter_record(
#             tenant_code=service.tenants_mst_code,
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_type=AWSResourceTypeEnum.secret,
#             full_resource_path=resource_path,
#             resource_arn=secret_arn,
#             environment=env_enum,
#             secret_name=secret_name,
#             description=f"Linked AWS Secret: {secret_name}"
#         )
#
#         db_record = await aws_repo.create(**record_data)
#
#         return {
#             "status": "success",
#             "message": "Secret linked successfully from AWS",
#             "secret": self._serialize_aws_resource(db_record, "secret")
#         }
#
#     async def _create_aws_secret(
#         self,
#         service,
#         secret_name: str,
#         secret_value: Dict[str, Any],
#         tags: Optional[List[Dict[str, str]]],
#         environment: str
#     ) -> Dict[str, Any]:
#         """Create new AWS secret"""
#         aws_repo = AWSSecretsParametersMstRepository(self.db)
#         env_enum = EnvironmentEnum(environment)
#
#         # Use secret_name as resource_path
#         resource_path = secret_name
#
#         # Check if already exists in DB
#         existing = await aws_repo.get_by_service_resource_path(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_path=resource_path,
#             environment=env_enum
#         )
#         if existing:
#             raise ValueError(f"Secret already exists: {resource_path}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, environment)
#
#         # Create secret in AWS
#         try:
#             aws_result = AWSIntegration.create_secret(
#                 auth_config=auth_config,
#                 secret_name=secret_name,
#                 secret_value=secret_value,
#                 description=f"AWS Secret: {secret_name}",
#                 tags=tags
#             )
#         except Exception as e:
#             raise Exception(f"Failed to create secret in AWS: {str(e)}")
#
#         # Save to database
#         record_data = make_aws_secret_parameter_record(
#             tenant_code=service.tenants_mst_code,
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_type=AWSResourceTypeEnum.secret,
#             full_resource_path=resource_path,
#             resource_arn=aws_result["arn"],
#             environment=env_enum,
#             secret_name=secret_name,
#             description=f"AWS Secret: {secret_name}"
#         )
#
#         db_record = await aws_repo.create(**record_data)
#
#         return {
#             "status": "success",
#             "message": "Secret created successfully in AWS",
#             "secret": self._serialize_aws_resource(db_record, "secret")
#         }
#
#     async def _get_aws_secret_values(self, record) -> Dict[str, Any]:
#         """Get AWS secret values"""
#         # Get service to get auth config
#         service = await self.services_repo.get_by_code(record.resource_code)
#         if not service:
#             raise ValueError(f"Service not found: {record.resource_code}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, record.environments_enum.value)
#
#         # Get secret from AWS
#         try:
#             aws_secret = AWSIntegration.get_secret(auth_config, record.full_resource_path)
#         except Exception as e:
#             error_message = str(e).lower()
#             # Check if secret was deleted or scheduled for deletion in AWS
#             if ("not found" in error_message or
#                 "resourcenotfoundexception" in error_message or
#                 "scheduled for deletion" in error_message or
#                 "invalidrequestexception" in error_message):
#                 # Mark as deleted in DB
#                 aws_repo = AWSSecretsParametersMstRepository(self.db)
#                 await aws_repo.mark_as_deleted(record.id)
#                 raise ValueError(f"Secret not found in AWS or scheduled for deletion (marked as deleted in database)")
#             raise Exception(f"Failed to get secret from AWS: {str(e)}")
#
#         return {
#             "status": "success",
#             "secret": {
#                 "id": record.id,
#                 "code": record.code,
#                 "name": record.name,
#                 "vendor": "aws",
#                 "resource_path": record.full_resource_path,
#                 "resource_identifier": record.resource_arn,
#                 "values": aws_secret["value"],
#                 "version_id": aws_secret.get("version_id"),
#                 "last_updated": aws_secret.get("last_updated_date"),
#                 "description": record.description
#             }
#         }
#
#     async def _update_aws_secret(
#         self,
#         record,
#         secret_value: Dict[str, Any],
#         description: Optional[str]
#     ) -> Dict[str, Any]:
#         """Update AWS secret values"""
#         # Get service
#         service = await self.services_repo.get_by_code(record.resource_code)
#         if not service:
#             raise ValueError(f"Service not found: {record.resource_code}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, record.environments_enum.value)
#
#         # Update secret in AWS
#         try:
#             aws_result = AWSIntegration.update_secret(
#                 auth_config=auth_config,
#                 secret_name=record.full_resource_path,
#                 secret_value=secret_value,
#                 description=description
#             )
#         except Exception as e:
#             error_message = str(e).lower()
#             # Check if secret was deleted or scheduled for deletion in AWS
#             if ("not found" in error_message or
#                 "resourcenotfoundexception" in error_message or
#                 "scheduled for deletion" in error_message or
#                 "invalidrequestexception" in error_message):
#                 # Mark as deleted in DB
#                 aws_repo = AWSSecretsParametersMstRepository(self.db)
#                 await aws_repo.mark_as_deleted(record.id)
#                 raise ValueError(f"Secret not found in AWS or scheduled for deletion (marked as deleted in database)")
#             raise Exception(f"Failed to update secret in AWS: {str(e)}")
#
#         return {
#             "status": "success",
#             "message": "Secret updated successfully in AWS",
#             "secret": {
#                 "id": record.id,
#                 "code": record.code,
#                 "name": record.name,
#                 "version_id": aws_result.get("version_id"),
#                 "last_updated": "now"  # Would need to fetch again for exact timestamp
#             }
#         }
#
#     async def _link_aws_parameter(
#         self,
#         service,
#         parameter_name: str,
#         environment: str
#     ) -> Dict[str, Any]:
#         """Link existing AWS SSM parameter"""
#         aws_repo = AWSSecretsParametersMstRepository(self.db)
#         env_enum = EnvironmentEnum(environment)
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, environment)
#
#         # Validate parameter exists in AWS and get ARN
#         try:
#             aws_param = AWSIntegration.get_parameter(auth_config, parameter_name)
#         except Exception as e:
#             raise ValueError(f"Parameter not found in AWS or not accessible: {str(e)}")
#
#         # Get ARN and type from AWS response
#         param_arn = aws_param.get("arn")
#         if not param_arn:
#             raise ValueError("Parameter ARN not returned from AWS")
#
#         param_type = aws_param.get("type", "String")  # Get parameter type from AWS
#
#         # Use parameter_name as resource_path
#         resource_path = parameter_name.strip("/")  # Remove leading slash for consistency
#
#         # Check if already linked
#         existing = await aws_repo.get_by_service_resource_path(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_path=resource_path,
#             environment=env_enum
#         )
#         if existing:
#             raise ValueError(f"Parameter already linked: {resource_path}")
#
#         # Save to database
#         record_data = make_aws_secret_parameter_record(
#             tenant_code=service.tenants_mst_code,
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_type=AWSResourceTypeEnum.parameter,
#             full_resource_path=resource_path,
#             resource_arn=param_arn,
#             environment=env_enum,
#             secret_name=parameter_name,
#             description=f"Linked AWS Parameter: {parameter_name}",
#             parameter_type=param_type  # Pass parameter type to factory
#         )
#
#         db_record = await aws_repo.create(**record_data)
#
#         return {
#             "status": "success",
#             "message": "Parameter linked successfully from AWS",
#             "parameter": self._serialize_aws_resource(db_record, "parameter")
#         }
#
#     async def _create_aws_parameter(
#         self,
#         service,
#         parameter_name: str,
#         parameter_value: str,
#         parameter_type: str,
#         tags: Optional[List[Dict[str, str]]],
#         environment: str
#     ) -> Dict[str, Any]:
#         """Create new AWS SSM parameter"""
#         aws_repo = AWSSecretsParametersMstRepository(self.db)
#         env_enum = EnvironmentEnum(environment)
#
#         # Use parameter_name as resource_path (strip leading / for consistency in DB)
#         resource_path = parameter_name.strip("/")
#
#         # Check if already exists
#         existing = await aws_repo.get_by_service_resource_path(
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_path=resource_path,
#             environment=env_enum
#         )
#         if existing:
#             raise ValueError(f"Parameter already exists: {resource_path}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, environment)
#
#         # Create parameter in AWS (ensure leading / for SSM)
#         ssm_param_name = f"/{parameter_name.strip('/')}"
#         try:
#             aws_result = AWSIntegration.put_parameter(
#                 auth_config=auth_config,
#                 parameter_name=ssm_param_name,
#                 parameter_value=parameter_value,
#                 parameter_type=parameter_type,
#                 description=f"AWS Parameter: {parameter_name}",
#                 tags=tags
#             )
#         except Exception as e:
#             raise Exception(f"Failed to create parameter in AWS: {str(e)}")
#
#         # Save to database using ARN and type from AWS response
#         param_arn = aws_result.get("arn")
#         if not param_arn:
#             raise Exception("Parameter ARN not returned from AWS")
#
#         param_type_from_aws = aws_result.get("type", parameter_type)  # Get type from AWS response
#
#         record_data = make_aws_secret_parameter_record(
#             tenant_code=service.tenants_mst_code,
#             resource_code=service.code,  # TODO: update callers to pass resource_code + resource_type_str + applications_mst_code
#             resource_type=AWSResourceTypeEnum.parameter,
#             full_resource_path=resource_path,
#             resource_arn=param_arn,
#             environment=env_enum,
#             secret_name=parameter_name,
#             description=f"AWS Parameter: {parameter_name}",
#             parameter_type=param_type_from_aws  # Pass parameter type to factory
#         )
#
#         db_record = await aws_repo.create(**record_data)
#
#         return {
#             "status": "success",
#             "message": "Parameter created successfully in AWS",
#             "parameter": self._serialize_aws_resource(db_record, "parameter")
#         }
#
#     async def _get_aws_parameter_value(self, record) -> Dict[str, Any]:
#         """Get AWS parameter value"""
#         # Get service
#         service = await self.services_repo.get_by_code(record.resource_code)
#         if not service:
#             raise ValueError(f"Service not found: {record.resource_code}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, record.environments_enum.value)
#
#         # Get parameter from AWS (add leading /)
#         ssm_param_name = f"/{record.full_resource_path}"
#         try:
#             aws_param = AWSIntegration.get_parameter(auth_config, ssm_param_name)
#         except Exception as e:
#             error_message = str(e).lower()
#             # Check if parameter was deleted in AWS
#             if ("not found" in error_message or
#                 "parameternotfound" in error_message or
#                 "parameternotfoundexception" in error_message):
#                 # Mark as deleted in DB
#                 aws_repo = AWSSecretsParametersMstRepository(self.db)
#                 await aws_repo.mark_as_deleted(record.id)
#                 raise ValueError(f"Parameter not found in AWS (marked as deleted in database)")
#             raise Exception(f"Failed to get parameter from AWS: {str(e)}")
#
#         return {
#             "status": "success",
#             "parameter": {
#                 "id": record.id,
#                 "code": record.code,
#                 "name": record.name,
#                 "vendor": "aws",
#                 "resource_path": record.full_resource_path,
#                 "resource_identifier": record.resource_arn,
#                 "value": aws_param["value"],
#                 "parameter_type": aws_param.get("type"),
#                 "version": aws_param.get("version"),
#                 "last_updated": aws_param.get("last_modified_date"),
#                 "description": record.description
#             }
#         }
#
#     async def _update_aws_parameter(
#         self,
#         record,
#         parameter_value: str,
#         description: Optional[str]
#     ) -> Dict[str, Any]:
#         """Update AWS parameter value"""
#         # Get service
#         service = await self.services_repo.get_by_code(record.resource_code)
#         if not service:
#             raise ValueError(f"Service not found: {record.resource_code}")
#
#         # Get AWS credentials
#         auth_config = await self._get_aws_auth_config(service, record.environments_enum.value)
#
#         # Update parameter in AWS (add leading /)
#         ssm_param_name = f"/{record.full_resource_path}"
#         try:
#             aws_result = AWSIntegration.put_parameter(
#                 auth_config=auth_config,
#                 parameter_name=ssm_param_name,
#                 parameter_value=parameter_value,
#                 parameter_type="String",  # Keep existing type
#                 description=description,
#                 overwrite=True
#             )
#         except Exception as e:
#             error_message = str(e).lower()
#             # Check if parameter was deleted in AWS
#             if ("not found" in error_message or
#                 "parameternotfound" in error_message or
#                 "parameternotfoundexception" in error_message):
#                 # Mark as deleted in DB
#                 aws_repo = AWSSecretsParametersMstRepository(self.db)
#                 await aws_repo.mark_as_deleted(record.id)
#                 raise ValueError(f"Parameter not found in AWS (marked as deleted in database)")
#             raise Exception(f"Failed to update parameter in AWS: {str(e)}")
#
#         return {
#             "status": "success",
#             "message": "Parameter updated successfully in AWS",
#             "parameter": {
#                 "id": record.id,
#                 "code": record.code,
#                 "name": record.name,
#                 "version": aws_result.get("version"),
#                 "last_updated": "now"
#             }
#         }
#
#     # ==================== HELPER METHODS ====================
#
#     async def _get_aws_auth_config(self, service, environment: str) -> Dict[str, Any]:
#         """
#         Get AWS authentication configuration using hierarchical lookup.
#         Priority: Resource Group → Application → Tenant
#
#         The region is overridden with the service's region to ensure resources
#         are created in the correct region for the service.
#         """
#         vendor_account = await self.vendor_accounts_repo.get_by_hierarchy(
#             infra_vendor_enum=InfraVendorEnum.aws,
#             environments_enum=EnvironmentEnum(environment),
#             resource_group_code=service.resource_group_mst_code,
#             application_code=service.applications_mst_code,
#             tenant_code=service.tenants_mst_code
#         )
#
#         if not vendor_account:
#             raise ValueError(
#                 f"No AWS account configured for service {service.code} in {environment} environment"
#             )
#
#         # Clone auth_config and override region with service's actual AWS region identifier
#         auth_config = vendor_account.auth_config.copy()
#
#         # Get the actual AWS region identifier (e.g., "ap-south-1" not "aws-ap-south-1")
#         if service.region_custom:
#             # For on-prem or custom regions, use as-is
#             auth_config["region"] = service.region_custom
#         elif service.region_ref:
#             # For cloud vendors, use the actual region identifier from region_ref
#             auth_config["region"] = service.region_ref.region_identifier
#         else:
#             raise ValueError(f"Service {service.code} has no region configured")
#
#         return auth_config
#
#     def _build_aws_resource_path(
#         self,
#         tenant_code: str,
#         environment: str,
#         region: str,
#         service_name: str,
#         resource_name: Optional[str]
#     ) -> str:
#         """
#         Build AWS resource path.
#
#         Logic:
#         - If resource_name provided: {tenant}/{environment}/{region}/{resource_name}
#         - If not provided: {tenant}/{environment}/{region}/{service_name}
#
#         AWS SSM Parameter Naming Rules:
#         - Can't be prefixed with "ssm" (case-insensitive)
#         - Can consist of sub-paths divided by slash
#         - Each sub-path can be: letters, numbers, .-_
#         - Spaces are NOT allowed
#         """
#         import re
#
#         def sanitize_path_component(component: str) -> str:
#             """Sanitize a single path component for AWS SSM compliance"""
#             # Replace spaces and invalid characters with hyphens
#             sanitized = re.sub(r'[^a-zA-Z0-9._-]', '-', component)
#             # Remove consecutive hyphens
#             sanitized = re.sub(r'-+', '-', sanitized)
#             # Remove leading/trailing hyphens
#             sanitized = sanitized.strip('-')
#             # Convert to lowercase for consistency
#             sanitized = sanitized.lower()
#             return sanitized
#
#         # Sanitize all components
#         tenant_code = sanitize_path_component(tenant_code)
#         environment = sanitize_path_component(environment)
#
#         # Remove "aws-" prefix from region if present (common mistake)
#         region = region.replace('aws-', '')
#         region = sanitize_path_component(region)
#
#         # Determine the final name component
#         name = resource_name if resource_name else service_name
#         name = sanitize_path_component(name)
#
#         return f"{tenant_code}/{environment}/{region}/{name}"
#
#     def _serialize_aws_resource(self, record, resource_type: str) -> Dict[str, Any]:
#         """Serialize AWS DB record to response dict"""
#         result = {
#             "id": record.id,
#             "code": record.code,
#             "name": record.name,
#             "type": resource_type,
#             "vendor": "aws",
#             "resource_path": record.full_resource_path,
#             "resource_identifier": record.resource_arn,
#             "environment": record.environments_enum.value,
#             "is_deleted": record.is_secret_deleted,
#             "created_at": record.created_at.isoformat() if record.created_at else None,
#             "updated_at": record.updated_at.isoformat() if record.updated_at else None,
#             "description": record.description
#         }
#
#         # Add parameter_type for parameters
#         if resource_type == "parameter":
#             result["parameter_type"] = record.parameter_type
#
#         return result
