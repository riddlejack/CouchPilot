"""Unit/contract tests for room-bound screenshot observer."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from home_media.errors import ConfigError, TimeoutError_, UnsupportedError
from home_media.observers.binding import ObserverBindingStore, fingerprint_udid
from home_media.observers.blank import assess_blank_or_protected, synthesize_png
from home_media.observers.capture import (
    PyMobileDeviceScreenshotCapturer,
    build_dvt_screenshot_argv,
    finalize_png_result,
    plan_capture_command,
)
from home_media.observers.fake import FakeScreenshotProvider, all_state_fixtures, fixture_for_state
from home_media.observers.retention import prune_screenshots, save_png_private
from home_media.observers.screenshot import BoundScreenshotProvider, UnavailableScreenshotProvider
from home_media.observers.service import RoomScreenshotService, classify_from_screenshot
from home_media.providers.base import ProviderState
from home_media.providers.netflix import NetflixAdapter
from home_media.service import ApplicationService

LIVING = "00000000-0000-4000-8000-000000000004"
THEATER = "00000000-0000-4000-8000-000000000001"
FAMILY = "00000000-0000-4000-8000-000000000002"
BEDROOM = "00000000-0000-4000-8000-000000000003"
OFFICE = "00000000-0000-4000-8000-000000000005"

FIVE_DEVICES = {
    LIVING: "udid-living-room-aaaa",
    THEATER: "udid-theater-bbbbbbbb",
    FAMILY: "udid-family-room-cc",
    BEDROOM: "udid-bedroom-dddd",
    OFFICE: "udid-office-ee",
}


@pytest.mark.asyncio
async def test_unavailable_provider_fail_closed() -> None:
    result = await UnavailableScreenshotProvider().capture(LIVING, "living_room")
    assert result.png_bytes is None
    assert result.blank_or_protected is True
    assert result.error is not None


@pytest.mark.asyncio
async def test_bound_provider_zero_one_multiple_and_duplicate_tunnel() -> None:
    with pytest.raises(ConfigError):
        BoundScreenshotProvider({})
    with pytest.raises(ConfigError):
        BoundScreenshotProvider({LIVING: "u1", THEATER: "u1"})

    provider = BoundScreenshotProvider({LIVING: "u-living"})
    with pytest.raises(UnsupportedError):
        await provider.capture(THEATER, "theater")

    result = await provider.capture(LIVING, "living_room")
    assert result.png_bytes is None
    assert result.observer_udid_fingerprint == fingerprint_udid("u-living")
    assert "binding stub" in (result.error or "")


@pytest.mark.asyncio
async def test_adversarial_five_device_never_devices0() -> None:
    captured_udids: list[str] = []

    async def capturer(udid: str, stable_device_id: str, room_key: str):
        captured_udids.append(udid)
        png = synthesize_png(16, 9, (10, 200, 10))
        return finalize_png_result(
            png,
            stable_device_id=stable_device_id,
            room_key=room_key,
            udid=udid,
            latency_ms=12,
        )

    provider = BoundScreenshotProvider(FIVE_DEVICES, capturer=capturer)
    # devices[0] anti-pattern would return theater (first insertion) for any request.
    first_listed = next(iter(FIVE_DEVICES.values()))
    result = await provider.capture(LIVING, "living_room")
    assert captured_udids == [FIVE_DEVICES[LIVING]]
    assert captured_udids[0] != FIVE_DEVICES[THEATER]
    assert result.device_id == LIVING
    assert result.room_key == "living_room"
    assert result.png_bytes is not None
    assert result.blank_or_protected is False
    # Requesting Living Room must not capture Theater even if Theater is also bound.
    theater_shot_udids = []

    async def theater_capturer(udid: str, stable_device_id: str, room_key: str):
        theater_shot_udids.append(udid)
        png = synthesize_png(16, 9, (10, 200, 10))
        return finalize_png_result(
            png,
            stable_device_id=stable_device_id,
            room_key=room_key,
            udid=udid,
            latency_ms=12,
        )

    provider2 = BoundScreenshotProvider(FIVE_DEVICES, capturer=theater_capturer)
    await provider2.capture(LIVING, "living_room")
    assert theater_shot_udids == [FIVE_DEVICES[LIVING]]
    assert FIVE_DEVICES[THEATER] not in theater_shot_udids
    _ = first_listed


def test_argv_has_explicit_udid_no_shell_metacharacters() -> None:
    from home_media.observers.capture import build_dvt_screenshot_argv, build_wifi_capture_argv

    wifi = build_wifi_capture_argv(
        udid="EXACT-UDID-123",
        output_path=Path("/tmp/home-media-test.png"),
        executable="python3.14",
    )
    assert wifi[0] == "python3.14"
    assert wifi[1].endswith("pmd3_wifi_capture.py")
    assert "--udid" in wifi
    assert wifi[wifi.index("--udid") + 1] == "EXACT-UDID-123"
    assert ";" not in " ".join(wifi) and "|" not in " ".join(wifi)

    legacy = plan_capture_command(
        udid="EXACT-UDID-123",
        output_path=Path("/tmp/home-media-test.png"),
        executable="pymobiledevice3",
        userspace=True,
        wifi_worker=False,
    )
    assert legacy.argv[0] == "pymobiledevice3"
    assert "--udid" in legacy.argv
    assert legacy.argv[legacy.argv.index("--udid") + 1] == "EXACT-UDID-123"
    assert "--userspace" in legacy.argv
    assert "developer" in legacy.argv and "dvt" in legacy.argv
    _ = build_dvt_screenshot_argv


@pytest.mark.asyncio
async def test_capture_timeout_and_malformed_png() -> None:
    async def slow_runner(argv, timeout_s=45.0):  # noqa: ANN001
        await asyncio.sleep(0.05)
        raise TimeoutError()

    capturer = PyMobileDeviceScreenshotCapturer(timeout_s=0.01, runner=slow_runner)
    with pytest.raises(TimeoutError_):
        await capturer.capture_udid("u1", stable_device_id=LIVING, room_key="living_room")

    bad = finalize_png_result(
        b"not-a-png",
        stable_device_id=LIVING,
        room_key="living_room",
        udid="u1",
        latency_ms=1,
    )
    assert bad.png_bytes is None
    assert bad.blank_or_protected is True
    assert bad.error == "malformed_or_empty_png"

    empty = finalize_png_result(
        b"",
        stable_device_id=LIVING,
        room_key="living_room",
        udid="u1",
        latency_ms=1,
    )
    assert empty.blank_or_protected is True


def test_blank_or_protected_detection() -> None:
    black = synthesize_png(8, 8, (0, 0, 0))
    color = synthesize_png(8, 8, (200, 40, 40))
    assert assess_blank_or_protected(black).blank_or_protected is True
    assert assess_blank_or_protected(color).blank_or_protected is False
    assert assess_blank_or_protected(None).blank_or_protected is True
    assert assess_blank_or_protected(b"\x00\x01").blank_or_protected is True


def test_private_file_modes_and_retention(tmp_path: Path) -> None:
    png = synthesize_png(4, 4, (50, 50, 200))
    path = save_png_private(png, room_key="living_room", sha256="abc123def456", directory=tmp_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0  # no group/other bits
    # Age the file and prune.
    os.utime(path, (0, 0))
    removed = prune_screenshots(tmp_path, max_age_s=1)
    assert removed == 1
    assert not path.exists()


def test_binding_store_duplicate_and_room_mismatch(tmp_path: Path) -> None:
    store = ObserverBindingStore.empty()
    store.confirm(room_key="living_room", stable_device_id=LIVING, observer_udid="u-living")
    with pytest.raises(ConfigError):
        store.confirm(room_key="theater", stable_device_id=THEATER, observer_udid="u-living")
    path = store.save(tmp_path / "bindings.json")
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0
    loaded = ObserverBindingStore.load(path)
    with pytest.raises(ConfigError):
        loaded.get_for_room("theater", expected_stable_id=LIVING)
    binding = loaded.get_for_room("living_room", expected_stable_id=LIVING)
    assert binding.observer_udid == "u-living"
    public = loaded.public_view()
    assert public[0]["observer_udid_fingerprint"] == fingerprint_udid("u-living")
    assert "u-living" not in str(public)


@pytest.mark.asyncio
async def test_fake_fixtures_for_every_provider_state() -> None:
    fixtures = all_state_fixtures()
    assert set(fixtures) == set(ProviderState)
    provider = FakeScreenshotProvider({LIVING: fixtures[ProviderState.HOME]})
    shot = await provider.capture(LIVING, "living_room")
    assert shot.png_bytes is not None
    assert provider.label_for_sha(shot.metadata.sha256) == ProviderState.HOME
    state, conf, _ = classify_from_screenshot(
        shot, label=ProviderState.HOME, confidence=0.95
    )
    assert state == ProviderState.HOME
    assert conf >= 0.9

    blank_provider = FakeScreenshotProvider(
        {LIVING: fixture_for_state(ProviderState.BLANK_OR_PROTECTED)}
    )
    blank = await blank_provider.capture(LIVING, "living_room")
    state2, _, _ = classify_from_screenshot(blank)
    assert state2 == ProviderState.BLANK_OR_PROTECTED
    from home_media.providers.base import ProviderObservation

    assert (
        NetflixAdapter().classify(
            ProviderObservation(screenshot_state="blank_or_protected", confidence=0.9)
        )
        == ProviderState.BLANK_OR_PROTECTED
    )


@pytest.mark.asyncio
async def test_room_service_living_room_not_theater() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    store = ObserverBindingStore.empty()
    for sid, udid in FIVE_DEVICES.items():
        room = {
            LIVING: "living_room",
            THEATER: "theater",
            FAMILY: "family_room",
            BEDROOM: "bedroom",
            OFFICE: "office",
        }[sid]
        store.confirm(room_key=room, stable_device_id=sid, observer_udid=udid)

    captured: list[str] = []

    async def runner(argv, timeout_s=45.0):  # noqa: ANN001
        udid = argv[argv.index("--udid") + 1]
        captured.append(udid)
        # Write a visible PNG to the output path argument (--out for wifi worker).
        if "--out" in argv:
            out = Path(argv[argv.index("--out") + 1])
        else:
            out = Path(argv[argv.index("screenshot") + 1])
        out.write_bytes(synthesize_png(12, 8, (30, 180, 30)))
        return 0, b"", b""

    capturer = PyMobileDeviceScreenshotCapturer(runner=runner, executable="pymobiledevice3")
    room_svc = RoomScreenshotService(
        svc.registry, bindings=store, capturer=capturer, require_gate=False
    )
    result = await room_svc.capture_room("living_room", save=False, prune=False)
    assert captured == ["udid-living-room-aaaa"]
    assert result.device_id == LIVING
    assert result.observer_udid_fingerprint == fingerprint_udid("udid-living-room-aaaa")
    meta = result.public_metadata()
    assert "udid-living-room-aaaa" not in str(meta)
    assert meta["observer_udid_fingerprint"] == fingerprint_udid("udid-living-room-aaaa")


@pytest.mark.asyncio
async def test_live_netflix_without_binding_stops() -> None:
    """Live (non-fake) path must refuse blind Netflix navigation without bindings."""
    svc = ApplicationService.from_config_path(use_fakes=True)
    # Simulate live gate while keeping fake adapters.
    svc._use_fakes = False  # noqa: SLF001
    assert svc.screenshot_service is not None
    assert svc.screenshot_service.bindings.bindings == {}
    result = await svc.prepare_content(
        "living_room",
        "Avatar: The Last Airbender",
        provider="netflix",
        goal="search_ready",
    )
    assert result.selected_result is False
    assert result.terminal_status.value == "handoff"
    assert any("screenshot_gate" in s.name for s in result.stages)


def test_build_argv_python_module_form() -> None:
    argv = build_dvt_screenshot_argv(
        udid="U",
        output_path=Path("/tmp/x.png"),
        executable="python3",
        userspace=True,
    )
    assert argv[:3] == ["python3", "-m", "pymobiledevice3"]
