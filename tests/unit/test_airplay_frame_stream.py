from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from home_media.errors import ConfigError, SafetyBlockedError
from home_media.observers.binding import fingerprint_udid
from home_media.observers.blank import synthesize_png
from home_media.observers.capture import finalize_png_result
from home_media.observers.stream import (
    AirPlayFrameStoreCapturer,
    HybridScreenshotCapturer,
    capture_identity_fingerprint,
    configured_screenshot_capturer,
)

STABLE_ID = "00000000-0000-4000-8000-000000000004"
UDID = "confirmed-dvt-living"
CAPTURE_ID = "exact-cmio-living"


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "airplay-capture.json"
    _private_write(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "minimum_frame_interval_ms": 250,
                "rooms": [
                    {
                        "room_key": "living_room",
                        "stable_device_id": STABLE_ID,
                        "capture_unique_id": CAPTURE_ID,
                        "expected_localized_name": "Living Room",
                    }
                ],
            }
        ).encode(),
    )
    return path


def _publish_fixture(
    state_root: Path,
    *,
    stable_id: str = STABLE_ID,
    room_key: str = "living_room",
    capture_fingerprint: str | None = None,
    captured_at: datetime | None = None,
    sequence: int = 7,
) -> bytes:
    png = synthesize_png(16, 9, (30, 110, 210))
    room = state_root / room_key
    room.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_root, 0o700)
    os.chmod(room, 0o700)
    frame_name = f"frame-{sequence}.png"
    _private_write(room / frame_name, png)
    state = {
        "protocol_version": 1,
        "room_key": room_key,
        "stable_device_id": stable_id,
        "capture_unique_id_fingerprint": capture_fingerprint
        or capture_identity_fingerprint(CAPTURE_ID),
        "sequence": sequence,
        "captured_at": (captured_at or datetime.now(UTC)).isoformat(),
        "frame_filename": frame_name,
        "sha256": hashlib.sha256(png).hexdigest(),
        "width": 16,
        "height": 9,
        "status": "ready",
        "error_code": None,
    }
    _private_write(room / "state.json", json.dumps(state).encode())
    return png


