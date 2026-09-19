"""
Quick script to add missing is_deleted column
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.db.session import AsyncSessionLocal


async def add_is_deleted_column():
    """Add is_deleted column if it doesn't exist"""
    async with AsyncSessionLocal() as session:
        try:
            # Check if column exists
            result = await session.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'cicd_template_mst'
                AND column_name = 'is_deleted'
            """))
            exists = result.scalar_one_or_none()

            if not exists:
                print("Adding is_deleted column...")
                await session.execute(text("""
                    ALTER TABLE cicd_template_mst
                    ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE NOT NULL
                """))
                await session.commit()
                print("✅ Added is_deleted column")
            else:
                print("ℹ️  is_deleted column already exists")

        except Exception as e:
            print(f"❌ Error: {e}")
            await session.rollback()


if __name__ == "__main__":
    asyncio.run(add_is_deleted_column())
