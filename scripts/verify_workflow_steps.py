"""
Verify workflow steps in database
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, text
from app.db.session import AsyncSessionLocal
from app.db.models.cicd_template_mst_model import CicdTemplateMstModel


async def verify_workflow_steps():
    """Verify the workflow steps in the database"""
    async with AsyncSessionLocal() as session:
        # Query all workflow steps
        result = await session.execute(
            select(CicdTemplateMstModel)
            .where(CicdTemplateMstModel.code.like("step-%"))
            .order_by(CicdTemplateMstModel.step_order)
        )
        steps = result.scalars().all()

        print(f"\n✅ Found {len(steps)} workflow steps in cicd_template_mst table:\n")
        print("─" * 100)
        print(f"{'ID':<5} {'Code':<30} {'Name':<25} {'Order':<7} {'Category':<12} {'Enabled':<8} {'Mandatory':<10}")
        print("─" * 100)

        for step in steps:
            enabled = "✓" if step.step_enabled else "✗"
            mandatory = "Yes" if step.step_mandatory else "No"
            print(f"{step.id:<5} {step.code:<30} {step.name:<25} {step.step_order:<7} {step.step_category:<12} {enabled:<8} {mandatory:<10}")

        print("─" * 100)

        # Show the SQL output format that the user wanted
        print("\nSQL Query Result Format:")
        print("─" * 100)
        for step in steps:
            print(f"{step.step_order}: {step.name}")


if __name__ == "__main__":
    asyncio.run(verify_workflow_steps())
