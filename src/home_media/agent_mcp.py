"""Compact stdio MCP surface for a user's existing Codex/Claude agent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession
from pydantic import BaseModel

from home_media.agent_control import AgentObservation, AppleTVAgent, load_agent_config
from home_media.errors import ErrorCode, HomeMediaError
from home_media.models import DeviceStatus
from home_media.wda import WDAElement, WDAObservation


@asynccontextmanager
async def lifespan(_server: FastMCP[AppleTVAgent]) -> AsyncIterator[AppleTVAgent]:
    agent = AppleTVAgent(load_agent_config())
    try:
        yield agent
    finally:
        await agent.aclose()


mcp = FastMCP(
    "apple-tv-agent",
    instructions=(
        "Control configured Apple TVs. Start with devices and inspect helper_profile; it checks "
        "local cached signing evidence, not Apple account login. Then observe. Prefer compact "
        "text; request an image for unfamiliar or incomplete UI. Use the returned "
        "observation_id for UI actions. After a timeout, observe before deciding whether to "
        "act again. An acknowledged action is not proof of a completed user goal."
    ),
    lifespan=lifespan,
    log_level="WARNING",
)


def _agent(ctx: Context[ServerSession, AppleTVAgent]) -> AppleTVAgent:
    return ctx.request_context.lifespan_context


def _element_json(element: WDAElement) -> dict[str, object]:
    result: dict[str, object] = {"role": element.role}
    if element.label is not None:
        result["label"] = element.label
    if element.value is not None and element.value != element.label:
        result["value"] = element.value
    if element.identifier is not None and element.identifier != element.label:
        result["identifier"] = element.identifier
    if element.focused:
        result["focused"] = True
    return result


def _ordinary_keyboard_key(element: WDAElement) -> bool:
    return (
        element.role == "Key"
        and element.label is not None
        and len(element.label) == 1
        and element.focused is False
        and element.value in {None, element.label}
        and element.identifier in {None, element.label}
    )


def _observation_json(observed: WDAObservation) -> dict[str, Any]:
    result = observed.model_dump(mode="json", exclude={"visible"})
    visible: list[dict[str, object]] = []
    keyboard_keys: list[str] = []
    for element in observed.visible:
        if _ordinary_keyboard_key(element):
            keyboard_keys.append(element.label or "")
        else:
            visible.append(_element_json(element))
    result["visible"] = visible
    if keyboard_keys:
        result["keyboard_keys"] = "".join(keyboard_keys)
    return result


def _json(value: Any) -> Any:
    if isinstance(value, WDAElement):
        return _element_json(value)
    if isinstance(value, WDAObservation):
        return _observation_json(value)
    if isinstance(value, AgentObservation):
        result = value.model_dump(mode="json", exclude={"observed"})
        result["observed"] = _observation_json(value.observed)
        return result
    if isinstance(value, DeviceStatus):
        return _json(value.model_dump(mode="json", exclude={"device_id"}))
    if isinstance(value, BaseModel):
        return _json(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HomeMediaError):
        # Do not forward adapter endpoint/device identifiers or raw server errors.
        remedies = {
            ErrorCode.PARTIAL: (
                "The input may have taken effect. Observe again; do not repeat it blindly."
            ),
            ErrorCode.AUTH_REQUIRED: (
                "Pair this device with apple-tv-agent pair on the local bridge."
            ),
            ErrorCode.AUTH_FAILED: (
                "Pairing was rejected. Check the device and run apple-tv-agent pair."
            ),
            ErrorCode.CONFIG: "Run apple-tv-agent doctor on the local bridge to check setup.",
            ErrorCode.UNSUPPORTED: (
                "This route is unavailable. Check devices and apple-tv-agent doctor."
            ),
            ErrorCode.VERIFICATION_FAILED: "The target was not uniquely verified. Observe again.",
        }
        reasons = {
            "target_not_focused": (
                "The target is not focused. Navigate one direction at a time "
                "and observe before Select."
            ),
            "stale_observation": "Observe again and use the new observation_id.",
            "semantic_observation_required": (
                "Use fresh labels for select/type; image fallback supports press."
            ),
            "helper_restarted": "The helper session changed. Observe again before acting.",
            "device_identity_mismatch": (
                "The endpoint belongs to a different device. Recheck its binding."
            ),
            "device_in_use": "Another bridge owns this device. Use that bridge or close it first.",
            "mutations_disabled": "Mutations are disabled in the bridge configuration.",
        }
        reason = exc.details.get("reason")
        return {
            "ok": False,
            "error": exc.code.value,
            "message": reasons.get(
                str(reason),
                remedies.get(
                    exc.code, "Operation did not complete; check setup or observe before retrying."
                ),
            ),
            "do_not_retry_blindly": True,
        }
    return {
        "ok": False,
        "error": type(exc).__name__,
        "message": "Outcome unverified. Observe before repeating an action; doctor checks setup.",
    }


def _observation_response(result: Any) -> list[Any]:
    state = result if isinstance(result, AgentObservation) else result.get("after")
    response: list[Any] = [json.dumps(_json(result), ensure_ascii=False, separators=(",", ":"))]
    if isinstance(state, AgentObservation):
        image = getattr(state.observed, "image", None)
        if image is not None and getattr(image, "png_bytes", None):
            response.append(Image(data=image.png_bytes, format="png"))
    return response


@mcp.tool()
async def devices(ctx: Context[ServerSession, AppleTVAgent]) -> dict[str, Any]:
    """List routes and freshly inspect local cached helper-profile expiry evidence."""
    agent = _agent(ctx)
    return {
        "devices": agent.devices(),
        "country": agent.config.country,
        "preferred_profile": getattr(agent.config, "preferred_profile", None),
        "subscriptions": agent.config.subscriptions,
        "setup": None if agent.config.devices else "Run apple-tv-agent configure on the bridge",
    }


@mcp.tool(structured_output=False)
async def observe(
    device: str,
    ctx: Context[ServerSession, AppleTVAgent],
    image: bool = False,
    expected_app: str | None = None,
    expected_label: str | None = None,
) -> list[Any]:
    """Read fresh app, focus, labels, and values. Request an image only when useful."""
    try:
        return _observation_response(
            await _agent(ctx).observe(
                device,
                include_image=image,
                expected_app=expected_app,
                expected_label=expected_label,
            )
        )
    except Exception as exc:
        return [_error(exc)]


@mcp.tool(structured_output=False)
async def act(
    device: str,
    action: Literal["press", "select", "type", "launch"],
    value: str,
    ctx: Context[ServerSession, AppleTVAgent],
    observation_id: str | None = None,
    role: Literal["Cell", "Button", "Icon", "TextField", "SearchField", "Other"] = "Cell",
    expected_app: str | None = None,
    expected_label: str | None = None,
    image: bool = False,
) -> list[Any]:
    """Act once, then observe. UI inputs need observation_id.

    Select activates a unique, already focused label without moving focus.
    """
    try:
        result = await _agent(ctx).act(
            device,
            action=action,
            value=value,
            observation_id=observation_id,
            role=role,
            expected_app=expected_app,
            expected_label=expected_label,
            include_image=image,
        )
        return _observation_response(result)
    except Exception as exc:
        return [_error(exc)]


@mcp.tool()
async def status(device: str, ctx: Context[ServerSession, AppleTVAgent]) -> dict[str, Any]:
    """Read power and now-playing metadata through paired control; no screenshots needed."""
    try:
        return {"ok": True, "data": _json(await _agent(ctx).direct(device, "status"))}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def apps(device: str, ctx: Context[ServerSession, AppleTVAgent]) -> dict[str, Any]:
    """List installed apps and their bundle IDs through paired control."""
    try:
        return {"ok": True, "data": _json(await _agent(ctx).direct(device, "apps"))}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def control(
    device: str,
    operation: Literal[
        "wake",
        "sleep",
        "transport",
        "press",
        "open_url",
        "launch",
        "type",
    ],
    ctx: Context[ServerSession, AppleTVAgent],
    value: str = "",
) -> dict[str, Any]:
    """Paired control. Use press for explicit buttons; observe agent-chosen navigation."""
    try:
        return {"ok": True, "data": _json(await _agent(ctx).direct(device, operation, value))}
    except Exception as exc:
        return _error(exc)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
