from __future__ import annotations

import logging
import logging.config


class ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BOLD = "\033[1m"

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        original_name = record.name

        record.levelname = self._colored_levelname(record.levelno, original_levelname)
        record.name = f"{self.MAGENTA}{original_name}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname
            record.name = original_name

    def _colored_levelname(self, levelno: int, levelname: str) -> str:
        if levelno >= logging.CRITICAL:
            color = f"{self.BOLD}{self.RED}"
        elif levelno >= logging.ERROR:
            color = self.RED
        elif levelno >= logging.WARNING:
            color = self.YELLOW
        elif levelno >= logging.INFO:
            color = self.GREEN
        else:
            color = self.CYAN
        return f"{color}{levelname}{self.RESET}"


def configure_logging(log_level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "()": "app.core.logging.ColorFormatter",
                    "format": f"{ColorFormatter.DIM}%(asctime)s{ColorFormatter.RESET} %(levelname)s [%(name)s] %(message)s",
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
