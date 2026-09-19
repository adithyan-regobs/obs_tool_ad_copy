"""
Seed CI/CD Template Reference table with Quick, Full, and Production templates
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models.cicd_template_ref_model import CicdTemplateRefModel
from app.db.models.tenants_mst_model import TenantsMstModel


# Template definitions
TEMPLATE_DEFS = [
    {
        "code": "quick-release-template",
        "name": "Quick Release Workflow",
        "description": "Fast deployment pipeline with essential quality checks. Ideal for development and testing environments.",
        "config": {
            "steps": [
                {
                    "id": "code-checkout",
                    "name": "Code Checkout",
                    "order": 1,
                    "mandatory": True,
                    "enabled": True,
                    "category": "setup",
                    "description": "Checkout source code from repository"
                },
                {
                    "id": "code-quality-check",
                    "name": "Code Quality Check",
                    "order": 4,
                    "mandatory": False,
                    "enabled": True,
                    "category": "quality",
                    "description": "Run linting, formatting, and static analysis",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "build",
                    "name": "Build Application",
                    "order": 5,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Compile/build the application binary",
                    "dependsOn": ["code-checkout", "code-quality-check"]
                },
                {
                    "id": "docker-build",
                    "name": "Docker Build",
                    "order": 7,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Build Docker container image",
                    "dependsOn": ["build"]
                },
                {
                    "id": "slack-notification",
                    "name": "Send Slack Notification",
                    "order": 10,
                    "mandatory": False,
                    "enabled": True,
                    "category": "notification",
                    "description": "Send build and deployment status to Slack",
                    "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"]
                }
            ]
        }
    },
    {
        "code": "full-release-template",
        "name": "Full Release Workflow",
        "description": "Comprehensive CI/CD pipeline with all quality and security checks. Recommended for staging and production environments.",
        "config": {
            "steps": [
                {
                    "id": "code-checkout",
                    "name": "Code Checkout",
                    "order": 1,
                    "mandatory": True,
                    "enabled": True,
                    "category": "setup",
                    "description": "Checkout source code from repository"
                },
                {
                    "id": "pre-security-validation",
                    "name": "Pre-Security Validation",
                    "order": 2,
                    "mandatory": False,
                    "enabled": True,
                    "category": "quality",
                    "description": "Validate code and configuration before build",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "env-validation",
                    "name": "Environment Validation",
                    "order": 3,
                    "mandatory": False,
                    "enabled": True,
                    "category": "setup",
                    "description": "Validate environment configuration and variables",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "code-quality-check",
                    "name": "Code Quality Check",
                    "order": 4,
                    "mandatory": False,
                    "enabled": True,
                    "category": "quality",
                    "description": "Run linting, formatting, and static analysis",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "build",
                    "name": "Build Application",
                    "order": 5,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Compile/build the application binary",
                    "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"]
                },
                {
                    "id": "post-security",
                    "name": "Post-Security Scan",
                    "order": 6,
                    "mandatory": False,
                    "enabled": True,
                    "category": "quality",
                    "description": "Run security scans after build",
                    "dependsOn": ["build"]
                },
                {
                    "id": "docker-build",
                    "name": "Docker Build",
                    "order": 7,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Build Docker container image",
                    "dependsOn": ["build", "post-security"]
                },
                {
                    "id": "generate-sbom",
                    "name": "Generate SBOM",
                    "order": 8,
                    "mandatory": False,
                    "enabled": True,
                    "category": "build",
                    "description": "Generate Software Bill of Materials",
                    "dependsOn": ["docker-build"]
                },
                {
                    "id": "image-security-check",
                    "name": "Image Security Check",
                    "order": 9,
                    "mandatory": False,
                    "enabled": True,
                    "category": "quality",
                    "description": "Scan container image for vulnerabilities",
                    "dependsOn": ["docker-build"]
                },
                {
                    "id": "slack-notification",
                    "name": "Send Slack Notification",
                    "order": 10,
                    "mandatory": False,
                    "enabled": True,
                    "category": "notification",
                    "description": "Send build and deployment status to Slack",
                    "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"]
                }
            ]
        }
    },
    {
        "code": "production-template",
        "name": "Production Workflow",
        "description": "Enterprise-grade production pipeline with maximum security and compliance. Includes all checks and validations for mission-critical deployments.",
        "config": {
            "steps": [
                {
                    "id": "code-checkout",
                    "name": "Code Checkout",
                    "order": 1,
                    "mandatory": True,
                    "enabled": True,
                    "category": "setup",
                    "description": "Checkout source code from repository"
                },
                {
                    "id": "pre-security-validation",
                    "name": "Pre-Security Validation",
                    "order": 2,
                    "mandatory": True,
                    "enabled": True,
                    "category": "quality",
                    "description": "Validate code and configuration before build",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "env-validation",
                    "name": "Environment Validation",
                    "order": 3,
                    "mandatory": True,
                    "enabled": True,
                    "category": "setup",
                    "description": "Validate environment configuration and variables",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "code-quality-check",
                    "name": "Code Quality Check",
                    "order": 4,
                    "mandatory": True,
                    "enabled": True,
                    "category": "quality",
                    "description": "Run linting, formatting, and static analysis",
                    "dependsOn": ["code-checkout"]
                },
                {
                    "id": "build",
                    "name": "Build Application",
                    "order": 5,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Compile/build the application binary",
                    "dependsOn": ["code-checkout", "pre-security-validation", "env-validation", "code-quality-check"]
                },
                {
                    "id": "post-security",
                    "name": "Post-Security Scan",
                    "order": 6,
                    "mandatory": True,
                    "enabled": True,
                    "category": "quality",
                    "description": "Run security scans after build",
                    "dependsOn": ["build"]
                },
                {
                    "id": "docker-build",
                    "name": "Docker Build",
                    "order": 7,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Build Docker container image",
                    "dependsOn": ["build", "post-security"]
                },
                {
                    "id": "generate-sbom",
                    "name": "Generate SBOM",
                    "order": 8,
                    "mandatory": True,
                    "enabled": True,
                    "category": "build",
                    "description": "Generate Software Bill of Materials",
                    "dependsOn": ["docker-build"]
                },
                {
                    "id": "image-security-check",
                    "name": "Image Security Check",
                    "order": 9,
                    "mandatory": True,
                    "enabled": True,
                    "category": "quality",
                    "description": "Scan container image for vulnerabilities",
                    "dependsOn": ["docker-build"]
                },
                {
                    "id": "slack-notification",
                    "name": "Send Slack Notification",
                    "order": 10,
                    "mandatory": False,
                    "enabled": True,
                    "category": "notification",
                    "description": "Send build and deployment status to Slack",
                    "dependsOn": ["code-checkout", "code-quality-check", "build", "docker-build"]
                }
            ]
        }
    }
]


async def seed_cicd_template_refs():
    """Seed the database with Quick, Full, and Production workflow templates"""
    async with AsyncSessionLocal() as session:
        # Get first tenant
        result = await session.execute(select(__import__('app.db.models.tenants_mst_model', fromlist=['TenantsMstModel']).TenantsMstModel).limit(1))
        tenant = result.scalar_one_or_none()

        if not tenant:
            print("❌ No tenant found. Please create a tenant first.")
            return

        print(f"📦 Using tenant: {tenant.code} ({tenant.name})")

        created_count = 0
        for template_def in TEMPLATE_DEFS:
            # Check if template already exists
            existing = await session.execute(
                select(CicdTemplateRefModel).where(
                    CicdTemplateRefModel.code == template_def["code"]
                )
            )
            existing_template = existing.scalar_one_or_none()

            if existing_template:
                print(f"⚠️  Template already exists: {template_def['code']}")
                continue

            # Create template
            template = CicdTemplateRefModel(
                code=template_def["code"],
                name=template_def["name"],
                description=template_def["description"],
                config=template_def["config"],
                tenant_mst_code=None,  # Global template for all tenants
                is_active=True,
                is_deleted=False
            )

            session.add(template)
            created_count += 1
            print(f"✅ Created template: {template_def['code']} - {template_def['name']}")

        await session.commit()

        print(f"\n🎉 Successfully created {created_count} CI/CD template references!")

        # Verification
        result = await session.execute(
            select(CicdTemplateRefModel).order_by(CicdTemplateRefModel.code)
        )
        templates = result.scalars().all()

        print(f"\n📋 All templates in database:")
        print("─" * 80)
        for tmpl in templates:
            enabled_steps = len([s for s in tmpl.config.get("steps", []) if s.get("enabled")])
            total_steps = len(tmpl.config.get("steps", []))
            print(f"  • {tmpl.name}")
            print(f"    Code: {tmpl.code}")
            print(f"    Steps: {enabled_steps}/{total_steps} enabled")
            print(f"    Description: {tmpl.description}")
            print()


if __name__ == "__main__":
    asyncio.run(seed_cicd_template_refs())
