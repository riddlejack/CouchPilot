"""In-memory MCP protocol contracts for the six-tool Apple TV agent surface."""

from __future__ import annotations

import base64
import json
import string
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, ImageContent, TextContent

import home_media.agent_mcp as agent_mcp
from home_media.agent_control import AgentObservation
from home_media.models import DeviceKind, DeviceStatus, PowerState
from home_media.wda import (
    WDAElement,
    WDAError,
    WDAIdentity,
    WDAImage,
    WDAObservation,
)

PNG = b"\x89PNG\r\n\x1a\nactual-mcp-image"


class FakeMCPAgent:
    def __init__(self, *, fail_observe: bool = False) -> None:
        self.config = SimpleNamespace(
            country="US",
            subscriptions=["netflix"],
            devices=["living-room"],
        )
        self.fail_observe = fail_observe
        self.direct_calls: list[tuple[str, str, str]] = []

    def devices(self) -> list[dict[str, object]]:
        return [
            {
                "device": "living-room",
                "name": "Living Room",
                "direct_control_configured": True,
                "screen_control_configured": True,
                "managed_helper": False,
                "helper_profile": {
                    "status": "external_unknown",
                    "basis": "external_helper_unchecked",
                    "apple_account_session": "not_checked",
                    "repair_hint": "Check the profile on the external helper host.",
                },
            }
        ]

    async def observe(
        self,
        device: str,
        *,
        include_image: bool = False,
        expected_app: str | None = None,
        expected_label: str | None = None,
    ) -> AgentObservation:
        if self.fail_observe:
            raise WDAError(
                "failed at http://private-host:8100 for PRIVATE-DEVICE-UUID"
            )
        now = datetime.now(UTC)
        image = (
            WDAImage(
                request_started_at=now,
                received_at=now,
                byte_count=len(PNG),
                sha256="image-sha",
                png_bytes=PNG,
            )
            if include_image
            else None
        )
        return AgentObservation(
            observation_id="observation-1",
            device=device,
            observed=WDAObservation(
                active_app="app.current",
                focused_label="q",
                visible=[
                    WDAElement(
                        label="Search",
                        role="SearchField",
                        value="primary",
                        identifier="query",
                        focused=False,
                    ),
                    WDAElement(
                        label="Search Results",
                        role="StaticText",
                        value="Search Results",
                        identifier="Search Results",
                        focused=False,
                    ),
                    *[
                        WDAElement(
                            label=label,
                            role="Key",
                            value=label,
                            identifier=label,
                            focused=label == "q",
                        )
                        for label in string.ascii_lowercase
                    ],
                    WDAElement(
                        label="Delete",
                        role="Key",
                        value=None,
                        identifier="delete-key",
                        focused=False,
                    ),
                    WDAElement(
                        label="Avatar",
                        role="Cell",
                        value="Avatar",
                        identifier="Avatar",
                        focused=False,
                    ),
                ],
                expected_app=expected_app,
                expected_label=expected_label,
                expected_app_verified=expected_app == "app.current",
                expected_label_verified=expected_label == "Search",
                identity=WDAIdentity(
                    helper_build_id="helper-build",
                    session_id="session-1",
                    generation=0,
                ),
                request_started_at=now,
                received_at=now,
                image=image,
                warnings=["visible_elements_truncated"],
            ),
            obtained_monotonic=time.monotonic(),
        )

    async def act(self, device: str, **kwargs: Any) -> dict[str, object]:
        after = await self.observe(device, include_image=bool(kwargs.get("include_image")))
        return {
            "device": device,
            "action_sent": True,
            "outcome": "unverified",
            "after": after,
        }

    async def direct(self, device: str, operation: str, value: str = "") -> object:
        self.direct_calls.append((device, operation, value))
        if operation == "press":
            return {"sent": True, "verified": False}
        if operation == "apps":
            return [{"name": "Settings", "bundle_id": "com.apple.TVSettings"}]
        if operation == "status":
            return DeviceStatus(
                device_id="PRIVATE-DEVICE-ID",
                kind=DeviceKind.APPLE_TV,
                name="Living Room Apple TV",
                power=PowerState.ON,
                current_app="Netflix",
                available=True,
                evidence=["paired_companion"],
            )
        return {"device": device, "operation": operation, "value": value}


@asynccontextmanager
async def _fake_lifespan(
    _server: object,
    agent: FakeMCPAgent,
) -> AsyncIterator[FakeMCPAgent]:
    yield agent


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


@pytest.mark.asyncio
async def test_wire_compaction_does_not_mutate_typed_observation() -> None:
    observation = await FakeMCPAgent().observe("living-room", include_image=True)
    original = observation.model_copy(deep=True)

    agent_mcp._observation_response(observation)  # noqa: SLF001

    assert observation == original


