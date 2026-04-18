from __future__ import annotations

import logging.config


def configure_logging(log_level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                }
            },
            "root": {
                "handlers": ["default"],
                "level": log_level.upper(),
            },
            "loggers": {
                "uvicorn": {"level": log_level.upper()},
                "uvicorn.access": {"level": log_level.upper()},
                "celery": {"level": log_level.upper()},
            },
        }
    )

