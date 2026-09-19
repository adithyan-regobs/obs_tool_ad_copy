import asyncio
import base64
import boto3
import aioboto3
import json
from typing import Dict, Any, Optional, List, Tuple
from botocore.exceptions import ClientError, BotoCoreError
from botocore.config import Config
import logging

logger = logging.getLogger(__name__)


class AWSIntegration:
    """
    Integration class for AWS services (Secrets Manager, SSM Parameter Store, ECR, IAM, etc.)

    Supports multi-tenant access via AWS STS AssumeRole for cross-account operations.

    ==================================================================================
    AUTHENTICATION TYPES - Stored in infra_vendor_accounts_mst.auth_config (JSONB)
    ==================================================================================

    The system supports 2 main authentication types, determined by the
    "authentication_type" field in auth_config:

    ---------------------------------------------------------------------------------
    1. IAM ROLE AUTHENTICATION (Recommended for Production)
    ---------------------------------------------------------------------------------
    Uses the IAM role attached to the EC2 instance or ECS task where the app runs.
    No credentials need to be stored in the database - AWS SDK finds them automatically.

    Use Case:
    - Production deployments on EC2, ECS, Lambda
    - When your infrastructure supports IAM roles
    - Most secure option (no credential storage)

    Database Example:
    {
        "authentication_type": "iam_role",
        "region": "us-east-1"
    }

    How it works:
    - aioboto3.Session(region_name=region) with NO credentials
    - AWS SDK automatically uses the instance/task IAM role
    - Credentials are rotated automatically by AWS

    ---------------------------------------------------------------------------------
    2. ACCESS KEY AUTHENTICATION (Use for Local Development Only)
    ---------------------------------------------------------------------------------
    Uses explicit AWS access keys stored in the database.
    NOT recommended for production due to security risks.

    Use Case:
    - Local development on developer machines
    - CI/CD pipelines that don't support IAM roles
    - Testing environments

    Database Example:
    {
        "authentication_type": "access_key",
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "region": "us-east-1"
    }

    How it works:
    - aioboto3.Session(aws_access_key_id=..., aws_secret_access_key=..., region_name=...)
    - Uses explicit credentials from database
    - Credentials must be manually rotated

    Security Warning:
    - Never commit access keys to git
    - Use IAM users with minimal permissions
    - Rotate keys regularly
    - Consider encrypting auth_config in production

    ---------------------------------------------------------------------------------
    ADVANCED: CROSS-ACCOUNT ACCESS via AssumeRole (Optional)
    ---------------------------------------------------------------------------------
    Both authentication types above can be combined with AssumeRole for cross-account
    or cross-tenant access. This is useful for multi-tenant SaaS applications.

    Use Case:
    - Managing resources in customer AWS accounts
    - Accessing resources across multiple AWS accounts
    - Tenant isolation in multi-tenant architectures

    Example 1: IAM Role + AssumeRole (Production Multi-Tenant)
    {
        "authentication_type": "iam_role",
        "region": "us-west-2",
        "assume_role_arn": "arn:aws:iam::123456789012:role/CustomerAccessRole",
        "external_id": "unique-security-token",          // Recommended for security
        "session_duration": 3600,                        // Session duration in seconds
        "session_name": "ObsToolSession"                 // Session name for CloudTrail
    }

    Example 2: Access Key + AssumeRole (Dev/Testing Multi-Tenant)
    {
        "authentication_type": "access_key",
        "aws_access_key_id": "AKIA...",
        "aws_secret_access_key": "wJalr...",
        "region": "us-east-1",
        "assume_role_arn": "arn:aws:iam::987654321098:role/TenantAccessRole",
        "external_id": "tenant-specific-id"
    }

    How it works:
    1. First authenticate using base credentials (IAM role or access keys)
    2. Call STS AssumeRole to get temporary credentials for target account
    3. Use temporary credentials to access resources in target account
    4. Temporary credentials auto-expire after session_duration

    ---------------------------------------------------------------------------------
    FIELD REFERENCE
    ---------------------------------------------------------------------------------
    Required Fields:
    - authentication_type: "iam_role" | "access_key" (defaults to "iam_role")
    - region: AWS region (e.g., "us-east-1", "us-west-2")

    Required for "access_key" type:
    - aws_access_key_id: AWS access key ID
    - aws_secret_access_key: AWS secret access key

    Optional (for AssumeRole):
    - assume_role_arn: ARN of role to assume in target account
    - external_id: Security token for AssumeRole (recommended)
    - session_duration: Session duration in seconds (default: 3600, max: 43200)
    - session_name: Session name for CloudTrail auditing (default: "ObsToolSession")

    Optional (for Same Account):
    - same_account: Set to True to use native IAM role without AssumeRole
                   (useful when Lambda/ECS is in the same account being accessed)

    ==================================================================================
    """

    @staticmethod
    def _get_config() -> Config:
        """Get boto3 config for AWS operations."""
        return Config(
            signature_version='v4',
            retries={'max_attempts': 10, 'mode': 'adaptive'}
        )

    @staticmethod
    def _get_session_kwargs(auth_config: Dict[str, str]) -> Dict[str, Any]:
        """
        Build session kwargs from auth config.

        Args:
            auth_config: Authentication configuration dict

        Returns:
            Dict of kwargs for aioboto3.Session

        Raises:
            ValueError: If authentication configuration is invalid
        """
        auth_type = auth_config.get("authentication_type", "iam_role")
        region = auth_config.get("region", "us-east-1")

        if auth_type == "access_key":
            access_key = auth_config.get("aws_access_key_id")
            secret_key = auth_config.get("aws_secret_access_key")
            session_token = auth_config.get("aws_session_token")  # For temporary credentials

            if not access_key or not secret_key:
                raise ValueError("aws_access_key_id and aws_secret_access_key are required for access_key authentication")

            session_kwargs = {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "region_name": region
            }

            # Add session token if present (for temporary credentials from STS)
            if session_token:
                session_kwargs["aws_session_token"] = session_token

            return session_kwargs
        elif auth_type == "iam_role":
            return {"region_name": region}
        else:
            raise ValueError(f"Unsupported authentication_type: {auth_type}. Use 'iam_role' or 'access_key'")

    @staticmethod
    async def _assume_role(auth_config: Dict[str, str]) -> Dict[str, Any]:
        """
        Assume a role for cross-account or cross-tenant access.

        Args:
            auth_config: Auth configuration containing assume_role_arn

        Returns:
            Dict of session kwargs with assumed role credentials

        Raises:
            ValueError: If assume_role_arn is missing
            Exception: If AssumeRole operation fails
        """
        role_arn = auth_config.get("assume_role_arn")
        if not role_arn:
            raise ValueError("assume_role_arn is required for AssumeRole operation")

        session_kwargs = AWSIntegration._get_session_kwargs(auth_config)
        region = auth_config.get("region", "us-east-1")
        config = AWSIntegration._get_config()

        assume_role_params = {
            "RoleArn": role_arn,
            "RoleSessionName": auth_config.get("session_name", "ObsToolSession"),
            "DurationSeconds": int(auth_config.get("session_duration", 3600))
        }

        if "external_id" in auth_config:
            assume_role_params["ExternalId"] = auth_config["external_id"]

        try:
            session = aioboto3.Session(**session_kwargs)
            async with session.client("sts", config=config) as sts_client:
                response = await sts_client.assume_role(**assume_role_params)
                credentials = response["Credentials"]

                return {
                    "aws_access_key_id": credentials["AccessKeyId"],
                    "aws_secret_access_key": credentials["SecretAccessKey"],
                    "aws_session_token": credentials["SessionToken"],
                    "region_name": region
                }
        except ClientError as e:
            logger.error(f"Failed to assume role {role_arn}: {str(e)}")
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "AccessDenied":
                raise PermissionError(
                    "Permission denied: unable to access the target AWS account. "
                    "Please verify the IAM role trust policy and permissions are configured correctly."
                )
            raise Exception(f"AssumeRole failed: {str(e)}")

    @staticmethod
    async def _get_client_kwargs(auth_config: Dict[str, str], use_assume_role: bool = True) -> Dict[str, Any]:
        """
        Get kwargs for creating an AWS service client.

        Args:
            auth_config: Authentication configuration dict
            use_assume_role: Whether to use AssumeRole if assume_role_arn is present

        Returns:
            Dict of kwargs for session and client creation
        """
        # If same_account flag is True, use Lambda's IAM role directly (no AssumeRole)
        if auth_config.get("same_account") is True:
            logger.info("same_account=True detected, using native IAM role")
            return AWSIntegration._get_session_kwargs(auth_config)

        # Otherwise, use AssumeRole if assume_role_arn is present
        if use_assume_role and "assume_role_arn" in auth_config:
            return await AWSIntegration._assume_role(auth_config)
        else:
            return AWSIntegration._get_session_kwargs(auth_config)

    # ==================== SYNC METHODS (for backwards compatibility) ====================

    @staticmethod
    def _get_base_session_sync(auth_config: Dict[str, str]) -> boto3.Session:
        """
        Create base boto3 session from auth config (SYNC version).

        Args:
            auth_config: Authentication configuration dict

        Returns:
            boto3.Session configured with base credentials
        """
        session_kwargs = AWSIntegration._get_session_kwargs(auth_config)
        return boto3.Session(**session_kwargs)

    @staticmethod
    def _assume_role_sync(base_session: boto3.Session, auth_config: Dict[str, str]) -> boto3.Session:
        """
        Assume a role for cross-account access (SYNC version).

        Args:
            base_session: Base boto3 session with initial credentials
            auth_config: Auth configuration containing assume_role_arn

        Returns:
            boto3.Session with assumed role credentials
        """
        role_arn = auth_config.get("assume_role_arn")
        if not role_arn:
            raise ValueError("assume_role_arn is required for AssumeRole operation")

        sts_client = base_session.client("sts")

        assume_role_params = {
            "RoleArn": role_arn,
            "RoleSessionName": auth_config.get("session_name", "ObsToolSession"),
            "DurationSeconds": int(auth_config.get("session_duration", 3600))
        }

        if "external_id" in auth_config:
            assume_role_params["ExternalId"] = auth_config["external_id"]

        try:
            response = sts_client.assume_role(**assume_role_params)
            credentials = response["Credentials"]

            return boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=auth_config.get("region", "us-east-1")
            )
        except ClientError as e:
            logger.error(f"Failed to assume role {role_arn}: {str(e)}")
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "AccessDenied":
                raise PermissionError(
                    "Permission denied: unable to access the target AWS account. "
                    "Please verify the IAM role trust policy and permissions are configured correctly."
                )
            raise Exception(f"AssumeRole failed: {str(e)}")

    @staticmethod
    def get_client(auth_config: Dict[str, str], service_name: str, use_assume_role: bool = True):
        """
        Get AWS service client with proper authentication (SYNC version).

        This method is provided for backwards compatibility with code that
        requires synchronous AWS clients (e.g., VPC discovery).

        For new code, prefer using the async methods.

        Args:
            auth_config: Authentication configuration dict
            service_name: AWS service name (e.g., 'secretsmanager', 'ssm', 's3', 'ec2')
            use_assume_role: Whether to use AssumeRole if assume_role_arn is present

        Returns:
            boto3 client for the specified service

        Example:
            client = AWSIntegration.get_client(auth_config, 'ec2')
        """
        base_session = AWSIntegration._get_base_session_sync(auth_config)

        if use_assume_role and "assume_role_arn" in auth_config:
            session = AWSIntegration._assume_role_sync(base_session, auth_config)
        else:
            session = base_session

        return session.client(service_name)

    # ==================== SECRETS MANAGER OPERATIONS ====================

    @staticmethod
    async def get_secret(auth_config: Dict[str, str], secret_name: str) -> Dict[str, Any]:
        """
        Retrieve a specific secret by name from AWS Secrets Manager.

        Args:
            auth_config: Authentication configuration dict
            secret_name: Exact name or ARN of the secret (e.g., "prod/database/password")

        Returns:
            Dict containing secret data:
            {
                "name": str,
                "value": str or dict,  # String or JSON-parsed dict
                "arn": str,
                "version_id": str,
                "created_date": datetime,
                "last_updated_date": datetime
            }

        Raises:
            Exception: If secret retrieval fails

        Example:
            secret = await AWSIntegration.get_secret(auth_config, "prod/api/key")
            api_key = secret["value"]
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("secretsmanager", config=config) as client:
                response = await client.get_secret_value(SecretId=secret_name)

                secret_value = response.get("SecretString")
                try:
                    parsed_value = json.loads(secret_value)
                except (json.JSONDecodeError, TypeError):
                    parsed_value = secret_value

                return {
                    "name": response.get("Name"),
                    "value": parsed_value,
                    "arn": response.get("ARN"),
                    "version_id": response.get("VersionId"),
                    "created_date": response.get("CreatedDate"),
                    "last_updated_date": response.get("LastChangedDate", response.get("CreatedDate"))
                }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                raise Exception(f"Secret not found: {secret_name}")
            elif error_code == "AccessDeniedException":
                raise Exception(f"Access denied to secret: {secret_name}")
            else:
                logger.error(f"Failed to get secret {secret_name}: {str(e)}")
                raise Exception(f"Failed to retrieve secret: {str(e)}")

    @staticmethod
    async def kms_encrypt(
        auth_config: Dict[str, str],
        key_id: str,
        plaintext: str,
    ) -> str:
        """
        Encrypt a plaintext string with an AWS KMS key.

        Args:
            auth_config: Authentication configuration dict
            key_id: KMS key id, alias (alias/xxx) or ARN
            plaintext: Value to encrypt

        Returns:
            Base64-encoded ciphertext blob (safe to embed in JSON)
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("kms", config=config) as client:
                response = await client.encrypt(
                    KeyId=key_id,
                    Plaintext=plaintext.encode("utf-8"),
                )
                return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            logger.error(f"KMS encrypt failed with key {key_id}: {str(e)}")
            if error_code == "NotFoundException":
                raise Exception(f"KMS key not found: {key_id}")
            elif error_code == "AccessDeniedException":
                raise Exception(f"Access denied to KMS key: {key_id}")
            raise Exception(f"Failed to encrypt with KMS: {str(e)}")

    @staticmethod
    async def kms_encrypt_many(
        auth_config: Dict[str, str],
        key_id: str,
        plaintexts: List[str],
        concurrency: int = 16,
    ) -> List[Any]:
        """Encrypt many values under ONE KMS client (one TLS connection, reused).

        Building the aioboto3 client (TLS handshake + endpoint resolution) costs
        ~250ms and dwarfs the encrypt call itself; doing it per value makes a
        1000-secret clone take minutes. Opening the client once and firing all
        encrypts through it collapses that to a couple of seconds.

        Returns one entry per input, positionally aligned. A per-value failure is
        returned as the Exception (never raised) so one bad value cannot fail the
        whole batch — the caller maps it to a per-key error. Client-setup / auth /
        key errors surface before any encrypt and DO raise (nothing can succeed).
        """
        if not plaintexts:
            return []

        session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
        config = AWSIntegration._get_config()
        session = aioboto3.Session(**session_kwargs)
        sem = asyncio.Semaphore(max(1, concurrency))

        async with session.client("kms", config=config) as client:
            async def _one(plaintext: str) -> str:
                async with sem:
                    response = await client.encrypt(
                        KeyId=key_id,
                        Plaintext=plaintext.encode("utf-8"),
                    )
                    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")

            return await asyncio.gather(
                *[_one(pt) for pt in plaintexts], return_exceptions=True
            )

    @staticmethod
    async def create_secret(
        auth_config: Dict[str, str],
        secret_name: str,
        secret_value: Any,
        description: Optional[str] = None,
        tags: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Create a new secret in AWS Secrets Manager.

        Args:
            auth_config: Authentication configuration dict
            secret_name: Name for the new secret (e.g., "prod/database/password")
            secret_value: Secret value (string or dict - will be JSON-serialized)
            description: Optional description
            tags: Optional list of tags [{"Key": "Environment", "Value": "prod"}]

        Returns:
            Dict containing:
            {
                "arn": str,
                "name": str,
                "version_id": str
            }

        Raises:
            Exception: If secret creation fails

        Example:
            result = await AWSIntegration.create_secret(
                auth_config,
                "prod/api/key",
                {"api_key": "secret123", "api_secret": "secret456"},
                description="Production API credentials"
            )
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            if isinstance(secret_value, dict):
                secret_string = json.dumps(secret_value)
            else:
                secret_string = str(secret_value)

            params = {
                "Name": secret_name,
                "SecretString": secret_string
            }

            if description:
                params["Description"] = description
            if tags:
                params["Tags"] = tags

            async with session.client("secretsmanager", config=config) as client:
                response = await client.create_secret(**params)

                return {
                    "arn": response.get("ARN"),
                    "name": response.get("Name"),
                    "version_id": response.get("VersionId")
                }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = str(e)

            if error_code == "ResourceExistsException":
                raise Exception(f"Secret already exists: {secret_name}")
            elif error_code == "InvalidRequestException" and "scheduled for deletion" in error_message:
                raise Exception(
                    f"Secret '{secret_name}' is scheduled for deletion. "
                    f"To create a new secret with this name, you must either:\n"
                    f"1. Wait for the scheduled deletion to complete (7-30 days)\n"
                    f"2. Restore and force-delete manually using AWS CLI:\n"
                    f"   aws secretsmanager restore-secret --secret-id {secret_name}\n"
                    f"   aws secretsmanager delete-secret --secret-id {secret_name} --force-delete-without-recovery"
                )
            else:
                logger.error(f"Failed to create secret {secret_name}: {error_message}")
                raise Exception(f"Failed to create secret: {error_message}")

    @staticmethod
    async def update_secret(
        auth_config: Dict[str, str],
        secret_name: str,
        secret_value: Any,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update an existing secret in AWS Secrets Manager.

        Args:
            auth_config: Authentication configuration dict
            secret_name: Name or ARN of the secret to update
            secret_value: New secret value (string or dict)
            description: Optional updated description

        Returns:
            Dict containing:
            {
                "arn": str,
                "name": str,
                "version_id": str
            }

        Example:
            result = await AWSIntegration.update_secret(
                auth_config,
                "prod/api/key",
                {"api_key": "newsecret123", "api_secret": "newsecret456"}
            )
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            if isinstance(secret_value, dict):
                secret_string = json.dumps(secret_value)
            else:
                secret_string = str(secret_value)

            params = {
                "SecretId": secret_name,
                "SecretString": secret_string
            }

            if description:
                params["Description"] = description

            async with session.client("secretsmanager", config=config) as client:
                response = await client.update_secret(**params)

                return {
                    "arn": response.get("ARN"),
                    "name": response.get("Name"),
                    "version_id": response.get("VersionId")
                }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = str(e)

            if error_code == "ResourceNotFoundException":
                raise Exception(f"Secret not found: {secret_name}")
            elif error_code == "InvalidRequestException" and "scheduled for deletion" in error_message:
                raise Exception(
                    f"Secret '{secret_name}' is scheduled for deletion and cannot be updated. "
                    f"To update this secret, you must first restore it:\n"
                    f"   aws secretsmanager restore-secret --secret-id {secret_name}"
                )
            else:
                logger.error(f"Failed to update secret {secret_name}: {error_message}")
                raise Exception(f"Failed to update secret: {error_message}")

    # ==================== SSM PARAMETER STORE OPERATIONS ====================

    @staticmethod
    async def get_parameter(
        auth_config: Dict[str, str],
        parameter_name: str,
        with_decryption: bool = True
    ) -> Dict[str, Any]:
        """
        Retrieve a specific parameter by name from AWS Systems Manager Parameter Store.

        Args:
            auth_config: Authentication configuration dict
            parameter_name: Exact name of the parameter (e.g., "/app/prod/database/host")
            with_decryption: Whether to decrypt SecureString parameters

        Returns:
            Dict containing:
            {
                "name": str,
                "value": str,
                "type": str,  # "String", "StringList", or "SecureString"
                "version": int,
                "last_modified_date": datetime,
                "arn": str
            }

        Example:
            param = await AWSIntegration.get_parameter(auth_config, "/app/prod/database/host")
            db_host = param["value"]
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("ssm", config=config) as client:
                response = await client.get_parameter(
                    Name=parameter_name,
                    WithDecryption=with_decryption
                )

                param = response.get("Parameter", {})
                return {
                    "name": param.get("Name"),
                    "value": param.get("Value"),
                    "type": param.get("Type"),
                    "version": param.get("Version"),
                    "last_modified_date": param.get("LastModifiedDate"),
                    "arn": param.get("ARN")
                }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ParameterNotFound":
                raise Exception(f"Parameter not found: {parameter_name}")
            else:
                logger.error(f"Failed to get parameter {parameter_name}: {str(e)}")
                raise Exception(f"Failed to retrieve parameter: {str(e)}")

    @staticmethod
    async def get_parameters_by_path(
        auth_config: Dict[str, str],
        path: str,
        recursive: bool = True,
        with_decryption: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Retrieve all parameters under a specific path from SSM Parameter Store.

        Args:
            auth_config: Authentication configuration dict
            path: Parameter path prefix (e.g., "/app/prod/")
            recursive: Whether to retrieve all parameters under the path hierarchy
            with_decryption: Whether to decrypt SecureString parameters

        Returns:
            List of parameter dicts:
            [{
                "name": str,
                "value": str,
                "type": str,
                "version": int,
                "last_modified_date": datetime,
                "arn": str
            }]

        Example:
            # Get all parameters under /app/prod/
            params = await AWSIntegration.get_parameters_by_path(auth_config, "/app/prod/")
            for param in params:
                print(f"{param['name']}: {param['value']}")
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            parameters = []

            async with session.client("ssm", config=config) as client:
                paginator = client.get_paginator("get_parameters_by_path")
                async for page in paginator.paginate(
                    Path=path,
                    Recursive=recursive,
                    WithDecryption=with_decryption
                ):
                    for param in page.get("Parameters", []):
                        parameters.append({
                            "name": param.get("Name"),
                            "value": param.get("Value"),
                            "type": param.get("Type"),
                            "version": param.get("Version"),
                            "last_modified_date": param.get("LastModifiedDate"),
                            "arn": param.get("ARN")
                        })

            return parameters
        except ClientError as e:
            logger.error(f"Failed to get parameters by path {path}: {str(e)}")
            raise Exception(f"Failed to retrieve parameters: {str(e)}")

    @staticmethod
    async def put_parameter(
        auth_config: Dict[str, str],
        parameter_name: str,
        parameter_value: str,
        parameter_type: str = "String",
        description: Optional[str] = None,
        overwrite: bool = False,
        tags: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Create or update a parameter in SSM Parameter Store.

        Args:
            auth_config: Authentication configuration dict
            parameter_name: Name of the parameter (e.g., "/app/prod/database/host")
            parameter_value: Value of the parameter
            parameter_type: Type ("String", "StringList", or "SecureString")
            description: Optional description
            overwrite: Whether to overwrite existing parameter
            tags: Optional list of tags [{"Key": "Environment", "Value": "prod"}]

        Returns:
            Dict containing:
            {
                "version": int,

                "tier": str,
                "arn": str,
                "name": str,
                "type": str
            }

        Example:
            result = await AWSIntegration.put_parameter(
                auth_config,
                "/app/prod/database/host",
                "db.example.com",
                parameter_type="String",
                overwrite=True
            )
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            params = {
                "Name": parameter_name,
                "Value": parameter_value,
                "Type": parameter_type
            }

            # AWS API restriction: tags and overwrite cannot be used together
            if tags:
                params["Tags"] = tags
            else:
                params["Overwrite"] = overwrite

            if description:
                params["Description"] = description

            async with session.client("ssm", config=config) as client:
                response = await client.put_parameter(**params)

            # Fetch the parameter to get the ARN
            param_info = await AWSIntegration.get_parameter(auth_config, parameter_name, with_decryption=False)

            return {
                "version": response.get("Version"),
                "tier": response.get("Tier", "Standard"),
                "arn": param_info.get("arn"),
                "name": param_info.get("name"),
                "type": param_info.get("type")
            }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ParameterAlreadyExists":
                raise Exception(f"Parameter already exists: {parameter_name}. Use overwrite=True to update.")
            else:
                logger.error(f"Failed to put parameter {parameter_name}: {str(e)}")
                raise Exception(f"Failed to put parameter: {str(e)}")

    @staticmethod
    async def delete_parameter(
        auth_config: Dict[str, str],
        parameter_name: str,
    ) -> None:
        """Delete an SSM parameter. No error if it doesn't exist (idempotent)."""
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("ssm", config=config) as client:
                await client.delete_parameter(Name=parameter_name)
                logger.info(f"Deleted SSM parameter: {parameter_name}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ParameterNotFound":
                logger.info(f"SSM parameter already absent: {parameter_name}")
                return
            logger.error(f"Failed to delete parameter {parameter_name}: {str(e)}")
            raise Exception(f"Failed to delete parameter: {str(e)}")

    @staticmethod
    async def ssm_write_bulk(
        auth_config: Dict[str, str],
        *,
        puts: Optional[Dict[str, str]] = None,
        deletes: Optional[List[str]] = None,
        parameter_type: str = "String",
        concurrency: int = 3,
    ) -> Tuple[Dict[str, bool], Dict[str, bool]]:
        """Put and delete many SSM parameters over ONE shared client.

        ``puts`` maps ``{name: value}`` (Overwrite=True); ``deletes`` is a list of
        names. Returns ``(put_results, delete_results)``, each ``{name: True/False}``.

        Throughput reality: SSM ``PutParameter``/``DeleteParameter`` are hard-capped
        at **3 TPS on standard** (10 TPS with high-throughput) — far below the 40 TPS
        READ limit. So a large variable set is inherently slow: 1000 params ≈ ~333s
        on standard, ~100s on high-throughput. There is NO client-side trick past
        this (AWS's own guidance: keep write concurrency at 2-3 on standard).

        We therefore use ``concurrency=3`` (matches the standard write rate, so
        almost nothing throttles → no error spam) with ``adaptive`` retry, which
        self-tunes to the real sustained rate and completes with NO failures. When
        high-throughput is enabled on the account, pass ``concurrency=10`` to match
        the higher write rate. Truly fast variable deploys require consolidating the
        writes (one object instead of one parameter per key), not client tuning.
        """
        puts = puts or {}
        deletes = deletes or []
        put_out: Dict[str, bool] = {k: False for k in puts}
        del_out: Dict[str, bool] = {k: False for k in deletes}
        if not puts and not deletes:
            return put_out, del_out

        session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
        config = Config(
            signature_version="v4",
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=max(concurrency, 10),
        )
        session = aioboto3.Session(**session_kwargs)
        sem = asyncio.Semaphore(max(1, concurrency))

        async with session.client("ssm", config=config) as client:
            async def _put(name: str, value: str):
                async with sem:
                    try:
                        await client.put_parameter(
                            Name=name, Value=value, Type=parameter_type, Overwrite=True
                        )
                        return name, True
                    except Exception as e:
                        logger.error(f"SSM bulk put failed for '{name}': {e}")
                        return name, False

            async def _del(name: str):
                async with sem:
                    try:
                        await client.delete_parameter(Name=name)
                        return name, True
                    except ClientError as e:
                        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
                            return name, True
                        logger.error(f"SSM bulk delete failed for '{name}': {e}")
                        return name, False
                    except Exception as e:
                        logger.error(f"SSM bulk delete failed for '{name}': {e}")
                        return name, False

            put_res, del_res = await asyncio.gather(
                asyncio.gather(*[_put(k, v) for k, v in puts.items()]),
                asyncio.gather(*[_del(k) for k in deletes]),
            )
            for name, ok in put_res:
                put_out[name] = ok
            for name, ok in del_res:
                del_out[name] = ok
        return put_out, del_out

    # ==================== S3 OBJECT OPERATIONS ====================

    @staticmethod
    async def list_objects(
        auth_config: Dict[str, str],
        bucket: str,
        prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """
        List objects under a prefix in an S3 bucket (paginated, all pages).

        Args:
            auth_config: Authentication configuration dict
            bucket: S3 bucket name
            prefix: Key prefix to list under (e.g., "workspace/tenant/app/prod/")

        Returns:
            List of object dicts:
            [{"key": str, "size": int, "last_modified": datetime, "etag": str}]

        Example:
            objs = await AWSIntegration.list_objects(auth_config, "my-bucket", "a/b/")
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            objects: List[Dict[str, Any]] = []
            async with session.client("s3", config=config) as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        objects.append({
                            "key": obj.get("Key"),
                            "size": obj.get("Size"),
                            "last_modified": obj.get("LastModified"),
                            "etag": obj.get("ETag"),
                        })
            return objects
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("NoSuchBucket",):
                raise Exception(f"Bucket not found: {bucket}")
            logger.error(f"Failed to list objects in {bucket}/{prefix}: {str(e)}")
            raise Exception(f"Failed to list objects: {str(e)}")

    @staticmethod
    async def get_object(
        auth_config: Dict[str, str],
        bucket: str,
        key: str,
    ) -> Dict[str, Any]:
        """
        Fetch a single object from S3 and return its decoded body.

        Args:
            auth_config: Authentication configuration dict
            bucket: S3 bucket name
            key: Full object key

        Returns:
            Dict containing:
            {
                "key": str,
                "body": str,            # UTF-8 decoded content
                "last_modified": datetime,
                "version_id": str,      # S3 object version id (if bucket versioning on)
                "etag": str
            }

        Raises:
            Exception: If the object does not exist or retrieval fails
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("s3", config=config) as client:
                response = await client.get_object(Bucket=bucket, Key=key)
                async with response["Body"] as stream:
                    raw = await stream.read()

                return {
                    "key": key,
                    "body": raw.decode("utf-8"),
                    "last_modified": response.get("LastModified"),
                    "version_id": response.get("VersionId"),
                    "etag": response.get("ETag"),
                }
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("NoSuchKey", "404"):
                raise Exception(f"Object not found: {bucket}/{key}")
            elif error_code in ("AccessDenied", "403"):
                raise Exception(f"Access denied to object: {bucket}/{key}")
            logger.error(f"Failed to get object {bucket}/{key}: {str(e)}")
            raise Exception(f"Failed to retrieve object: {str(e)}")

    # ==================== KMS OPERATIONS ====================

    @staticmethod
    async def kms_decrypt(
        auth_config: Dict[str, str],
        ciphertext_b64: str,
        key_id: Optional[str] = None,
    ) -> str:
        """
        Decrypt a base64-encoded KMS ciphertext blob and return the UTF-8 plaintext.

        Used for client-side-encrypted env values: each value is an independent
        base64 KMS ciphertext produced by kms.Encrypt. The CMK is embedded in the
        blob, so key_id is optional; when provided it is passed as KeyId to
        validate the ciphertext was encrypted under the expected key.

        Args:
            auth_config: Authentication configuration dict
            ciphertext_b64: Base64-encoded KMS CiphertextBlob
            key_id: Optional KMS key id/ARN to enforce on decrypt

        Returns:
            Decrypted plaintext string

        Raises:
            Exception: If the blob is not valid base64 or decryption fails
        """
        try:
            blob = base64.b64decode(ciphertext_b64)
        except Exception:
            raise Exception("Invalid base64 KMS ciphertext")

        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            params: Dict[str, Any] = {"CiphertextBlob": blob}
            if key_id:
                params["KeyId"] = key_id

            async with session.client("kms", config=config) as client:
                response = await client.decrypt(**params)
                return response["Plaintext"].decode("utf-8")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "AccessDeniedException":
                raise Exception("Access denied for kms:Decrypt on the KMS key")
            logger.error(f"Failed to KMS-decrypt value: {str(e)}")
            raise Exception(f"Failed to decrypt value: {str(e)}")

    @staticmethod
    async def kms_decrypt_bulk(
        auth_config: Dict[str, str],
        items: Dict[str, str],
        concurrency: int = 32,
    ) -> Dict[str, Optional[str]]:
        """Decrypt many base64 KMS ciphertexts over ONE shared KMS client.

        Takes ``{key: ciphertext}`` and returns ``{key: plaintext}`` (None on a
        per-item failure, so one bad blob never fails the batch). Keyed by the
        caller's own id, so results carry their key explicitly — no reliance on
        list order. One client + a connection pool sized to ``concurrency`` serves
        every decrypt, so N blobs cost ~``ceil(N / concurrency)`` round-trips
        instead of N separate client-per-call decrypts (the slow path).
        """
        results: Dict[str, Optional[str]] = {k: None for k in items}
        if not items:
            return results

        session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
        # Pool must be >= concurrency, else the shared client serialises at 10.
        config = Config(
            signature_version="v4",
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=max(concurrency, 10),
        )
        session = aioboto3.Session(**session_kwargs)
        sem = asyncio.Semaphore(max(1, concurrency))

        async with session.client("kms", config=config) as client:
            async def _one(key: str, ct: str):
                async with sem:
                    try:
                        blob = base64.b64decode(ct)
                    except Exception:
                        return key, None
                    try:
                        resp = await client.decrypt(CiphertextBlob=blob)
                        return key, resp["Plaintext"].decode("utf-8")
                    except Exception as e:
                        logger.error(f"KMS bulk decrypt failed for '{key}': {e}")
                        return key, None

            for key, pt in await asyncio.gather(
                *[_one(k, ct) for k, ct in items.items()]
            ):
                results[key] = pt
        return results

    # ========================================================================
    # ECR (Elastic Container Registry) Operations
    # ========================================================================

    @staticmethod
    async def create_ecr_repository(
        auth_config: Dict[str, Any],
        repository_name: str,
        tags: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Create an ECR repository for container images.

        Args:
            auth_config: AWS authentication configuration
            repository_name: Name of the ECR repository (e.g., 'acme_us-east-1_prod_payment-api')
            tags: Optional list of tags [{"Key": "Name", "Value": "val"}]

        Returns:
            Dict containing:
                - repository_uri: URI for pushing/pulling images
                - repository_arn: ARN of the repository
                - registry_id: AWS account ID
                - repository_name: Name of the repository

        Raises:
            Exception: If repository creation fails
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            create_params = {
                "repositoryName": repository_name,
                "imageScanningConfiguration": {"scanOnPush": True},
                "imageTagMutability": "MUTABLE",
            }

            if tags:
                create_params["tags"] = tags

            async with session.client("ecr", config=config) as ecr_client:
                try:
                    response = await ecr_client.create_repository(**create_params)
                    repository = response["repository"]

                    logger.info(f"Created ECR repository: {repository_name}")

                    # Set lifecycle policy to keep last 10 images
                    lifecycle_policy = {
                        "rules": [
                            {
                                "rulePriority": 1,
                                "description": "Keep last 10 images",
                                "selection": {
                                    "tagStatus": "any",
                                    "countType": "imageCountMoreThan",
                                    "countNumber": 10
                                },
                                "action": {"type": "expire"}
                            }
                        ]
                    }

                    await ecr_client.put_lifecycle_policy(
                        repositoryName=repository_name,
                        lifecyclePolicyText=json.dumps(lifecycle_policy)
                    )

                    return {
                        "repository_uri": repository["repositoryUri"],
                        "repository_arn": repository["repositoryArn"],
                        "registry_id": repository["registryId"],
                        "repository_name": repository["repositoryName"],
                        "created_at": repository.get("createdAt")
                    }

                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")
                    if error_code == "RepositoryAlreadyExistsException":
                        logger.info(f"ECR repository already exists: {repository_name}")
                        response = await ecr_client.describe_repositories(
                            repositoryNames=[repository_name]
                        )
                        repository = response["repositories"][0]
                        return {
                            "repository_uri": repository["repositoryUri"],
                            "repository_arn": repository["repositoryArn"],
                            "registry_id": repository["registryId"],
                            "repository_name": repository["repositoryName"],
                            "created_at": repository.get("createdAt")
                        }
                    else:
                        raise

        except ClientError as e:
            logger.error(f"Failed to create ECR repository {repository_name}: {str(e)}")
            raise Exception(f"Failed to create ECR repository: {str(e)}")

    # ========================================================================
    # IAM Role Operations for GitHub OIDC
    # ========================================================================

    @staticmethod
    async def ensure_github_oidc_provider(auth_config: Dict[str, Any]) -> str:
        """
        Ensure GitHub OIDC provider exists in AWS account.
        Creates it if it doesn't exist.

        Args:
            auth_config: AWS authentication configuration

        Returns:
            Provider ARN

        Raises:
            Exception: If provider check/creation fails
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("iam", config=config) as iam_client:
                async with session.client("sts", config=config) as sts_client:
                    # Get AWS account ID
                    identity = await sts_client.get_caller_identity()
                    account_id = identity["Account"]

                provider_url = "token.actions.githubusercontent.com"
                provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/{provider_url}"

                try:
                    await iam_client.get_open_id_connect_provider(
                        OpenIDConnectProviderArn=provider_arn
                    )
                    logger.info(f"GitHub OIDC provider already exists: {provider_arn}")
                    return provider_arn
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "NoSuchEntity":
                        logger.info("Creating GitHub OIDC provider...")
                        response = await iam_client.create_open_id_connect_provider(
                            Url=f"https://{provider_url}",
                            ClientIDList=["sts.amazonaws.com"],
                            ThumbprintList=["6938fd4d98bab03faadb97b34396831e3780aea1"]
                        )
                        logger.info(f"Created GitHub OIDC provider: {response['OpenIDConnectProviderArn']}")
                        return response["OpenIDConnectProviderArn"]
                    else:
                        raise

        except ClientError as e:
            logger.error(f"Failed to ensure GitHub OIDC provider: {str(e)}")
            raise Exception(f"Failed to ensure GitHub OIDC provider: {str(e)}")

    @staticmethod
    async def create_github_oidc_role(
        auth_config: Dict[str, Any],
        role_name: str,
        github_repo: str,
        branch_name: str,
        ecr_repo_arn: str
    ) -> Dict[str, Any]:
        """
        Create IAM role for GitHub Actions with OIDC authentication.

        Args:
            auth_config: AWS authentication configuration
            role_name: Name for the IAM role (e.g., 'GitHubActions_payment_api_company-payment-service_prod')
            github_repo: GitHub repository in format 'org/repo'
            branch_name: Git branch name
            ecr_repo_arn: ARN of the ECR repository

        Returns:
            Dict containing:
                - role_arn: ARN of the created role
                - role_name: Name of the role
                - trust_policy: Trust policy document

        Raises:
            Exception: If role creation fails
        """
        try:
            session_kwargs = await AWSIntegration._get_client_kwargs(auth_config)
            config = AWSIntegration._get_config()
            session = aioboto3.Session(**session_kwargs)

            async with session.client("iam", config=config) as iam_client:
                async with session.client("sts", config=config) as sts_client:
                    # Get AWS account ID and OIDC provider ARN
                    identity = await sts_client.get_caller_identity()
                    account_id = identity["Account"]

                oidc_provider_arn = await AWSIntegration.ensure_github_oidc_provider(auth_config)
                oidc_provider = oidc_provider_arn.split("/")[-1]

                # Build trust policy for GitHub OIDC
                trust_policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "Federated": oidc_provider_arn
                            },
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Condition": {
                                "StringEquals": {
                                    f"{oidc_provider}:aud": "sts.amazonaws.com"
                                },
                                "StringLike": {
                                    f"{oidc_provider}:sub": f"repo:{github_repo}:ref:refs/heads/{branch_name}"
                                }
                            }
                        }
                    ]
                }

                # Create IAM role
                try:
                    response = await iam_client.create_role(
                        RoleName=role_name,
                        AssumeRolePolicyDocument=json.dumps(trust_policy),
                        Description=f"GitHub Actions deployment role for {github_repo}",
                        Tags=[
                            {"Key": "ManagedBy", "Value": "ObsTool"},
                            {"Key": "GitHubRepo", "Value": github_repo},
                            {"Key": "Branch", "Value": branch_name}
                        ]
                    )
                    role_arn = response["Role"]["Arn"]
                    logger.info(f"Created IAM role: {role_name}")
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "EntityAlreadyExists":
                        logger.info(f"IAM role already exists: {role_name}")
                        response = await iam_client.get_role(RoleName=role_name)
                        role_arn = response["Role"]["Arn"]
                    else:
                        raise

                # Attach ECR PowerUser managed policy
                try:
                    await iam_client.attach_role_policy(
                        RoleName=role_name,
                        PolicyArn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser"
                    )
                    logger.info("Attached AmazonEC2ContainerRegistryPowerUser policy")
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") != "PolicyAlreadyAttached":
                        logger.warning(f"Failed to attach ECR policy: {str(e)}")

                # Create and attach custom inline policy for ECS/EC2/Logs/SSM
                inline_policy = {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ecs:UpdateService",
                                "ecs:DescribeServices",
                                "ecs:RegisterTaskDefinition",
                                "ecs:DescribeTaskDefinition",
                                "ecs:ListTasks",
                                "ecs:DescribeTasks",
                                "ecs:CreateService"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ec2:DescribeInstances",
                                "ec2:DescribeSecurityGroups",
                                "ec2:DescribeSubnets",
                                "ec2:DescribeVpcs"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                                "logs:DescribeLogStreams"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssm:SendCommand"
                            ],
                            "Resource": [
                                f"arn:aws:ec2:*:{account_id}:instance/*",
                                "arn:aws:ssm:*::document/AWS-RunShellScript"
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ssm:GetCommandInvocation",
                                "ssm:ListCommandInvocations",
                                "ssm:ListCommands",
                                "ssm:DescribeInstanceInformation"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": "iam:PassRole",
                            "Resource": "*",
                            "Condition": {
                                "StringLike": {
                                    "iam:PassedToService": "ecs-tasks.amazonaws.com"
                                }
                            }
                        }
                    ]
                }

                try:
                    await iam_client.put_role_policy(
                        RoleName=role_name,
                        PolicyName="GitHubActionsDeploymentPolicy",
                        PolicyDocument=json.dumps(inline_policy)
                    )
                    logger.info("Attached custom deployment policy")
                except ClientError as e:
                    logger.warning(f"Failed to attach custom policy: {str(e)}")

                return {
                    "role_arn": role_arn,
                    "role_name": role_name,
                    "trust_policy": trust_policy
                }

        except ClientError as e:
            logger.error(f"Failed to create GitHub OIDC role {role_name}: {str(e)}")
            raise Exception(f"Failed to create IAM role: {str(e)}")
