"""Exact-room client for the persistent macOS AirPlay frame store.

The AVFoundation daemon owns wireless capture sessions and atomically publishes
immutable frames. This client never enumerates devices or chooses a nearest
name: it verifies the exact private capture identity, stable Apple TV identity,
room, frame sequence, timestamp, and SHA before returning pixels.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from home_media.errors import ConfigError, SafetyBlockedError
from home_media.observers.binding import fingerprint_udid
from home_media.observers.capture import (
    PyMobileDeviceScreenshotCapturer,
    finalize_png_result,
)
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult

FRAME_STORE_PROTOCOL_VERSION = 1
DEFAULT_FRAME_MAX_AGE_S = 3.0
MAX_STATE_BYTES = 64 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
_ROOM_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FRAME_FILENAME = re.compile(r"^frame-([1-9][0-9]*)\.png$")
_SAFE_ERROR = re.compile(r"^[a-z0-9_]{1,64}$")


class _FrameStoreMiss(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class ScreenshotCapturer(Protocol):
    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ) -> ScreenshotResult: ...

    async def aclose(self) -> None: ...

    def health_snapshot(self) -> dict[str, object]: ...


def capture_identity_fingerprint(identity: str) -> str:
    """Match the daemon's SHA-256 first-eight-byte fingerprint."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _private_mode(path: Path, *, label: str, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigError(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError(f"{label} must not be a symlink")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode):
        raise ConfigError(f"{label} has the wrong file type")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError(f"{label} must not be group/world accessible")


