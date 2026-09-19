from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from app.core.config import settings


# Build database URLs from settings
DATABASE_URL = f"postgresql://{settings.db_user}:{settings.db_password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
ASYNC_DATABASE_URL = f"postgresql+asyncpg://{settings.db_user}:{settings.db_password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"

# --- Sync engine (for Base.metadata.create_all) ---
engine = create_engine(DATABASE_URL)

# --- Async engine ---
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
    pool_size=10,  # Number of connections to keep in the pool
    max_overflow=20,  # Additional connections that can be created on demand
    pool_timeout=30,  # Timeout for getting a connection from the pool (seconds)
    pool_recycle=3600,  # Recycle connections after 1 hour (prevents stale connections)
    pool_pre_ping=True,  # Check if connection is alive before using it (critical!)
)

# --- Sync session factory ---
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# --- Async session factory ---
AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)


# --- Read-only async engine (MCP query_data tool) ---
# Separate credentials so a SELECT-only Postgres role, not application code,
# is what stops a generated query from reading anything outside mcp_ro.*.
# Small pool: a handful of ad-hoc questions, never request traffic.
READ_ONLY_CREDENTIALS_CONFIGURED = bool(settings.db_ro_user)
_ro_user = settings.db_ro_user or settings.db_user
_ro_password = settings.db_ro_password or settings.db_password
READ_ONLY_ASYNC_DATABASE_URL = (
    f"postgresql+asyncpg://{_ro_user}:{_ro_password}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)

read_only_async_engine = create_async_engine(
    READ_ONLY_ASYNC_DATABASE_URL,
    echo=False,
    pool_size=3,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=3600,
    pool_pre_ping=True,
)
