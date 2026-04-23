from __future__ import annotations

import logging
import logging.config


class ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    RED = "\033[31m"

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"{self.RED}{formatted}{self.RESET}"
        return formatted


def configure_logging(log_level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "()": "app.core.logging.ColorFormatter",
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
