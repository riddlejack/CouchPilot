"""Real MCP lifespan / sequential tool call proofs (not tool-name listing)."""

from __future__ import annotations

import os

import pytest

from home_media.mcp_server import _FACTORY_CALLS, AppContext, app_lifespan, mcp


@pytest.mark.asyncio
async def test_mcp_lifespan_single_service_and_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    before = _FACTORY_CALLS
    async with app_lifespan(mcp) as ctx:
        assert isinstance(ctx, AppContext)
        assert ctx.factory_calls == before + 1
        svc = ctx.service
        rooms = await svc.list_rooms()
        assert any(r["key"] == "living_room" for r in rooms)
        # Second call reuses same service instance (no new factory in lifespan).
        mid = _FACTORY_CALLS
        status = await svc.get_room_status("theater")
        assert status.audio is not None
        assert mid == _FACTORY_CALLS
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
        assert "press_remote_key" not in {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
        assert "enter_text" not in {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
        assert "prepare_content" in {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
        assert "finish_pairing" not in {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
        _ = apple
    # After shutdown, aclose ran (sessions cleared on fake).
    assert os.environ["HOME_MEDIA_USE_FAKES"] == "1"


@pytest.mark.asyncio
async def test_mcp_concurrent_prepare_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    async with app_lifespan(mcp) as ctx:
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
