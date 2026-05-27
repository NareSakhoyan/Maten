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
    BLUE = "\033[34m"
    WHITE = "\033[37m"
    BOLD = "\033[1m"

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        original_name = record.name
        original_msg = record.msg

        record.levelname = self._colored_levelname(record.levelno, original_levelname)
        logger_color = self._logger_color(original_name)
        record.name = f"{logger_color}{original_name}{self.RESET}"
        if isinstance(record.msg, str):
            record.msg = f"{self._message_color(original_name, record.levelno)}{record.msg}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname
            record.name = original_name
            record.msg = original_msg

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

    def _logger_color(self, logger_name: str) -> str:
        if logger_name.startswith("app.performance.requests"):
            return f"{self.BOLD}{self.CYAN}"
        if logger_name.startswith("app.performance.sql"):
            return f"{self.BOLD}{self.YELLOW}"
        if logger_name.startswith("app.performance.celery"):
            return f"{self.BOLD}{self.BLUE}"
        if logger_name.startswith("app.performance.otel"):
            return self.CYAN
        if logger_name.startswith("app.api.errors"):
            return f"{self.BOLD}{self.RED}"
        if logger_name.startswith("app.workers"):
            return self.BLUE
        if logger_name.startswith("app."):
            return self.GREEN
        if logger_name.startswith("uvicorn"):
            return self.WHITE
        if logger_name.startswith("celery"):
            return self.BLUE
        return self.MAGENTA

    def _message_color(self, logger_name: str, levelno: int) -> str:
        if levelno >= logging.ERROR:
            return self.RED
        if levelno >= logging.WARNING:
            return self.YELLOW
        if logger_name.startswith("app.performance.requests"):
            return self.CYAN
        if logger_name.startswith("app.performance.sql"):
            return self.YELLOW
        if logger_name.startswith("app.performance.celery"):
            return self.BLUE
        return self.RESET


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
