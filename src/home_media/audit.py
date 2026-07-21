"""Redacting audit trail for mutations and sensitive observations."""

from __future__ import annotations

import logging
import re
from typing import Any

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization=)(bearer\s+\S+)"),
    re.compile(r"(?i)(\bbearer\s+)([a-z0-9\._\-]+)"),
    re.compile(r"(?i)(pin|password|token|secret|credential|cookie)=([^\s&]+)"),
    re.compile(r"(?i)(access_token|refresh_token|api_key)=([^\s&]+)"),
    re.compile(r"(https?://[^\s]+[?&](?:sig|signature|token|key|auth)=)([^&\s]+)"),
]

REDACTED = "***REDACTED***"

logger = logging.getLogger("home_media.audit")


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", redacted)
    return redacted


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if any(
                part in key_l
                for part in ("pin", "password", "token", "secret", "credential", "cookie", "auth")
            ):
                out[key] = REDACTED
            else:
                out[key] = redact_obj(item)
        return out
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    return value


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("home_media")
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    # Never let libraries dump credentials to stdout accidentally via our logger chain.
    logging.getLogger("pyatv").setLevel(logging.WARNING)


def audit(
    event: str,
    *,
    room_key: str | None = None,
    intent: str | None = None,
    targets: list[str] | None = None,
    result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = redact_obj(
        {
            "event": event,
            "room_key": room_key,
            "intent": intent,
            "targets": targets or [],
            "result": result or {},
            **(extra or {}),
        }
    )
    logger.info("audit %s", payload)
