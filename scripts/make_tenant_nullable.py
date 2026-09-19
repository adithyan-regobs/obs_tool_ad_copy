"""
Make tenant_mst_code nullable in cicd_template_mst table
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.db.session import AsyncSessionLocal


async def make_tenant_nullable():
    """Make tenant_mst_code nullable and update workflow steps"""
    async with AsyncSessionLocal() as session:
        try:
            print("Step 1: Dropping foreign key constraint...")
            await session.execute(text(
                "ALTER TABLE cicd_template_mst DROP CONSTRAINT IF EXISTS fk_cicd_template_tenant"
            ))

            print("\nStep 2: Making tenant_mst_code nullable...")
            await session.execute(text(
                "ALTER TABLE cicd_template_mst ALTER COLUMN tenant_mst_code DROP NOT NULL"
            ))

            print("\nStep 3: Updating workflow steps to have null tenant...")
            result = await session.execute(text(
                "UPDATE cicd_template_mst SET tenant_mst_code = NULL WHERE code LIKE 'step-%'"
            ))
            print(f"   Updated {result.rowcount} rows")

            print("\nStep 4: Recreating foreign key constraint...")
            await session.execute(text("""
                ALTER TABLE cicd_template_mst
                ADD CONSTRAINT fk_cicd_template_tenant
                FOREIGN KEY (tenant_mst_code)
                REFERENCES tenants_mst(code)
                ON DELETE CASCADE
            """))

            await session.commit()
            print("\n✅ Successfully made tenant_mst_code nullable")

            # Verify
            result = await session.execute(text("""
                SELECT code, name, tenant_mst_code
                FROM cicd_template_mst
                WHERE code LIKE 'step-%'
                ORDER BY step_order
                LIMIT 5
            """))
            steps = result.fetchall()
            print("\n✅ Verification - First 5 workflow steps:")
            for step in steps:
                tenant = step[2] if step[2] else "NULL (global)"
                print(f"   - {step[0]}: {step[1]} (tenant: {tenant})")

        except Exception as e:
            print(f"\n❌ Error: {e}")
            await session.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(make_tenant_nullable())
