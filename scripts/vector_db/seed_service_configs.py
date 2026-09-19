"""
Seed Qdrant with Service Configuration Vectors.

This script populates the Qdrant vector database with service config embeddings
for the Service Sage semantic config search feature.

Usage:
    # From the obs_tool directory:
    python -m scripts.vector_db.seed_service_configs

    # With options:
    python -m scripts.vector_db.seed_service_configs --recreate  # Delete and recreate collection
    python -m scripts.vector_db.seed_service_configs --dry-run   # Show what would be done
    python -m scripts.vector_db.seed_service_configs --tenant <code>  # Seed specific tenant only

Requirements:
    - Qdrant must be running (default: localhost:6333)
    - OpenAI API key must be configured in .env
    - PostgreSQL must be running with service_configs data
    - Run from the obs_tool directory with PYTHONPATH set
"""
import asyncio
import argparse
import logging
import sys
from typing import List, Dict, Any, Optional

# Add parent directory to path for imports
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations.qdrant_integration import QdrantIntegration
from app.db.session import AsyncSessionLocal
# Import all models to resolve relationships
from app.db.models import *  # noqa: F401, F403
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.services_mst_model import ServicesMstModel
from app.utils.service_config_chat.config_vectorizer import ConfigVectorizer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def get_db_session() -> AsyncSession:
    """Create async database session."""
    return AsyncSessionLocal()


async def fetch_configs(
    session: AsyncSession,
    tenant_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch all service configs from database.

    Args:
        session: Database session
        tenant_code: Optional filter by tenant

    Returns:
        List of config dicts ready for vectorization
    """
    # Build query
    query = (
        select(ServiceConfigModel, ServicesMstModel.name)
        .join(
            ServicesMstModel,
            ServiceConfigModel.services_mst_code == ServicesMstModel.code,
        )
        .where(ServiceConfigModel.is_deleted == False)
    )

    if tenant_code:
        query = query.where(ServiceConfigModel.tenant_mst_code == tenant_code)

    result = await session.execute(query)
    rows = result.all()

    configs = []
    for config, service_name in rows:
        env_value = config.environment.value if hasattr(config.environment, 'value') else str(config.environment)

        configs.append({
            "tenant_code": config.tenant_mst_code,
            "service_code": config.services_mst_code,
            "service_name": service_name or config.services_mst_code,
            "environment": env_value,
            "geo_loc": config.geo_loc_mst_code,
            "config": config.config or {},
            "sidecar_config": config.sidecar_config,
        })

    return configs


async def seed_vectors(
    recreate: bool = False,
    dry_run: bool = False,
    tenant_code: Optional[str] = None,
) -> bool:
    """
    Main seeding function.

    Args:
        recreate: If True, delete and recreate the collection
        dry_run: If True, only show what would be done
        tenant_code: If provided, only seed configs for this tenant

    Returns:
        True on success, False on failure
    """
    vectorizer = ConfigVectorizer()
    collection_name = settings.qdrant_configs_collection
    vector_size = settings.embedding_dimensions

    logger.info("=" * 60)
    logger.info("Service Config Vector Seeding")
    logger.info("=" * 60)
    logger.info(f"Collection: {collection_name}")
    logger.info(f"Vector size: {vector_size}")
    logger.info(f"Qdrant: {settings.qdrant_host}:{settings.qdrant_port}")
    logger.info(f"Embedding model: {settings.embedding_model}")
    if tenant_code:
        logger.info(f"Tenant filter: {tenant_code}")

    if dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    # Connect to database and fetch configs
    logger.info("Connecting to database...")
    try:
        session = await get_db_session()
        configs = await fetch_configs(session, tenant_code)
        await session.close()
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return False

    logger.info(f"Configs to embed: {len(configs)}")

    if not configs:
        logger.warning("No configs found to seed")
        return True

    # Group by tenant for summary
    tenants = {}
    for c in configs:
        t = c["tenant_code"]
        tenants[t] = tenants.get(t, 0) + 1

    logger.info("Configs by tenant:")
    for t, count in sorted(tenants.items()):
        logger.info(f"  - {t}: {count} configs")

    if dry_run:
        logger.info("\nConfigs that would be embedded:")
        for c in configs[:10]:  # Show first 10
            logger.info(f"  - {c['service_name']} ({c['environment']}/{c['geo_loc']})")
        if len(configs) > 10:
            logger.info(f"  ... and {len(configs) - 10} more")
        logger.info("\nDRY RUN complete. No changes made.")
        return True

    # Check Qdrant connectivity
    if not await QdrantIntegration.health_check():
        logger.error("Failed to connect to Qdrant. Is it running?")
        return False

    logger.info("Qdrant connection: OK")

    # Handle collection
    collection_exists = await QdrantIntegration.collection_exists(collection_name)

    if collection_exists and recreate:
        logger.info(f"Deleting existing collection: {collection_name}")
        await QdrantIntegration.delete_collection(collection_name)
        collection_exists = False

    if not collection_exists:
        logger.info(f"Creating collection: {collection_name}")
        if not await QdrantIntegration.create_collection(collection_name, vector_size):
            logger.error("Failed to create collection")
            return False
    else:
        logger.info(f"Collection exists: {collection_name}")
        info = await QdrantIntegration.get_collection_info(collection_name)
        if info:
            logger.info(f"Current vector count: {info.get('vectors_count', 0)}")

    # Use ConfigVectorizer for batch indexing
    logger.info("Generating embeddings and indexing to Qdrant...")
    indexed_count = await vectorizer.index_configs_batch(configs)

    logger.info(f"Indexed {indexed_count} of {len(configs)} configs")

    # Verify
    info = await vectorizer.get_collection_info()
    if info:
        logger.info(f"Final vector count: {info.get('vectors_count', 0)}")

    logger.info("=" * 60)
    logger.info("Seeding complete!")
    logger.info("=" * 60)

    return indexed_count == len(configs)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Seed Qdrant with Service Config vectors for semantic search"
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate the collection before seeding",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--tenant",
        type=str,
        default=None,
        help="Only seed configs for this tenant code",
    )

    args = parser.parse_args()

    success = asyncio.run(
        seed_vectors(
            recreate=args.recreate,
            dry_run=args.dry_run,
            tenant_code=args.tenant,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
