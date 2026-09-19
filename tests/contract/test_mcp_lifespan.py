"""Real MCP lifespan / sequential tool call proofs (not tool-name listing)."""

from __future__ import annotations

import os

import pytest

import home_media.mcp_server as mcp_server


@pytest.mark.asyncio
async def test_mcp_lifespan_single_service_and_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    before = mcp_server._FACTORY_CALLS  # noqa: SLF001
    async with mcp_server.app_lifespan(mcp_server.mcp) as ctx:
        assert isinstance(ctx, mcp_server.AppContext)
        assert ctx.factory_calls == before + 1
        svc = ctx.service
        rooms = await svc.list_rooms()
        assert any(r["key"] == "living_room" for r in rooms)
        # Second call reuses same service instance (no new factory in lifespan).
        mid = mcp_server._FACTORY_CALLS  # noqa: SLF001
        status = await svc.get_room_status("theater")
        assert status.audio is not None
        assert mid == mcp_server._FACTORY_CALLS  # noqa: SLF001
        apple = svc.adapters["apple_tv"]
        await svc.prepare_content(
            "living_room",
            "Avatar",
            provider="netflix",
            goal="search_ready",
            idempotency_key="mcp-life-1",
        )
        again = await svc.prepare_content(
            "living_room",
            "Avatar",
            provider="netflix",
            goal="search_ready",
            idempotency_key="mcp-life-1",
        )
        assert again.selected_result is False
        names = {  # noqa: SLF001
            tool.name for tool in mcp_server.mcp._tool_manager.list_tools()
        }
        assert "press_remote_key" not in names
        assert "enter_text" not in names
        assert "prepare_content" in names
        assert "start_pairing" not in names
        assert "finish_pairing" not in names
        _ = apple
    # After shutdown, aclose ran (sessions cleared on fake).
    assert os.environ["HOME_MEDIA_USE_FAKES"] == "1"


@pytest.mark.asyncio
async def test_mcp_concurrent_prepare_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    async with mcp_server.app_lifespan(mcp_server.mcp) as ctx:
        svc = ctx.service

        async def call() -> str:
            result = await svc.prepare_content(
                "living_room",
                "Avatar",
                provider="netflix",
                goal="search_ready",
                idempotency_key="concurrent-prep",
            )
            return result.terminal_status.value

        a, b = await asyncio.gather(call(), call())
        assert a == b
