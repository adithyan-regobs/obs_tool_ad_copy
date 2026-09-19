"""
Seed Qdrant with Service Parameter Vectors.

This script populates the Qdrant vector database with parameter embeddings
for the Service Sage semantic parameter matching feature.

Usage:
    # From the obs_tool directory:
    python -m scripts.vector_db.seed_service_parameters

    # With options:
    python -m scripts.vector_db.seed_service_parameters --recreate  # Delete and recreate collection
    python -m scripts.vector_db.seed_service_parameters --dry-run   # Show what would be done

Requirements:
    - Qdrant must be running (default: localhost:6333)
    - OpenAI API key must be configured in .env
    - Run from the obs_tool directory with PYTHONPATH set
"""
import asyncio
import argparse
import logging
import sys
from typing import List, Dict, Any

try:
    from qdrant_client.http import models as qdrant_models
except ImportError:
    qdrant_models = None

# Add parent directory to path for imports
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import settings
from app.integrations.qdrant_integration import QdrantIntegration
from app.integrations.openai_integration import OpenAIIntegration
from app.utils.service_config_chat.prompts.parameter_definitions import (
    PARAMETER_DEFINITIONS,
    PARAMETER_ALIASES,
    PARAMETER_CATEGORIES,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_category_for_param(param_name: str) -> str:
    """Get category for a parameter using existing PARAMETER_CATEGORIES."""
    for category, params in PARAMETER_CATEGORIES.items():
        if param_name in params:
            return category
    return "Other"


def get_aliases_for_param(canonical_name: str) -> List[str]:
    """
    Get all aliases that map to this canonical name.

    Iterates through the complete PARAMETER_ALIASES dict from parameter_definitions.py
    which contains all known aliases (100+).
    """
    return [
        alias for alias, target in PARAMETER_ALIASES.items()
        if target == canonical_name
    ]


def get_semantic_hints(param_name: str, category: str) -> str:
    """
    Get additional semantic hints based on parameter type.

    These hints help the embedding model understand related concepts
    that users might ask about.
    """
    hints = []

    # Category-based hints
    category_hints = {
        "Container": "compute resources, task definition, ECS container, Fargate",
        "Health & Routing": "load balancer, ALB, health probe, URL routing, path matching",
        "Scaling": "autoscaling, replicas, task count, elasticity, capacity, HPA",
        "HTTP Scaling": "request-based scaling, HTTP requests, API traffic, RPS",
        "ALB": "Application Load Balancer, routing rules, load balancing, listener",
        "Datadog Sidecar": "monitoring, APM, metrics, logging, observability, tracing",
        "OTel Sidecar": "OpenTelemetry, tracing, distributed tracing, observability, telemetry",
        "EBS Storage": "persistent storage, disk, volume, data persistence, block storage",
        "JVM": "Java Virtual Machine, heap memory, garbage collection, Java, heap size",
        "Build": "CI/CD, Docker, build process, compilation, Dockerfile",
        "Repository": "source code, GitHub, version control, Git, branches",
    }

    if category in category_hints:
        hints.append(category_hints[category])

    # Parameter-specific hints for key parameters
    param_hints = {
        "cpu": "vCPU, cores, processing power, compute, CPU units, millicores",
        "memory": "RAM, heap, buffer, allocation, megabytes, MB, gigabytes",
        "container_port": "application port, service port, listener port, expose port",
        "xmx": "JVM max heap, Java memory limit, heap size, OutOfMemory, maximum heap",
        "xms": "JVM initial heap, Java startup memory, minimum heap",
        "health_check_path": "liveness probe, readiness check, /health endpoint, health check URL",
        "service_path": "URL path, route path, API path, path pattern, wildcard route",
        "desired_count": "replicas, instances, task count, pods, number of tasks",
        "enable_autoscaling": "auto scale, elastic scaling, HPA, automatic scaling",
        "min_task_count": "minimum replicas, min instances, minimum capacity, scale down limit",
        "max_task_count": "maximum replicas, max instances, maximum capacity, scale up limit",
        "alb_selection": "load balancer choice, ALB mode, no_alb, create_new_alb, existing_alb",
        "listener_rule_priority": "rule order, priority number, routing order, ALB priority",
        "enable_datadog_sidecar": "Datadog agent, DD agent, monitoring sidecar",
        "enable_otel_sidecar": "OpenTelemetry collector, OTEL, tracing sidecar",
        "ebs_enabled": "persistent disk, EBS volume, block storage, data disk",
        "ebs_size": "disk size, volume size, storage size, gigabytes",
        "dockerfile_path": "Dockerfile location, docker file, container build file",
        "build_path": "build directory, target folder, dist folder, output path",
        "repository": "GitHub repo, source repository, code repository, Git URL",
        "branches": "Git branches, deployment branches, trigger branches",
    }

    if param_name in param_hints:
        hints.append(param_hints[param_name])

    return " | ".join(hints) if hints else ""


def build_embedding_text(param_name: str, definition: str) -> str:
    """
    Build rich text for embedding that captures semantic meaning.

    Combines:
    - Parameter name and category
    - Full description
    - All known aliases (from PARAMETER_ALIASES)
    - Semantic hints for related concepts
    """
    aliases = get_aliases_for_param(param_name)
    category = get_category_for_param(param_name)
    semantic_hints = get_semantic_hints(param_name, category)

    # Build comprehensive text for embedding
    parts = [
        f"Parameter: {param_name}",
        f"Category: {category}",
        f"Description: {definition}",
    ]

    if aliases:
        parts.append(f"Also known as: {', '.join(aliases)}")

    if semantic_hints:
        parts.append(f"Related concepts: {semantic_hints}")

    return " | ".join(parts)


def build_param_data() -> List[Dict[str, Any]]:
    """Build parameter data for embedding."""
    params_data = []

    for param_name, definition in PARAMETER_DEFINITIONS.items():
        category = get_category_for_param(param_name)
        aliases = get_aliases_for_param(param_name)
        embedding_text = build_embedding_text(param_name, definition)

        params_data.append({
            "canonical_name": param_name,
            "definition": definition,
            "category": category,
            "aliases": aliases,
            "embedding_text": embedding_text,
        })

    return params_data


async def seed_vectors(recreate: bool = False, dry_run: bool = False) -> bool:
    """
    Main seeding function.

    Args:
        recreate: If True, delete and recreate the collection
        dry_run: If True, only show what would be done

    Returns:
        True on success, False on failure
    """
    collection_name = settings.qdrant_collection_name
    vector_size = settings.embedding_dimensions

    logger.info("=" * 60)
    logger.info("Service Parameter Vector Seeding")
    logger.info("=" * 60)
    logger.info(f"Collection: {collection_name}")
    logger.info(f"Vector size: {vector_size}")
    logger.info(f"Qdrant: {settings.qdrant_host}:{settings.qdrant_port}")
    logger.info(f"Embedding model: {settings.embedding_model}")

    if dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    # Build parameter data first to validate
    params_data = build_param_data()
    logger.info(f"Parameters to embed: {len(params_data)}")
    logger.info(f"Total aliases in PARAMETER_ALIASES: {len(PARAMETER_ALIASES)}")

    # Show alias distribution
    total_aliases_mapped = sum(len(p["aliases"]) for p in params_data)
    logger.info(f"Total aliases mapped to parameters: {total_aliases_mapped}")

    if dry_run:
        logger.info("\nParameters that would be embedded:")
        for p in params_data:
            alias_count = len(p['aliases'])
            logger.info(f"  - {p['canonical_name']} ({p['category']}) - {alias_count} aliases")
            if p['aliases']:
                logger.info(f"    Aliases: {', '.join(p['aliases'][:5])}{'...' if len(p['aliases']) > 5 else ''}")
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

    # Generate embeddings
    logger.info("Generating embeddings via OpenAI...")
    embedding_texts = [p["embedding_text"] for p in params_data]
    embeddings = await OpenAIIntegration.embed_batch(embedding_texts)

    # Check for failures
    failed_count = sum(1 for e in embeddings if e is None)
    if failed_count > 0:
        logger.warning(f"Failed to generate {failed_count} embeddings")

    # Build Qdrant points
    points = []
    for idx, (param_data, embedding) in enumerate(zip(params_data, embeddings)):
        if embedding is None:
            logger.warning(f"Skipping {param_data['canonical_name']}: embedding failed")
            continue

        points.append(
            qdrant_models.PointStruct(
                id=idx,
                vector=embedding,
                payload={
                    "canonical_name": param_data["canonical_name"],
                    "definition": param_data["definition"],
                    "category": param_data["category"],
                    "aliases": param_data["aliases"],
                },
            )
        )

    logger.info(f"Upserting {len(points)} vectors to Qdrant...")

    # Upsert to Qdrant
    if not await QdrantIntegration.upsert_points(collection_name, points):
        logger.error("Failed to upsert vectors")
        return False

    # Verify
    info = await QdrantIntegration.get_collection_info(collection_name)
    if info:
        logger.info(f"Final vector count: {info.get('vectors_count', 0)}")

    logger.info("=" * 60)
    logger.info("Seeding complete!")
    logger.info("=" * 60)

    return True


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Seed Qdrant with Service Parameter vectors for semantic matching"
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

    args = parser.parse_args()

    success = asyncio.run(seed_vectors(recreate=args.recreate, dry_run=args.dry_run))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
