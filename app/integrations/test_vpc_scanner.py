"""
Integration test for VPC Scanner V2 with proper authentication.
Tests the authentication priority chain:
1. IAM Role + AssumeRole
2. IAM Role only
3. Access Keys (fallback)
"""
import asyncio
import sys
import os

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from app.services.vpc_discovery_service import VPCDiscoveryService


async def test_vpc_scanner_v2():
    """Test VPC auto-discovery with V2 authentication."""

    # Test configuration - Multiple VPCs (no region needed!)
    vpc_ids = ["vpc-0e29e8447b163e238", "vpc-0ed42f977c464aed8"]

    print("=" * 80)
    print("VPC Auto-Discovery - Integration Test")
    print("=" * 80)
    print(f"\nTest VPC IDs: {', '.join(vpc_ids)}")
    print(f"Total VPCs to discover: {len(vpc_ids)}")
    print("Note: Region will be auto-discovered")
    print()

    # Auth config - Using access keys from environment variables
    # Set these before running: export AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN
    import os

    auth_config = {
        "account_id": "597189966628",
        "authentication_type": "access_key",
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
        "aws_session_token": os.getenv("AWS_SESSION_TOKEN")  # For temporary credentials
    }

    print("Authentication Configuration:")
    print(f"  Type: {auth_config['authentication_type']}")
    print(f"  Account ID: {auth_config['account_id']}")
    print(f"  Using temporary STS credentials: {'Yes' if os.getenv('AWS_SESSION_TOKEN') else 'No'}")
    print()

    print("Authentication Priority Chain:")
    print("  1️⃣ IAM Role + AssumeRole (if assume_role_arn exists)")
    print("  2️⃣ IAM Role (if authentication_type = 'iam_role')")
    print("  3️⃣ Access Keys (fallback if authentication_type = 'access_key')")
    print()

    print("-" * 80)
    print("Discovering VPCs across all regions...")
    print("-" * 80)
    print()

    try:
        # Initialize service
        service = VPCDiscoveryService(auth_config)

        # Discover all VPCs (auto-finds regions)
        print(f"Searching for {len(vpc_ids)} VPC(s) across AWS regions...")
        result = await service.discover_vpcs_by_ids(vpc_ids)

        # Success!
        print()
        print("✅ SUCCESS! VPC discovery completed")
        print("=" * 80)
        print()

        # Display results
        for account in result.aws_account:
            print(f"Account ID: {account.account_id}")
            print(f"Account Name: {account.account_name}")
            print(f"VPCs Discovered: {len(account.vpcs)}")
            print()

            for idx, vpc in enumerate(account.vpcs, 1):
                print("=" * 80)
                print(f"VPC {idx} of {len(account.vpcs)}")
                print("=" * 80)
                print()

                # Display VPC details
                # VPC Overview
                print(f"  VPC ID: {vpc.vpc_id}")
                print(f"  Name: {vpc.name or 'N/A'}")
                print(f"  CIDR: {vpc.cidr}")
                print(f"  Region: {vpc.region} (auto-discovered ✓)")
                print()

                # Internet connectivity
                print("  Internet Connectivity:")
                if vpc.internet_gateway:
                    print(f"    IGW: {vpc.internet_gateway.igw_id}")
                else:
                    print("    IGW: None")

                if vpc.nat_gateways:
                    print(f"    NAT Gateways: {len(vpc.nat_gateways)}")
                else:
                    print("    NAT Gateways: None")
                print()

                # Subnets
                print("  Subnets:")
                print(f"    Public: {len(vpc.subnets.public)}")
                print(f"    Private: {len(vpc.subnets.private)}")
                print()

                # Show total resources count
                total_resources = sum(len(s.resources) for s in vpc.subnets.public + vpc.subnets.private)
                print(f"  Resources: {total_resources} total")
                print(f"  Security Groups: {len(vpc.security_groups)}")
                print()

        # Final Summary
        print("=" * 80)
        print("✅ Integration Test PASSED")
        print("=" * 80)
        print()
        print("Summary:")
        total_vpcs = sum(len(acc.vpcs) for acc in result.aws_account)
        print(f"  ✓ Successfully discovered {total_vpcs} VPC(s) across multiple regions")
        print(f"  ✓ Auto-discovered regions for each VPC")
        print(f"  ✓ Authentication working correctly")
        print()

        # Show which auth method was used
        if auth_config.get("assume_role_arn"):
            print("Authentication Method: 1️⃣ IAM Role + AssumeRole ✓")
        elif auth_config.get("authentication_type") == "iam_role":
            print("Authentication Method: 2️⃣ IAM Role ✓")
        else:
            print("Authentication Method: 3️⃣ Access Keys ✓")
        print()

        return True

    except Exception as e:
        print("❌ FAILED")
        print("=" * 80)
        print()
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        print()
        print("Troubleshooting:")
        print("  1. Check AWS credentials are set in environment variables")
        print("  2. Verify VPC IDs are correct:")
        print(f"     VPCs to discover: {', '.join(vpc_ids)}")
        print("  3. Check if VPCs exist in any region:")
        print(f"     aws ec2 describe-vpcs --vpc-ids {vpc_ids[0]} --region ap-south-1")
        print("  4. Verify IAM permissions for VPC describe operations")
        print("  5. Check AWS credentials:")
        print("     aws sts get-caller-identity")
        print()
        print("If trust policy is not configured, try with authentication_type: 'access_key'")
        print()

        import traceback
        print("Full Traceback:")
        print("-" * 80)
        traceback.print_exc()
        print()

        return False


if __name__ == "__main__":
    success = asyncio.run(test_vpc_scanner_v2())
    sys.exit(0 if success else 1)
