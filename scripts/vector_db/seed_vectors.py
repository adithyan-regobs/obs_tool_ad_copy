"""
Unified Vector Database Seeding Script.

Seeds both service parameters and service configs to Qdrant.
Idempotent - only seeds collections that are empty.

Usage:
    # From the obs_tool directory:
    python -m scripts.vector_db.seed_vectors           # Seed if empty (idempotent)
    python -m scripts.vector_db.seed_vectors --recreate  # Force recreate all
    python -m scripts.vector_db.seed_vectors --dry-run   # Show what would be done

Environment Variables:
    SEED_VECTORS=true   -> Seed if empty (from entrypoint.sh)
    SEED_VECTORS=force  -> Force recreate (from entrypoint.sh)
"""
import asyncio
import argparse
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import settings
from app.integrations.qdrant_integration import QdrantIntegration

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_collection_vector_count(collection_name: str) -> int:
    """Get current vector count for a collection."""
    info = QdrantIntegration.get_collection_info(collection_name)
    if info:
        return info.get("vectors_count", 0)
    return 0


async def seed_parameters(recreate: bool = False, dry_run: bool = False) -> bool:
    """Seed service parameters collection."""
    from scripts.vector_db.seed_service_parameters import seed_vectors as seed_params
    return await seed_params(recreate=recreate, dry_run=dry_run)


async def seed_configs(recreate: bool = False, dry_run: bool = False) -> bool:
    """Seed service configs collection."""
    from scripts.vector_db.seed_service_configs import seed_vectors as seed_cfgs
    return await seed_cfgs(recreate=recreate, dry_run=dry_run)


async def main(recreate: bool = False, dry_run: bool = False) -> bool:
    """
    Main seeding function.

    Args:
        recreate: Force recreate collections even if they have data
        dry_run: Show what would be done without making changes

    Returns:
        True if all seeding succeeded
    """
    logger.info("=" * 60)
    logger.info("Unified Vector Database Seeding")
    logger.info("=" * 60)
    logger.info(f"Qdrant: {settings.qdrant_host}:{settings.qdrant_port}")

    if dry_run:
        logger.info("DRY RUN MODE")

    # Check Qdrant connectivity
    if not dry_run and not QdrantIntegration.health_check():
        logger.error("Failed to connect to Qdrant. Is it running?")
        return False

    results = []

    # ==== Service Parameters ====
    params_collection = settings.qdrant_collection_name
    params_count = get_collection_vector_count(params_collection) if not dry_run else 0

    logger.info("")
    logger.info(f"[1/2] Service Parameters ({params_collection})")
    logger.info(f"      Current vectors: {params_count}")

    if recreate or params_count == 0:
        if params_count > 0:
            logger.info("      -> RECREATING (forced)")
        else:
            logger.info("      -> SEEDING (collection empty)")
        results.append(await seed_parameters(recreate=recreate, dry_run=dry_run))
    else:
        logger.info("      -> SKIPPING (already has data)")
        results.append(True)

    # ==== Service Configs ====
    configs_collection = settings.qdrant_configs_collection
    configs_count = get_collection_vector_count(configs_collection) if not dry_run else 0

    logger.info("")
    logger.info(f"[2/2] Service Configs ({configs_collection})")
    logger.info(f"      Current vectors: {configs_count}")

    if recreate or configs_count == 0:
        if configs_count > 0:
            logger.info("      -> RECREATING (forced)")
        else:
            logger.info("      -> SEEDING (collection empty)")
        results.append(await seed_configs(recreate=recreate, dry_run=dry_run))
    else:
        logger.info("      -> SKIPPING (already has data)")
        results.append(True)

    # ==== Summary ====
    logger.info("")
    logger.info("=" * 60)
    if all(results):
        logger.info("All seeding operations completed successfully!")
    else:
        logger.warning("Some seeding operations failed")
    logger.info("=" * 60)

    return all(results)


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Seed Qdrant vector collections (idempotent)"
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Force recreate collections even if they have data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )

    args = parser.parse_args()

    success = asyncio.run(main(recreate=args.recreate, dry_run=args.dry_run))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    cli()
