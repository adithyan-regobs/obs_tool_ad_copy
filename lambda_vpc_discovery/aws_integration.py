"""
Minimal AWS Integration for Lambda - Authentication Only
Supports IAM Role and AssumeRole for cross-account access.
"""
import aioboto3
import logging
from typing import Dict, Any
from botocore.exceptions import ClientError
from botocore.config import Config

logger = logging.getLogger(__name__)


class AWSIntegration:
    """
    Minimal AWS Integration class for Lambda authentication.
    Only includes methods needed for VPC discovery service.
    """

    @staticmethod
    def _get_config() -> Config:
        """Get boto3 config for AWS operations."""
        return Config(
            signature_version='v4',
            retries={'max_attempts': 3, 'mode': 'standard'}
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
            session_token = auth_config.get("aws_session_token")

            if not access_key or not secret_key:
                raise ValueError("aws_access_key_id and aws_secret_access_key are required for access_key authentication")

            session_kwargs = {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "region_name": region
            }

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
        Assume a role for cross-account access.

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
            "RoleSessionName": auth_config.get("session_name", "VPCDiscoverySession"),
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
            logger.info("same_account=True detected, using Lambda's native IAM role")
            return AWSIntegration._get_session_kwargs(auth_config)

        # Otherwise, use AssumeRole if assume_role_arn is present
        if use_assume_role and "assume_role_arn" in auth_config:
            return await AWSIntegration._assume_role(auth_config)
        else:
            return AWSIntegration._get_session_kwargs(auth_config)
