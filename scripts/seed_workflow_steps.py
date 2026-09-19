"""
Seed script to populate cicd_template_mst table with predefined workflow steps
Each step is stored as a separate row.
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models.cicd_template_mst_model import CicdTemplateMstModel
from app.db.models.tenants_mst_model import TenantsMstModel


# 10 workflow steps as separate rows
WORKFLOW_STEPS = [
    {
        "code": "step-code-checkout",
        "name": "Code Checkout",
        "description": "Checkout source code from repository",
        "step_order": 1,
        "step_category": "setup",
        "step_mandatory": True,
        "step_enabled": True,
        "step_dependencies": [],
        "language": "go",
    },
    {
        "code": "step-pre-security",
        "name": "Pre-Security Validation",
        "description": "Validate code and configuration before build",
        "step_order": 2,
        "step_category": "quality",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-code-checkout"],
        "language": "go",
    },
    {
        "code": "step-env-validation",
        "name": "Environment Validation",
        "description": "Validate environment configuration and variables",
        "step_order": 3,
        "step_category": "setup",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-code-checkout"],
        "language": "go",
    },
    {
        "code": "step-code-quality",
        "name": "Code Quality Check",
        "description": "Run linting, formatting, and static analysis",
        "step_order": 4,
        "step_category": "quality",
        "step_mandatory": False,
        "step_enabled": True,
        "step_dependencies": ["step-code-checkout"],
        "language": "go",
    },
    {
        "code": "step-build",
        "name": "Build Application",
        "description": "Compile/build the application binary",
        "step_order": 5,
        "step_category": "build",
        "step_mandatory": True,
        "step_enabled": True,
        "step_dependencies": ["step-code-checkout", "step-pre-security", "step-env-validation", "step-code-quality"],
        "language": "go",
    },
    {
        "code": "step-post-security",
        "name": "Post-Security Scan",
        "description": "Run security scans after build",
        "step_order": 6,
        "step_category": "quality",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-build"],
        "language": "go",
    },
    {
        "code": "step-docker-build",
        "name": "Docker Build",
        "description": "Build Docker container image",
        "step_order": 7,
        "step_category": "build",
        "step_mandatory": True,
        "step_enabled": True,
        "step_dependencies": ["step-build", "step-post-security"],
        "language": "go",
    },
    {
        "code": "step-generate-sbom",
        "name": "Generate SBOM",
        "description": "Generate Software Bill of Materials",
        "step_order": 8,
        "step_category": "build",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-docker-build"],
        "language": "go",
    },
    {
        "code": "step-image-security",
        "name": "Image Security Check",
        "description": "Scan container image for vulnerabilities",
        "step_order": 9,
        "step_category": "quality",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-docker-build"],
        "language": "go",
    },
    {
        "code": "step-slack-notification",
        "name": "Send Slack Notification",
        "description": "Send build and deployment status to Slack",
        "step_order": 10,
        "step_category": "notification",
        "step_mandatory": False,
        "step_enabled": False,
        "step_dependencies": ["step-code-checkout", "step-code-quality", "step-build", "step-docker-build"],
        "language": "go",
    },
]


async def seed_workflow_steps():
    """Seed the database with CI/CD workflow steps as separate rows"""
    async with AsyncSessionLocal() as session:
        # Get first tenant
        result = await session.execute(select(TenantsMstModel).limit(1))
        tenant = result.scalar_one_or_none()

        if not tenant:
            print("❌ No tenant found. Please create a tenant first.")
            return

        print(f"Using tenant: {tenant.code}")

        # Check if steps already exist
        existing_steps = await session.execute(
            select(CicdTemplateMstModel).where(
                CicdTemplateMstModel.tenant_mst_code == tenant.code,
                CicdTemplateMstModel.code.in_([step["code"] for step in WORKFLOW_STEPS])
            )
        )
        existing = existing_steps.scalars().all()

        if existing:
            print(f"⚠️  Found {len(existing)} existing workflow steps. Skipping seed.")
            for step in existing:
                print(f"   - {step.code}: {step.name}")
            return

        # Create the workflow steps
        created_steps = []
        for step_data in WORKFLOW_STEPS:
            step = CicdTemplateMstModel(
                code=step_data["code"],
                tenant_mst_code=tenant.code,
                applications_mst_code=None,  # Universal steps (not tied to a specific service)
                name=step_data["name"],
                description=step_data["description"],
                step_order=step_data["step_order"],
                step_category=step_data["step_category"],
                step_enabled=step_data["step_enabled"],
                step_mandatory=step_data["step_mandatory"],
                step_dependencies=step_data["step_dependencies"],
                language=step_data["language"],
                template_type="custom",
                is_active=True,
                is_system_template=True,  # These are system-defined steps
                config={"language": step_data["language"]}  # Minimal config for compatibility
            )
            session.add(step)
            created_steps.append(step)

        await session.commit()

        print(f"✅ Successfully created {len(created_steps)} CI/CD workflow steps:")
        for step in created_steps:
            enabled_status = "✓" if step.step_enabled else "✗"
            mandatory_status = "Required" if step.step_mandatory else "Optional"
            print(f"   {step.step_order}. [{enabled_status}] {step.name} ({mandatory_status}, {step.step_category})")


if __name__ == "__main__":
    asyncio.run(seed_workflow_steps())
