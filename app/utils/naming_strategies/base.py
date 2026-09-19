"""
Base naming strategy interface.

All tenant-specific naming strategies must inherit from this class.
"""

import re
from abc import ABC, abstractmethod


class NamingStrategy(ABC):
    """
    Abstract base class for naming strategies.

    Each tenant can implement their own naming conventions by
    subclassing this and implementing the abstract methods.
    """

    @staticmethod
    def sanitize_for_aws(name: str) -> str:
        """
        Sanitize name for AWS resource naming.
        Replaces spaces and special characters with hyphens.
        Keeps only alphanumeric and hyphens, converts to lowercase.

        Args:
            name: Name to sanitize

        Returns:
            Sanitized lowercase name
        """
        # Replace spaces and underscores with hyphens
        name = name.replace(" ", "-").replace("_", "-")
        # Remove all characters except alphanumeric and hyphens
        name = re.sub(r'[^a-zA-Z0-9\-]', '', name)
        # Replace multiple consecutive hyphens with single hyphen
        name = re.sub(r'-+', '-', name)
        # Remove leading/trailing hyphens and convert to lowercase
        return name.strip('-').lower()

    @abstractmethod
    def generate_org_name(
        self,
        tenant_code: str,
        application_name: str
    ) -> str:
        """
        Generate organization name for resource naming.

        Args:
            tenant_code: Tenant code
            application_name: Application/product name

        Returns:
            Organization name
        """
        pass

    @abstractmethod
    def generate_ecs_service_name(
        self,
        application_name: str,
        environment: str,
        geo_loc_mst_code: str,
        index: str,
        service_name: str
    ) -> str:
        """
        Generate ECS service name.

        Args:
            application_name: Application/product name
            environment: Environment (dev, staging, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECS service name
        """
        pass

    @abstractmethod
    def generate_ecr_repo_name(
        self,
        org_name: str,
        environment: str,
        geo_loc_mst_code: str,
        index: str,
        service_name: str
    ) -> str:
        """
        Generate ECR repository name (path portion).

        Args:
            org_name: Organization name
            environment: Environment (dev, staging, prod)
            geo_loc_mst_code: Geographic location code (e.g., "mumbai")
            index: Index (e.g., "01")
            service_name: Service name

        Returns:
            ECR repository name
        """
        pass

    def generate_ecr_uri(
        self,
        account_id: str,
        aws_region: str,
        ecr_repo_name: str
    ) -> str:
        """
        Generate full ECR URI.

        Format: {account_id}.dkr.ecr.{aws_region}.amazonaws.com/{ecr_repo_name}

        Args:
            account_id: AWS account ID
            aws_region: AWS region (e.g., "ap-south-1")
            ecr_repo_name: ECR repository name

        Returns:
            Full ECR URI
        """
        return f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{ecr_repo_name}"

    def generate_iam_role_arn(
        self,
        account_id: str,
        role_name: str = "github-actions-role"
    ) -> str:
        """
        Generate IAM role ARN.

        Format: arn:aws:iam::{account_id}:role/{role_name}

        Args:
            account_id: AWS account ID
            role_name: IAM role name

        Returns:
            IAM role ARN
        """
        return f"arn:aws:iam::{account_id}:role/{role_name}"
