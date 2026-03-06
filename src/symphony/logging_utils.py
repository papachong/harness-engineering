from __future__ import annotations

import json
import logging
from typing import Any


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("symphony")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\n", " ").strip()
    if not text:
        return '""'
    if any(ch.isspace() for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def log_kv(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    message = " ".join([f"event={event}"] + [f"{key}={_format_value(value)}" for key, value in fields.items()])
    logger.log(level, message)
