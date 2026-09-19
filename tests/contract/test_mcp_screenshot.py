"""MCP screenshot debug tool returns a real Image content block."""

from __future__ import annotations

import importlib
import json

import pytest
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ImageContent

from home_media.hub import HomeMediaHub
from home_media.observers.binding import ObserverBindingStore, fingerprint_udid
from home_media.observers.blank import synthesize_png
from home_media.observers.capture import PyMobileDeviceScreenshotCapturer
from home_media.observers.fake import FakeScreenshotProvider, fixture_for_state
from home_media.observers.service import RoomScreenshotService
from home_media.providers.base import ProviderState
from home_media.service import ApplicationService

LIVING = "00000000-0000-4000-8000-000000000004"


def test_image_helper_produces_image_content_block() -> None:
    png = synthesize_png(8, 8, (40, 120, 200))
    image = Image(data=png, format="png")
    content = image.to_image_content()
    assert isinstance(content, ImageContent)
    assert content.type == "image"
    assert content.mimeType == "image/png"
    assert content.data  # base64 payload present


@pytest.mark.asyncio
async def test_debug_tool_response_contains_image_not_raw_udid(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG", "1")
    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    monkeypatch.setenv("HOME_MEDIA_OBSERVER_BINDINGS", str(tmp_path / "bindings.json"))

    import home_media.mcp_server as mcp_server

    mcp_server = importlib.reload(mcp_server)
    assert "capture_room_screenshot" in {t.name for t in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001

    svc = ApplicationService.from_config_path(use_fakes=True)
    store = ObserverBindingStore.empty()
    store.confirm(
        room_key="living_room", stable_device_id=LIVING, observer_udid="SECRET-UDID-LIVING"
    )
    fake = FakeScreenshotProvider({LIVING: fixture_for_state(ProviderState.HOME)})
    fake_capture = fake.capture

    async def bound_fake_capture(stable_device_id: str, room_key: str):
        result = await fake_capture(stable_device_id, room_key)
        result.observer_udid_fingerprint = fingerprint_udid("SECRET-UDID-LIVING")
        return result

    fake.capture = bound_fake_capture  # type: ignore[method-assign]
    svc.screenshot_service = RoomScreenshotService(
        svc.registry, bindings=store, provider=fake, require_gate=False
    )
    hub = HomeMediaHub(lambda: svc)
    await hub.start()

    # Call tool function directly with a minimal context stand-in.
    class _Ctx:
        class request_context:
            class lifespan_context:
                pass

    _Ctx.request_context.lifespan_context.hub = hub

    try:
        result = await mcp_server.capture_room_screenshot("living_room", ctx=_Ctx())  # type: ignore[arg-type]
    finally:
        await hub.aclose()
    assert isinstance(result, list)
    assert any(isinstance(item, Image) for item in result)
    image_item = next(item for item in result if isinstance(item, Image))
    content = image_item.to_image_content()
    assert content.type == "image"
    meta_item = next(item for item in result if isinstance(item, dict))
    blob = json.dumps(meta_item, default=str)
    assert "SECRET-UDID-LIVING" not in blob
    assert "192.168." not in blob
    assert meta_item["data"]["observer_udid_fingerprint"] == fingerprint_udid(
        "SECRET-UDID-LIVING"
    )
    assert meta_item["data"]["room_key"] == "living_room"
    assert meta_item["data"]["device_id"] == LIVING

    # Reset module flag for other tests by reloading without debug.
    monkeypatch.delenv("HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG", raising=False)
    importlib.reload(mcp_server)


def test_default_mcp_surface_hides_screenshot_tool() -> None:
    import home_media.mcp_server as mcp_server

    # Fresh import state without debug flag (module may have been reloaded).
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001
    if mcp_server._SCREENSHOT_DEBUG_ENABLED:  # noqa: SLF001
        pytest.skip("debug flag enabled in this process")
    assert "capture_room_screenshot" not in names


@pytest.mark.asyncio
async def test_capturer_redacts_udid_in_errors() -> None:
    async def fail_runner(argv, timeout_s=45.0):  # noqa: ANN001
        udid = argv[argv.index("--udid") + 1]
        return 1, b"", f"failed for {udid} at fd12::abcd".encode()

    capturer = PyMobileDeviceScreenshotCapturer(runner=fail_runner, executable="pymobiledevice3")
    result = await capturer.capture_udid(
        "SECRET-UDID-ERR", stable_device_id=LIVING, room_key="living_room"
    )
    assert result.error is not None
    assert "SECRET-UDID-ERR" not in result.error
    assert "fd12::" not in result.error