class AirPlayFrameStoreCapturer:
    """Read fresh frames published by one exact-config AVFoundation daemon."""

    def __init__(
        self,
        *,
        config_path: Path,
        state_directory: Path,
        max_age_s: float = DEFAULT_FRAME_MAX_AGE_S,
    ) -> None:
        if max_age_s <= 0 or max_age_s > 60:
            raise ConfigError("AirPlay frame max age must be in (0, 60] seconds")
        # Keep the lexical path until lstat checks it; resolve() would silently
        # dereference a symlink before the private-store guard can reject it.
        self.config_path = config_path.expanduser().absolute()
        self.state_directory = state_directory.expanduser().absolute()
        self.max_age_s = max_age_s
        self._bindings = self._load_bindings(self.config_path)
        self._capture_count = 0
        self._miss_count = 0
        self._last_error_code: str | None = None

    @staticmethod
    def _load_bindings(path: Path) -> dict[str, dict[str, str]]:
        _private_mode(path, label="AirPlay capture config")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("AirPlay capture config is not valid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ConfigError("AirPlay capture config requires schema_version 1")
        rooms = raw.get("rooms")
        if not isinstance(rooms, list) or not rooms:
            raise ConfigError("AirPlay capture config requires exact room bindings")
        bindings: dict[str, dict[str, str]] = {}
        seen_rooms: set[str] = set()
        seen_captures: set[str] = set()
        for entry in rooms:
            if not isinstance(entry, dict):
                raise ConfigError("AirPlay capture room binding must be a mapping")
            room = entry.get("room_key")
            stable = entry.get("stable_device_id")
            capture = entry.get("capture_unique_id")
            if (
                not isinstance(room, str)
                or not _ROOM_KEY.fullmatch(room)
                or not isinstance(stable, str)
                or not stable.strip()
                or not isinstance(capture, str)
                or not capture.strip()
            ):
                raise ConfigError("AirPlay capture binding identities are invalid")
            if room in seen_rooms or stable in bindings or capture in seen_captures:
                raise ConfigError("AirPlay capture identities must each be unique")
            seen_rooms.add(room)
            seen_captures.add(capture)
            bindings[stable] = {
                "room_key": room,
                "capture_fingerprint": capture_identity_fingerprint(capture),
            }
        return bindings

    def _failure(
        self,
        code: str,
        *,
        udid: str,
        stable_device_id: str,
        room_key: str,
        latency_ms: int,
    ) -> ScreenshotResult:
        safe_code = code if _SAFE_ERROR.fullmatch(code) else "frame_stream_unavailable"
        self._miss_count += 1
        self._last_error_code = safe_code
        return ScreenshotResult(
            device_id=stable_device_id,
            room_key=room_key,
            blank_or_protected=True,
            error=safe_code,
            observer_udid_fingerprint=fingerprint_udid(udid),
            metadata=CaptureMetadata(
                latency_ms=latency_ms,
                capture_backend="airplay_avfoundation_stream",
                worker_reused=True,
                worker_startup_ms=0,
                argv_has_udid=False,
                argv_has_userspace=False,
            ),
        )

    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ) -> ScreenshotResult:
        started = time.perf_counter()
        return await asyncio.to_thread(
            self._capture_sync,
            udid,
            stable_device_id,
            room_key,
            started,
        )

    def _capture_sync(
        self,
        udid: str,
        stable_device_id: str,
        room_key: str,
        started: float,
    ) -> ScreenshotResult:
        if not udid.strip():
            raise ConfigError("AirPlay frame capture requires the confirmed DVT anchor")
        expected = self._bindings.get(stable_device_id)
        if expected is None or expected["room_key"] != room_key:
            raise SafetyBlockedError(
                "AirPlay frame request does not match an exact configured room/device",
                reason="airplay_frame_binding_mismatch",
            )

        def latency() -> int:
            return max(0, int((time.perf_counter() - started) * 1000))
        if not self.state_directory.exists():
            return self._failure(
                "frame_stream_not_running",
                udid=udid,
                stable_device_id=stable_device_id,
                room_key=room_key,
                latency_ms=latency(),
            )
        _private_mode(self.state_directory, label="AirPlay frame store", directory=True)
        room_directory = self.state_directory / room_key
        state_path = room_directory / "state.json"
        if not state_path.exists():
            return self._failure(
                "frame_stream_not_ready",
                udid=udid,
                stable_device_id=stable_device_id,
                room_key=room_key,
                latency_ms=latency(),
            )
        _private_mode(room_directory, label="AirPlay room frame store", directory=True)

        for _attempt in range(2):
            state = self._read_state(state_path)
            self._verify_identity(
                state,
                expected=expected,
                stable_device_id=stable_device_id,
                room_key=room_key,
            )
            if state.get("status") != "ready":
                daemon_error = state.get("error_code")
                if daemon_error == "capture_identity_mismatch":
                    raise SafetyBlockedError(
                        "AirPlay capture device name disagrees with its exact binding",
                        reason="airplay_frame_identity_mismatch",
                    )
                code = (
                    f"frame_stream_{daemon_error}"
                    if isinstance(daemon_error, str) and _SAFE_ERROR.fullmatch(daemon_error)
                    else "frame_stream_not_ready"
                )
                return self._failure(
                    code,
                    udid=udid,
                    stable_device_id=stable_device_id,
                    room_key=room_key,
                    latency_ms=latency(),
                )
            try:
                frame = self._read_verified_frame(
                    state,
                    state_path=state_path,
                    room_directory=room_directory,
                )
            except _FrameStoreMiss as exc:
                return self._failure(
                    exc.error_code,
                    udid=udid,
                    stable_device_id=stable_device_id,
                    room_key=room_key,
                    latency_ms=latency(),
                )
            if frame is None:
                continue
            png, captured_at = frame
            result = finalize_png_result(
                png,
                stable_device_id=stable_device_id,
                room_key=room_key,
                udid=udid,
                latency_ms=latency(),
                worker_reused=True,
                worker_startup_ms=0,
                capture_backend="airplay_avfoundation_stream",
                captured_at=captured_at,
            )
            if result.png_bytes is None:
                self._miss_count += 1
                self._last_error_code = result.error
                return result
            if result.metadata.width != state.get("width") or result.metadata.height != state.get(
                "height"
            ):
                return self._failure(
                    "frame_stream_dimension_mismatch",
                    udid=udid,
                    stable_device_id=stable_device_id,
                    room_key=room_key,
                    latency_ms=latency(),
                )
            self._capture_count += 1
            self._last_error_code = None
            return result
        return self._failure(
            "frame_stream_raced",
            udid=udid,
            stable_device_id=stable_device_id,
            room_key=room_key,
            latency_ms=latency(),
        )

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any]:
        _private_mode(path, label="AirPlay frame state")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ConfigError("AirPlay frame state cannot be read") from exc
        if len(raw) > MAX_STATE_BYTES:
            raise ConfigError("AirPlay frame state is oversized")
        try:
            state = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("AirPlay frame state is malformed") from exc
        if not isinstance(state, dict):
            raise ConfigError("AirPlay frame state must be a mapping")
        return state

    @staticmethod
    def _verify_identity(
        state: dict[str, Any],
        *,
        expected: dict[str, str],
        stable_device_id: str,
        room_key: str,
    ) -> None:
        if state.get("protocol_version") != FRAME_STORE_PROTOCOL_VERSION:
            raise ConfigError("AirPlay frame protocol version is unsupported")
        if (
            state.get("room_key") != room_key
            or state.get("stable_device_id") != stable_device_id
            or state.get("capture_unique_id_fingerprint")
            != expected["capture_fingerprint"]
        ):
            raise SafetyBlockedError(
                "AirPlay frame metadata identity does not match the requested Apple TV",
                reason="airplay_frame_identity_mismatch",
            )

    def _read_verified_frame(
        self,
        state: dict[str, Any],
        *,
        state_path: Path,
        room_directory: Path,
    ) -> tuple[bytes, str] | None:
        sequence = state.get("sequence")
        filename = state.get("frame_filename")
        captured_at = state.get("captured_at")
        digest = state.get("sha256")
        width = state.get("width")
        height = state.get("height")
        match = _FRAME_FILENAME.fullmatch(filename) if isinstance(filename, str) else None
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or match is None
            or int(match.group(1)) != sequence
            or not isinstance(captured_at, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise ConfigError("AirPlay ready-frame metadata is invalid")
        assert isinstance(filename, str)
        try:
            timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError("AirPlay frame timestamp is invalid") from exc
        if timestamp.tzinfo is None:
            raise ConfigError("AirPlay frame timestamp must include a timezone")
        age = (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds()
        if age > self.max_age_s or age < -5:
            raise _FrameStoreMiss("frame_stream_stale")
        frame_path = room_directory / filename
        _private_mode(frame_path, label="AirPlay frame")
        if frame_path.parent.resolve() != room_directory.resolve():
            raise ConfigError("AirPlay frame escaped its room store")
        if frame_path.stat().st_size > MAX_FRAME_BYTES:
            raise ConfigError("AirPlay frame is oversized")
        try:
            png = frame_path.read_bytes()
        except OSError as exc:
            raise ConfigError("AirPlay frame cannot be read") from exc
        if hashlib.sha256(png).hexdigest() != digest:
            raise _FrameStoreMiss("frame_stream_integrity_mismatch")
        after = self._read_state(state_path)
        if after.get("sequence") != sequence:
            return None
        return png, captured_at

    async def aclose(self) -> None:
        return None

    def health_snapshot(self) -> dict[str, object]:
        if self._last_error_code is not None:
            status = "degraded"
        elif self._capture_count:
            status = "ready"
        else:
            status = "configured"
        return {
            "status": status,
            "backend": "airplay_avfoundation_stream",
            "binding_count": len(self._bindings),
            "capture_count": self._capture_count,
            "miss_count": self._miss_count,
            "last_error_code": self._last_error_code,
        }


class HybridScreenshotCapturer:
    """Prefer persistent frames; fall back only to the same exact DVT binding."""

    def __init__(
        self,
        primary: AirPlayFrameStoreCapturer,
        fallback: ScreenshotCapturer,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self._fallback_count = 0

    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ) -> ScreenshotResult:
        # Identity/config exceptions intentionally propagate. Only a normal
        # no-frame result falls back, and the fallback receives the already
        # confirmed exact DVT UDID from BoundScreenshotProvider.
        result = await self.primary.capture_udid(
            udid,
            stable_device_id=stable_device_id,
            room_key=room_key,
        )
        if result.png_bytes is not None:
            return result
        self._fallback_count += 1
        return await self.fallback.capture_udid(
            udid,
            stable_device_id=stable_device_id,
            room_key=room_key,
        )

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()

    def health_snapshot(self) -> dict[str, object]:
        stream = self._safe_health_snapshot(self.primary)
        fallback = self._safe_health_snapshot(self.fallback)
        states = {str(stream.get("status")), str(fallback.get("status"))}
        if "ready" in states:
            status = "ready"
        elif states & {"degraded", "failed", "closed", "unavailable"}:
            status = "degraded"
        elif states & {"starting", "warming"}:
            status = "starting"
        else:
            status = "configured"
        return {
            "status": status,
            "persistent": True,
            "backend": "airplay_with_exact_dvt_fallback",
            "fallback_count": self._fallback_count,
            "stream": stream,
            "fallback": fallback,
        }

    @staticmethod
    def _safe_health_snapshot(capturer: ScreenshotCapturer) -> dict[str, object]:
        """Return redacted health even if an optional backend probe fails."""
        try:
            return dict(capturer.health_snapshot())
        except Exception:  # noqa: BLE001 -- health must stay observable and redacted
            return {
                "status": "degraded",
                "last_error_code": "health_snapshot_failed",
            }


def configured_screenshot_capturer() -> ScreenshotCapturer:
    """Build the environment-selected stream + warm DVT capture stack."""
    fallback = PyMobileDeviceScreenshotCapturer()
    config_value = os.environ.get("HOME_MEDIA_AIRPLAY_CAPTURE_CONFIG")
    state_value = os.environ.get("HOME_MEDIA_FRAME_STREAM_DIR")
    if not config_value and not state_value:
        return fallback
    if not config_value or not state_value:
        raise ConfigError(
            "HOME_MEDIA_AIRPLAY_CAPTURE_CONFIG and HOME_MEDIA_FRAME_STREAM_DIR "
            "must be configured together"
        )
    try:
        max_age = float(
            os.environ.get(
                "HOME_MEDIA_FRAME_STREAM_MAX_AGE_S",
                str(DEFAULT_FRAME_MAX_AGE_S),
            )
        )
    except ValueError as exc:
        raise ConfigError("HOME_MEDIA_FRAME_STREAM_MAX_AGE_S must be numeric") from exc
    primary = AirPlayFrameStoreCapturer(
        config_path=Path(config_value),
        state_directory=Path(state_value),
        max_age_s=max_age,
    )
    return HybridScreenshotCapturer(primary, fallback)
