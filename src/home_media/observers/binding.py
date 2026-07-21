"""Private room → Apple TV stable ID → observer UDID bindings.

Bindings are confirmed once by the user after a successful capture and live
outside the git tree. Full UDIDs never appear in logs or MCP text metadata —
only short fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from home_media.config import DEFAULT_CONFIG_DIR, ensure_private_dir, ensure_private_file
from home_media.errors import ConfigError, UnsupportedError

DEFAULT_BINDING_PATH = DEFAULT_CONFIG_DIR / "observer_bindings.json"


def fingerprint_udid(udid: str) -> str:
    """Stable short fingerprint for logs/MCP (never the raw UDID)."""
    digest = hashlib.sha256(udid.encode("utf-8")).hexdigest()
    return digest[:12]


def redact_udid(udid: str | None) -> str | None:
    if not udid:
        return None
    return f"udid_fp:{fingerprint_udid(udid)}"


class ObserverBinding(BaseModel):
    room_key: str
    stable_device_id: str
    observer_udid: str
    confirmed_at: str
    confirmed_by: str = "user"
    notes: str | None = None


class ObserverBindingStore(BaseModel):
    """In-memory + on-disk store. Keys are Apple TV stable device ids."""

    schema_version: int = 1
    bindings: dict[str, ObserverBinding] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> ObserverBindingStore:
        return cls()

    @classmethod
    def load(cls, path: Path | None = None) -> ObserverBindingStore:
        store_path = path or _path_from_env()
        if not store_path.exists():
            return cls.empty()
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError("observer_bindings.json root must be a mapping")
        return cls.model_validate(raw)

    def save(self, path: Path | None = None) -> Path:
        store_path = path or _path_from_env()
        ensure_private_dir(store_path.parent)
        payload = self.model_dump(mode="json")
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        store_path.write_text(text, encoding="utf-8")
        ensure_private_file(store_path)
        return store_path

    def validate_unique_udids(self) -> None:
        seen: dict[str, str] = {}
        for stable_id, binding in self.bindings.items():
            udid = binding.observer_udid
            if udid in seen and seen[udid] != stable_id:
                raise ConfigError(
                    "Duplicate observer UDID bound to multiple Apple TV stable ids "
                    f"({redact_udid(udid)})"
                )
            seen[udid] = stable_id

    def get_for_stable_id(self, stable_device_id: str) -> ObserverBinding:
        if not stable_device_id:
            raise UnsupportedError(
                "screenshot.binding",
                reason="stable_device_id is required; refusing unbound capture",
            )
        matches = [b for sid, b in self.bindings.items() if sid == stable_device_id]
        if len(matches) == 0:
            raise UnsupportedError(
                "screenshot.binding",
                reason=(
                    f"No observer binding for stable_device_id={stable_device_id!r}; "
                    "never falling back to devices[0]"
                ),
            )
        if len(matches) > 1:
            raise ConfigError(f"Multiple bindings for stable_device_id={stable_device_id!r}")
        binding = matches[0]
        if binding.stable_device_id != stable_device_id:
            raise ConfigError("Binding stable_device_id mismatch")
        return binding

    def get_for_room(self, room_key: str, *, expected_stable_id: str) -> ObserverBinding:
        binding = self.get_for_stable_id(expected_stable_id)
        if binding.room_key != room_key:
            raise ConfigError(
                f"Observer binding room mismatch: bound={binding.room_key!r} requested={room_key!r}"
            )
        return binding

    def confirm(
        self,
        *,
        room_key: str,
        stable_device_id: str,
        observer_udid: str,
        notes: str | None = None,
    ) -> ObserverBinding:
        udid = observer_udid.strip()
        if not udid:
            raise ConfigError("observer_udid must be non-empty")
        if not stable_device_id.strip():
            raise ConfigError("stable_device_id must be non-empty")
        # Reject duplicate UDID on a different stable id.
        for sid, existing in self.bindings.items():
            if existing.observer_udid == udid and sid != stable_device_id:
                raise ConfigError(
                    f"Observer UDID already bound to another device ({redact_udid(udid)})"
                )
        binding = ObserverBinding(
            room_key=room_key,
            stable_device_id=stable_device_id,
            observer_udid=udid,
            confirmed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            notes=notes,
        )
        self.bindings[stable_device_id] = binding
        self.validate_unique_udids()
        return binding

    def public_view(self) -> list[dict[str, Any]]:
        """Redacted listing safe for CLI/MCP text."""
        rows: list[dict[str, Any]] = []
        for binding in self.bindings.values():
            rows.append(
                {
                    "room_key": binding.room_key,
                    "stable_device_id": binding.stable_device_id,
                    "observer_udid_fingerprint": fingerprint_udid(binding.observer_udid),
                    "confirmed_at": binding.confirmed_at,
                    "confirmed_by": binding.confirmed_by,
                }
            )
        return rows


def _path_from_env() -> Path:
    override = os.environ.get("HOME_MEDIA_OBSERVER_BINDINGS")
    if override:
        return Path(override).expanduser()
    return DEFAULT_BINDING_PATH
