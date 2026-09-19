"""
LangGraph PostgreSQL checkpointer lifecycle management.

The checkpointer is initialized once per process at app startup and reused
across all graph invocations. This ensures proper connection pool management
and supports multi-worker deployments.
"""
import logging
from typing import Optional
from psycopg_pool import AsyncConnectionPool
from psycopg.errors import UniqueViolation
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.core.config import settings

logger = logging.getLogger(__name__)

# Global checkpointer instance (process-level singleton)
checkpointer: Optional[AsyncPostgresSaver] = None
_pool: Optional[AsyncConnectionPool] = None


async def init_checkpointer() -> None:
    """
    Initialize the PostgreSQL checkpointer for LangGraph.

    This should be called once at app startup. The checkpointer is kept alive
    for the lifetime of the process and reused across all graph invocations.

    Uses a connection pool to handle remote database timeouts and provide
    better concurrency support.
    """
    global checkpointer, _pool

    if checkpointer is not None:
        logger.info("Checkpointer already initialized")
        return

    logger.info("Initializing LangGraph PostgreSQL checkpointer...")

    # Create connection pool with proper settings for remote database
    _pool = AsyncConnectionPool(
        conninfo=settings.async_database_url,
        min_size=2,  # Keep minimum connections alive
        max_size=10,  # Allow more connections under load
        timeout=30,  # Connection acquisition timeout (seconds)
        max_idle=300,  # Close idle connections after 5 minutes
        max_lifetime=3600,  # Recycle connections after 1 hour
        kwargs={
            "autocommit": True,  # Required for CREATE INDEX CONCURRENTLY
            "prepare_threshold": 0,
            "keepalives": 1,  # Enable TCP keepalives
            "keepalives_idle": 30,  # Send keepalive after 30s idle
            "keepalives_interval": 10,  # Retry keepalive every 10s
            "keepalives_count": 3,  # Drop connection after 3 failed keepalives
            "connect_timeout": 10,  # Timeout for new connections (seconds)
        },
        reconnect_timeout=30,  # Allow pool to reconnect broken connections
        open=False,  # Don't open in constructor (deprecated warning)
    )

    # Open pool explicitly
    await _pool.open()
    logger.info("Connection pool opened successfully")

    # Create saver with the pool
    saver = AsyncPostgresSaver(_pool)

    # Setup tables (idempotent, but may race in multi-worker startup)
    logger.info("Setting up checkpoint tables...")
    try:
        await saver.setup()
        logger.info("Checkpoint tables setup completed")
    except UniqueViolation:
        logger.info("Checkpoint tables already exist - skipping setup")
    except Exception as e:
        error_msg = str(e)
        if "already exists" in error_msg or "duplicate key" in error_msg:
            logger.info(f"Checkpoint tables already exist: {error_msg}")
        else:
            logger.error(f"Failed to setup checkpoint tables: {error_msg}")
            # Don't raise - tables might already exist from another worker

    checkpointer = saver
    logger.info("LangGraph checkpointer initialized successfully")


async def close_checkpointer() -> None:
    """
    Close the PostgreSQL checkpointer and release connection pool.

    This should be called once at app shutdown to ensure graceful cleanup.
    """
    global checkpointer, _pool

    if not checkpointer:
        return

    logger.info("Closing LangGraph checkpointer...")
    try:
        await checkpointer.aclose()
        logger.info("Checkpointer closed")

        if _pool:
            await _pool.close()
            logger.info("Connection pool closed")
    except Exception as e:
        logger.error(f"Error closing checkpointer: {e}")
    finally:
        checkpointer = None
        _pool = None
