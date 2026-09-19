"""MCP Computer Use returns images and enforces exact-frame one-action control."""

from __future__ import annotations

import importlib

import pytest
from mcp.server.fastmcp.utilities.types import Image

from home_media.hub import HomeMediaHub
from home_media.service import ApplicationService


@pytest.mark.asyncio
async def test_computer_use_observe_and_one_action_return_real_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME_MEDIA_ENABLE_COMPUTER_USE", "1")
    import home_media.mcp_server as mcp_server

    mcp_server = importlib.reload(mcp_server)
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001
    assert {"observe_apple_tv", "act_apple_tv"} <= names

    service = ApplicationService.from_config_path(use_fakes=True)
    hub = HomeMediaHub(lambda: service)
    await hub.start()

    class _Ctx:
        class request_context:
            class lifespan_context:
                pass

    _Ctx.request_context.lifespan_context.hub = hub
    try:
        observed = await mcp_server.observe_apple_tv("living_room", ctx=_Ctx())  # type: ignore[arg-type]
        metadata = next(item for item in observed if isinstance(item, dict))
        sequence = metadata["data"]["sequence"]
        acted = await mcp_server.act_apple_tv(  # type: ignore[attr-defined]
            "living_room",
            sequence,
            "right",
            ctx=_Ctx(),  # type: ignore[arg-type]
        )
    finally:
        await hub.aclose()

    assert any(isinstance(item, Image) for item in observed)
    assert any(isinstance(item, Image) for item in acted)
    acted_metadata = next(item for item in acted if isinstance(item, dict))
    assert acted_metadata["ok"] is True
    assert acted_metadata["data"]["before_sequence"] == sequence
    assert acted_metadata["data"]["after"]["sequence"] == sequence + 1

    monkeypatch.delenv("HOME_MEDIA_ENABLE_COMPUTER_USE", raising=False)
    importlib.reload(mcp_server)


@pytest.mark.asyncio
async def test_computer_use_mcp_refuses_blind_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME_MEDIA_ENABLE_COMPUTER_USE", "1")
    import home_media.mcp_server as mcp_server

    mcp_server = importlib.reload(mcp_server)
    response = await mcp_server.act_apple_tv(  # type: ignore[attr-defined]
        "living_room",
        1,
        "select",
        visible_target=None,
        ctx=None,
    )
    assert response[0]["ok"] is False
    assert response[0]["error"]["error"] == "safety_blocked"

    monkeypatch.delenv("HOME_MEDIA_ENABLE_COMPUTER_USE", raising=False)
    importlib.reload(mcp_server)