@pytest.mark.asyncio
async def test_actual_mcp_protocol_exposes_exact_six_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMCPAgent()

    @asynccontextmanager
    async def lifespan(server: object) -> AsyncIterator[FakeMCPAgent]:
        async with _fake_lifespan(server, fake) as agent:
            yield agent

    monkeypatch.setattr(agent_mcp.mcp._mcp_server, "lifespan", lifespan)  # noqa: SLF001
    async with create_connected_server_and_client_session(agent_mcp.mcp) as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "devices",
            "observe",
            "act",
            "status",
            "apps",
            "control",
        }
        control_tool = next(tool for tool in tools.tools if tool.name == "control")
        assert "press" in control_tool.inputSchema["properties"]["operation"]["enum"]
        devices = await session.call_tool("devices", {})
        status = await session.call_tool("status", {"device": "living-room"})
        apps = await session.call_tool("apps", {"device": "living-room"})
        control = await session.call_tool(
            "control",
            {"device": "living-room", "operation": "transport", "value": "pause"},
        )
        press = await session.call_tool(
            "control",
            {"device": "living-room", "operation": "press", "value": "Home"},
        )

    assert devices.isError is not True
    assert "living-room" in _text(devices)
    assert "external_unknown" in _text(devices)
    assert "not_checked" in _text(devices)
    assert status.isError is not True
    assert "PRIVATE-DEVICE-ID" not in _text(status)
    assert "Living Room Apple TV" in _text(status)
    assert "paired_companion" in _text(status)
    assert apps.isError is not True
    assert control.isError is not True
    assert press.isError is not True
    assert '"sent":true' in _text(press).replace(" ", "").lower()
    assert '"verified":false' in _text(press).replace(" ", "").lower()
    assert fake.direct_calls == [
        ("living-room", "status", ""),
        ("living-room", "apps", ""),
        ("living-room", "transport", "pause"),
        ("living-room", "press", "Home"),
    ]


@pytest.mark.asyncio
async def test_actual_mcp_observe_returns_image_block_without_bytes_in_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMCPAgent()

    @asynccontextmanager
    async def lifespan(_server: object) -> AsyncIterator[FakeMCPAgent]:
        yield fake

    monkeypatch.setattr(agent_mcp.mcp._mcp_server, "lifespan", lifespan)  # noqa: SLF001
    baseline = await fake.observe(
        "living-room",
        include_image=True,
        expected_app="app.current",
        expected_label="Search",
    )
    baseline_text = json.dumps(
        baseline.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    async with create_connected_server_and_client_session(agent_mcp.mcp) as session:
        result = await session.call_tool(
            "observe",
            {
                "device": "living-room",
                "image": True,
                "expected_app": "app.current",
                "expected_label": "Search",
            },
        )

    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data) == PNG
    text = _text(result)
    assert "observation-1" in text
    assert "png_bytes" not in text
    assert "actual-mcp-image" not in text
    assert len(text) < len(baseline_text) - 1_000

    payload = json.loads(text)
    observed = payload["observed"]
    assert observed["keyboard_keys"] == string.ascii_lowercase.replace("q", "")
    assert observed["errors"] == []
    assert observed["warnings"] == ["visible_elements_truncated"]
    assert observed["request_started_at"]
    assert observed["received_at"]
    assert observed["expected_app_verified"] is True
    assert observed["expected_label_verified"] is True
    assert {"role": "Key", "label": "q", "focused": True} in observed["visible"]
    assert {
        "role": "Key",
        "label": "Delete",
        "identifier": "delete-key",
    } in observed["visible"]
    assert {"role": "Cell", "label": "Avatar"} in observed["visible"]
    assert {
        "role": "SearchField",
        "label": "Search",
        "value": "primary",
        "identifier": "query",
    } in observed["visible"]


@pytest.mark.asyncio
async def test_actual_mcp_act_compacts_nested_after_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMCPAgent()

    @asynccontextmanager
    async def lifespan(_server: object) -> AsyncIterator[FakeMCPAgent]:
        yield fake

    monkeypatch.setattr(agent_mcp.mcp._mcp_server, "lifespan", lifespan)  # noqa: SLF001
    async with create_connected_server_and_client_session(agent_mcp.mcp) as session:
        result = await session.call_tool(
            "act",
            {"device": "living-room", "action": "press", "value": "down"},
        )

    payload = json.loads(_text(result))
    observed = payload["after"]["observed"]
    assert observed["keyboard_keys"] == string.ascii_lowercase.replace("q", "")
    assert {"role": "Key", "label": "q", "focused": True} in observed["visible"]


@pytest.mark.asyncio
async def test_actual_mcp_error_redacts_endpoint_uuid_and_raw_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMCPAgent(fail_observe=True)

    @asynccontextmanager
    async def lifespan(_server: object) -> AsyncIterator[FakeMCPAgent]:
        yield fake

    monkeypatch.setattr(agent_mcp.mcp._mcp_server, "lifespan", lifespan)  # noqa: SLF001
    async with create_connected_server_and_client_session(agent_mcp.mcp) as session:
        result = await session.call_tool("observe", {"device": "living-room"})

    text = _text(result)
    assert "network" in text
    assert "PRIVATE-DEVICE-UUID" not in text
    assert "private-host" not in text
    assert "failed at" not in text
