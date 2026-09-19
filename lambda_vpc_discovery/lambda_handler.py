"""
AWS Lambda Handler for VPC Discovery
Entry point for Lambda function invocation
"""
import json
import logging
import asyncio
import os
from typing import Dict, Any

from vpc_discovery_service import VPCDiscoveryService

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entry point for VPC discovery.
    
    Expected Input (event):
    {
        "auth_config": {
            "authentication_type": "iam_role",
            "accounts": [
                {
                    "account_id": "123456789012",
                    "region": "ap-south-1",
                    "assume_role_arn": "arn:aws:iam::123456789012:role/VPCDiscoveryRole"
                }
            ]
        },
        "accounts_vpcs": {
            "123456789012": ["vpc-xxx", "vpc-yyy"]
        },
        "application_code": "my-app"
    }
    
    Returns:
    {
        "statusCode": 200,
        "body": JSON string of MultiAccountResponse
    }
    """
    try:
        logger.info("=== VPC Discovery Lambda Started ===")
        
        # Parse event body if it's an API Gateway event
        if "body" in event:
            if isinstance(event["body"], str):
                body = json.loads(event["body"])
            else:
                body = event["body"]
        else:
            body = event
        
        # Extract parameters
        auth_config = body.get("auth_config")
        accounts_vpcs = body.get("accounts_vpcs", {})
        application_code = body.get("application_code", "default")
        
        # Validate inputs
        if not auth_config:
            raise ValueError("auth_config is required")
        
        if not accounts_vpcs:
            raise ValueError("accounts_vpcs is required")
        
        if "accounts" not in auth_config or not isinstance(auth_config["accounts"], list):
            raise ValueError("auth_config must contain 'accounts' array")
        
        logger.info(f"Processing {len(accounts_vpcs)} accounts")
        logger.info(f"Application: {application_code}")
        
        # Run async VPC discovery (now returns canvas response)
        service = VPCDiscoveryService(auth_config)

        canvas_response = asyncio.run(
            service.discover_vpcs_multi_account(auth_config, accounts_vpcs, application_code)
        )

        # Convert Pydantic model to dict
        response_dict = canvas_response.model_dump()

        # Count results from canvas response
        total_nodes = len(response_dict.get("nodes", []))
        total_vpcs = len(response_dict.get("vpcs", []))
        total_subnets = len(response_dict.get("subnets", []))

        logger.info(f"✅ Discovery complete: {total_vpcs} VPCs, {total_subnets} subnets, {total_nodes} nodes")
        logger.info("=== VPC Discovery Lambda Completed ===")

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(response_dict)
        }
        
    except ValueError as e:
        logger.error(f"❌ Validation error: {str(e)}")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": f"Validation error: {str(e)}"})
        }
        
    except Exception as e:
        logger.error(f"❌ Lambda error: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": f"Internal error: {str(e)}"})
        }
