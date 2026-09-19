import time
from contextlib import contextmanager
from typing import Optional


@contextmanager
def log_timing(logger, label: str, context: Optional[str] = None, level: str = "info"):
    """
    Log elapsed time for a code block using the provided logger.

    Args:
        logger: Logger instance.
        label: Human-readable label for the timed block.
        context: Optional extra context to append to the log line.
        level: Logger method name (e.g., "info", "warning").
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        suffix = f" | {context}" if context else ""
        log_fn = getattr(logger, level, logger.info)
        log_fn(f"[TIMING] {label} took {elapsed_ms:.2f}ms{suffix}")
