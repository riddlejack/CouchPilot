"""CLI and MCP facade contract checks."""

from __future__ import annotations

import json
import os

from typer.testing import CliRunner

from home_media.cli import app
from home_media.mcp_server import mcp

runner = CliRunner()


def test_cli_rooms_json() -> None:
    result = runner.invoke(app, ["--json", "--fakes", "rooms", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    keys = {r["key"] for r in payload["data"]}
    assert "theater" in keys


def test_cli_dry_run_volume() -> None:
    result = runner.invoke(
        app,
        ["--json", "--fakes", "volume", "set", "--room", "theater", "--level", "25", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["execution_status"] == "dry_run"


def test_cli_unknown_room_exit_code() -> None:
    result = runner.invoke(app, ["--json", "--fakes", "status", "--room", "nope"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False


def test_mcp_tool_names_are_narrow() -> None:
    # FastMCP keeps tools in _tool_manager
    tools = mcp._tool_manager.list_tools()  # noqa: SLF001 — contract introspection
    names = {t.name for t in tools}
    assert "discover_devices" in names
    assert "execute_watch_scene" in names
    assert "set_volume" in names
    assert "prepare_content" in names
    # Raw remote / PIN finish are not on the default agent surface
    assert "press_remote_key" not in names
    assert "enter_text" not in names
    assert "finish_pairing" not in names
    # Never expose a generic shell
    assert "shell" not in names
    assert "run_command" not in names
    assert "execute_bash" not in names
    # Screenshot debug tool is opt-in via HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG
    from home_media import mcp_server as _mcp_mod

    if not _mcp_mod._SCREENSHOT_DEBUG_ENABLED:  # noqa: SLF001
        assert "capture_room_screenshot" not in names


def test_mcp_server_import_safe(monkeypatch) -> None:
    monkeypatch.setenv("HOME_MEDIA_USE_FAKES", "1")
    assert os.environ["HOME_MEDIA_USE_FAKES"] == "1"
    from home_media import mcp_server

    assert mcp_server.mcp.name == "home-media"
