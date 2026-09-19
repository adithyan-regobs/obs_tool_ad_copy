"""
Centralized logging configuration for ObsTool Backend.

This module provides a consistent logging setup across the application,
ensuring all logs are properly formatted and captured.
"""

import logging
import logging.config
import logging.handlers
from pathlib import Path


def setup_logging(log_level: str = "INFO", log_file: str = None, retention_days: int = 30) -> None:
    """
    Configure logging for the application with date-based rotation.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file. If None, only console logging is used.
        retention_days: Number of days to retain log files (default: 30)
    """
    # Create logs directory if it doesn't exist and log_file is specified
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers_config = {
        "console": {
            "class": "logging.StreamHandler",
            "level": log_level,
            "formatter": "standard",
            "stream": "ext://sys.stdout"
        }
    }

    # Add rotating file handler if log_file is specified
    if log_file:
        # Main application log with date-based rotation
        handlers_config["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "level": "DEBUG",
            "formatter": "detailed",
            "filename": log_file,
            "when": "midnight",
            "interval": 1,
            "backupCount": retention_days,
            "encoding": "utf-8"
        }

        # Separate error log for ERROR and above
        error_log_file = str(log_path.parent / f"error.log")
        handlers_config["error_file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "level": "ERROR",
            "formatter": "detailed",
            "filename": error_log_file,
            "when": "midnight",
            "interval": 1,
            "backupCount": retention_days,
            "encoding": "utf-8"
        }

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            },
            "detailed": {
                "format": "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            }
        },
        "handlers": handlers_config,
        "root": {
            "level": log_level,
            "handlers": list(handlers_config.keys())
        },
        "loggers": {
            "uvicorn": {
                "level": "INFO",
                "handlers": list(handlers_config.keys()),
                "propagate": False
            },
            "sqlalchemy": {
                "level": "WARNING",
                "handlers": list(handlers_config.keys()),
                "propagate": False
            }
        }
    }

    logging.config.dictConfig(config)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given name.

    Args:
        name: Name of the logger (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)