@pytest.mark.asyncio
async def test_frame_store_client_returns_fresh_exact_room_frame(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "frames"
    expected = _publish_fixture(state_root)
    capturer = AirPlayFrameStoreCapturer(
        config_path=config,
        state_directory=state_root,
    )

    result = await capturer.capture_udid(
        UDID,
        stable_device_id=STABLE_ID,
        room_key="living_room",
    )

    assert result.png_bytes == expected
    assert result.observer_udid_fingerprint == fingerprint_udid(UDID)
    assert result.metadata.capture_backend == "airplay_avfoundation_stream"
    assert result.metadata.worker_reused is True
    assert capturer.health_snapshot()["capture_count"] == 1


@pytest.mark.asyncio
async def test_frame_store_identity_mismatch_stops_without_guessing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "frames"
    _publish_fixture(state_root, capture_fingerprint="0" * 16)
    capturer = AirPlayFrameStoreCapturer(
        config_path=config,
        state_directory=state_root,
    )

    with pytest.raises(SafetyBlockedError, match="identity"):
        await capturer.capture_udid(
            UDID,
            stable_device_id=STABLE_ID,
            room_key="living_room",
        )

    with pytest.raises(SafetyBlockedError, match="exact configured"):
        await capturer.capture_udid(
            UDID,
            stable_device_id="some-other-tv",
            room_key="living_room",
        )


@pytest.mark.asyncio
async def test_frame_store_protocol_mismatch_is_a_hard_stop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "frames"
    _publish_fixture(state_root)
    state_path = state_root / "living_room" / "state.json"
    state = json.loads(state_path.read_text())
    state["protocol_version"] = 2
    _private_write(state_path, json.dumps(state).encode())
    primary = AirPlayFrameStoreCapturer(
        config_path=config,
        state_directory=state_root,
    )
    fallback = _FallbackCapturer()
    hybrid = HybridScreenshotCapturer(primary, fallback)

    with pytest.raises(ConfigError, match="protocol"):
        await hybrid.capture_udid(
            UDID,
            stable_device_id=STABLE_ID,
            room_key="living_room",
        )
    assert fallback.calls == []


class _FallbackCapturer:
    def __init__(
        self,
        *,
        health_status: str = "ready",
        health_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.health_status = health_status
        self.health_error = health_error

    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ):  # noqa: ANN201
        self.calls.append((udid, stable_device_id, room_key))
        return finalize_png_result(
            synthesize_png(8, 8, (200, 30, 30)),
            stable_device_id=stable_device_id,
            room_key=room_key,
            udid=udid,
            latency_ms=10,
        )

    async def aclose(self) -> None:
        return None

    def health_snapshot(self) -> dict[str, object]:
        if self.health_error is not None:
            raise self.health_error
        return {"status": self.health_status}


@pytest.mark.asyncio
async def test_stale_stream_falls_back_to_same_confirmed_dvt_udid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "frames"
    _publish_fixture(state_root, captured_at=datetime.now(UTC) - timedelta(minutes=1))
    primary = AirPlayFrameStoreCapturer(
        config_path=config,
        state_directory=state_root,
        max_age_s=1,
    )
    fallback = _FallbackCapturer()
    hybrid = HybridScreenshotCapturer(primary, fallback)

    result = await hybrid.capture_udid(
        UDID,
        stable_device_id=STABLE_ID,
        room_key="living_room",
    )

    assert fallback.calls == [(UDID, STABLE_ID, "living_room")]
    assert result.metadata.capture_backend == "pymobiledevice3_wifi_remote_pair_tcp"
    health = hybrid.health_snapshot()
    assert health["status"] == "ready"
    assert health["stream"]["status"] == "degraded"  # type: ignore[index]
    assert health["fallback_count"] == 1


def test_hybrid_health_reports_degraded_fallback_instead_of_unconditional_ready(
    tmp_path: Path,
) -> None:
    primary = AirPlayFrameStoreCapturer(
        config_path=_config(tmp_path),
        state_directory=tmp_path / "frames",
    )
    hybrid = HybridScreenshotCapturer(
        primary,
        _FallbackCapturer(health_status="degraded"),
    )

    health = hybrid.health_snapshot()

    assert health["status"] == "degraded"
    assert health["stream"]["status"] == "configured"  # type: ignore[index]
    assert health["fallback"]["status"] == "degraded"  # type: ignore[index]


def test_hybrid_health_probe_failure_is_redacted(tmp_path: Path) -> None:
    primary = AirPlayFrameStoreCapturer(
        config_path=_config(tmp_path),
        state_directory=tmp_path / "frames",
    )
    hybrid = HybridScreenshotCapturer(
        primary,
        _FallbackCapturer(health_error=RuntimeError(f"secret:{UDID}")),
    )

    health = hybrid.health_snapshot()

    assert health["status"] == "degraded"
    assert health["fallback"] == {
        "status": "degraded",
        "last_error_code": "health_snapshot_failed",
    }
    assert UDID not in json.dumps(health)


@pytest.mark.asyncio
async def test_hybrid_never_falls_back_across_identity_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "frames"
    _publish_fixture(state_root, stable_id="wrong-tv")
    primary = AirPlayFrameStoreCapturer(
        config_path=config,
        state_directory=state_root,
    )
    fallback = _FallbackCapturer()
    hybrid = HybridScreenshotCapturer(primary, fallback)

    with pytest.raises(SafetyBlockedError):
        await hybrid.capture_udid(
            UDID,
            stable_device_id=STABLE_ID,
            room_key="living_room",
        )
    assert fallback.calls == []


def test_stream_env_requires_config_and_state_as_a_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_MEDIA_AIRPLAY_CAPTURE_CONFIG", "/private/missing.json")
    monkeypatch.delenv("HOME_MEDIA_FRAME_STREAM_DIR", raising=False)
    with pytest.raises(ConfigError, match="configured together"):
        configured_screenshot_capturer()


def test_world_readable_capture_config_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    os.chmod(config, 0o644)
    with pytest.raises(ConfigError, match="group/world"):
        AirPlayFrameStoreCapturer(config_path=config, state_directory=tmp_path / "frames")


def test_symlinked_capture_config_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    link = tmp_path / "linked-config.json"
    link.symlink_to(config)
    with pytest.raises(ConfigError, match="symlink"):
        AirPlayFrameStoreCapturer(config_path=link, state_directory=tmp_path / "frames")
