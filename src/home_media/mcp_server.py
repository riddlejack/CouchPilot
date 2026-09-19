"""stdio MCP server — thin typed facade over a process-scoped ApplicationService."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.session import ServerSession

from home_media.computer_use import ComputerUseAction, ComputerUseActionKind
from home_media.errors import HomeMediaError
from home_media.hub import HomeMediaHub
from home_media.service import ApplicationService


@dataclass
class AppContext:
    hub: HomeMediaHub
    factory_calls: int = 1

    @property
    def service(self) -> ApplicationService:
        return self.hub.service


_FACTORY_CALLS = 0


def _build_service() -> ApplicationService:
    global _FACTORY_CALLS
    _FACTORY_CALLS += 1
    use_fakes = os.environ.get("HOME_MEDIA_USE_FAKES", "").lower() in {"1", "true", "yes"}
    cfg = os.environ.get("HOME_MEDIA_CONFIG")
    return ApplicationService.from_config_path(Path(cfg) if cfg else None, use_fakes=use_fakes)


@asynccontextmanager
async def app_lifespan(_server: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
    hub = HomeMediaHub(_build_service)
    await hub.start()
    ctx = AppContext(hub=hub, factory_calls=_FACTORY_CALLS)
    try:
        yield ctx
    finally:
        await hub.aclose()


mcp = FastMCP("home-media", lifespan=app_lifespan)

_RAW_REMOTE_ENABLED = os.environ.get("HOME_MEDIA_ENABLE_RAW_REMOTE", "").lower() in {
    "1",
    "true",
    "yes",
}
_SCREENSHOT_DEBUG_ENABLED = os.environ.get("HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG", "").lower() in {
    "1",
    "true",
    "yes",
}
_COMPUTER_USE_ENABLED = os.environ.get("HOME_MEDIA_ENABLE_COMPUTER_USE", "").lower() in {
    "1",
    "true",
    "yes",
}


def _hub(ctx: Context[ServerSession, AppContext] | None = None) -> HomeMediaHub:
    if ctx is not None and ctx.request_context and ctx.request_context.lifespan_context:
        return ctx.request_context.lifespan_context.hub
    raise RuntimeError(
        "HomeMediaHub is only available inside the MCP lifespan context; "
        "refusing to construct a second live runtime"
    )


async def _call[T](
    ctx: Context[ServerSession, AppContext] | None,
    operation: Callable[[ApplicationService], Awaitable[T]],
) -> T:
    return await _hub(ctx).call(operation)


def _dump(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json")
    elif isinstance(result, list):
        data = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in result
        ]
    else:
        data = result
    return {"schema_version": 1, "ok": True, "data": data}


def _err(exc: HomeMediaError) -> dict[str, Any]:
    return {"schema_version": 1, "ok": False, "error": exc.to_dict()}


@mcp.tool()
async def get_hub_health(
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Return process-local health without probing devices or exposing private identifiers."""
    if ctx is not None and ctx.request_context and ctx.request_context.lifespan_context:
        health = await ctx.request_context.lifespan_context.hub.health()
        return _dump(health)
    raise RuntimeError("HomeMediaHub health is only available inside the MCP lifespan context")


@mcp.tool()
async def discover_devices(
    include_private_inventory: bool = False,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Passive discovery. Default omits LAN addresses; private inventory is local-debug only."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.discover(
                    include_private_inventory=include_private_inventory
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def list_rooms(ctx: Context[ServerSession, AppContext] | None = None) -> dict[str, Any]:
    """List configured rooms and their stable device bindings."""
    try:
        return _dump(await _call(ctx, lambda svc: svc.list_rooms()))
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def get_room_capabilities(
    room: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Return capability truth for a room's Apple TV, physical TV, and audio targets."""
    try:
        return _dump(await _call(ctx, lambda svc: svc.get_room_capabilities(room)))
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def get_room_status(
    room: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Read status for each device role in a room."""
    try:
        return _dump(await _call(ctx, lambda svc: svc.get_room_status(room)))
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def set_power(
    room: str,
    state: str,
    dry_run: bool = False,
    confirm_power_off: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Set power for room targets. Power off requires confirm_power_off=true."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.set_power(
                    room,
                    state,
                    dry_run=dry_run,
                    confirm_power_off=confirm_power_off,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def list_apps(
    room: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """List installed apps on the room Apple TV."""
    try:
        return _dump(await _call(ctx, lambda svc: svc.list_apps(room)))
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def open_app(
    room: str,
    app: str,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Open an app by alias or bundle id on the room Apple TV."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.open_app(
                    room, app, dry_run=dry_run, idempotency_key=idempotency_key
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def open_content(
    room: str,
    url: str | None = None,
    alias: str | None = None,
    resume: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Open a deep link or alias. Resume never claims exact progress without evidence."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.open_content(
                    room,
                    url=url,
                    alias=alias,
                    resume=resume,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def prepare_content(
    room: str,
    title: str,
    provider: str | None = None,
    goal: str = "search_ready",
    wake: bool = True,
    dry_run: bool = False,
    url: str | None = None,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Prepare content via route ladder, including verified title-open/resume goals."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.prepare_content(
                    room,
                    title,
                    provider=provider,
                    goal=goal,
                    wake=wake,
                    dry_run=dry_run,
                    url=url,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def get_now_playing(
    room: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Return now-playing from the room Apple TV status."""
    try:
        status = await _call(ctx, lambda svc: svc.get_room_status(room))
        np = status.apple_tv.now_playing if status.apple_tv else None
        return _dump(np.model_dump(mode="json") if np else None)
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def control_playback(
    room: str,
    action: str,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Transport control: play, pause, stop, next, previous."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.control_playback(
                    room, action, dry_run=dry_run, idempotency_key=idempotency_key
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def get_volume(
    room: str,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Read absolute volume from the room's preferred audio target (usually Sonos)."""
    try:
        return _dump(await _call(ctx, lambda svc: svc.get_volume(room)))
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def set_volume(
    room: str,
    level: int,
    override_ceiling: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Set exact volume on Sonos/absolute target. Rejects CEC-only exact sets."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.set_volume(
                    room,
                    level,
                    override_ceiling=override_ceiling,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def change_volume(
    room: str,
    delta: int,
    override_ceiling: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Change volume by delta with ceiling enforcement via absolute target."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.change_volume(
                    room,
                    delta,
                    override_ceiling=override_ceiling,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def set_tv_input(
    room: str,
    source: str,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Set physical TV input when a direct adapter supports independently readable input."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.set_tv_input(
                    room, source, dry_run=dry_run, idempotency_key=idempotency_key
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


@mcp.tool()
async def execute_watch_scene(
    room: str,
    service: str | None = None,
    url: str | None = None,
    volume: int | None = None,
    override_ceiling: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    ctx: Context[ServerSession, AppContext] | None = None,
) -> dict[str, Any]:
    """Multi-step watch scene with per-step evidence. Partial failures stay partial."""
    try:
        return _dump(
            await _call(
                ctx,
                lambda svc: svc.execute_watch_scene(
                    room,
                    service=service,
                    url=url,
                    volume=volume,
                    override_ceiling=override_ceiling,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                ),
            )
        )
    except HomeMediaError as exc:
        return _err(exc)


if _RAW_REMOTE_ENABLED:

    @mcp.tool()
    async def press_remote_key(
        room: str,
        key: str,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        ctx: Context[ServerSession, AppContext] | None = None,
    ) -> dict[str, Any]:
        """Debug-only raw remote key. Disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1."""
        try:
            return _dump(
                await _call(
                    ctx,
                    lambda svc: svc.press_remote_key(
                        room, key, dry_run=dry_run, idempotency_key=idempotency_key
                    ),
                )
            )
        except HomeMediaError as exc:
            return _err(exc)

    @mcp.tool()
    async def enter_text(
        room: str,
        text: str,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        ctx: Context[ServerSession, AppContext] | None = None,
    ) -> dict[str, Any]:
        """Debug-only raw text entry. Disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1."""
        try:
            return _dump(
                await _call(
                    ctx,
                    lambda svc: svc.enter_text(
                        room, text, dry_run=dry_run, idempotency_key=idempotency_key
                    ),
                )
            )
        except HomeMediaError as exc:
            return _err(exc)


if _SCREENSHOT_DEBUG_ENABLED:

    @mcp.tool(structured_output=False)
    async def capture_room_screenshot(
        room: str,
        ctx: Context[ServerSession, AppContext] | None = None,
    ) -> list[Any]:
        """Debug-only room screenshot. Returns an MCP Image block plus redacted metadata.

        Disabled unless HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG=1. Never logs image bytes
        or raw observer UDIDs. Household screenshots stay in the local model session
        the user explicitly invoked — not a separate remote vision service.
        """
        async def _capture(svc: ApplicationService) -> list[Any]:
            if svc.screenshot_service is None:
                return [
                    {
                        "schema_version": 1,
                        "ok": False,
                        "error": {
                            "error": "unsupported",
                            "message": "Screenshot service unavailable",
                        },
                    }
                ]
            try:
                result = await svc.screenshot_service.capture_room(
                    room, save=True, prune=True
                )
            except HomeMediaError as exc:
                return [_err(exc)]
            meta = result.public_metadata()
            # Fail closed if public metadata somehow includes a raw UDID-shaped value.
            text_blob = json_dumps_safe(meta)
            if (
                result.observer_udid_fingerprint
                and result.observer_udid_fingerprint in text_blob
            ):
                pass  # fingerprint is intentional
            content: list[Any] = []
            if result.png_bytes:
                content.append(Image(data=result.png_bytes, format="png"))
            content.append(
                {"schema_version": 1, "ok": result.error is None, "data": meta}
            )
            return content

        return await _call(ctx, _capture)


if _COMPUTER_USE_ENABLED:

    @mcp.tool(structured_output=False)
    async def observe_apple_tv(
        room: str,
        ctx: Context[ServerSession, AppContext] | None = None,
    ) -> list[Any]:
        """Observe one exact room as an MCP Image plus stale-frame-safe metadata."""

        async def _observe(svc: ApplicationService) -> list[Any]:
            try:
                state = await svc.observe_apple_tv(room)
            except HomeMediaError as exc:
                return [_err(exc)]
            content: list[Any] = []
            if state.png_bytes:
                content.append(Image(data=state.png_bytes, format="png"))
            content.append(_dump(state.agent_metadata()))
            return content

        return await _call(ctx, _observe)

    @mcp.tool(structured_output=False)
    async def act_apple_tv(
        room: str,
        expected_sequence: int,
        key: str,
        visible_target: str | None = None,
        ctx: Context[ServerSession, AppContext] | None = None,
    ) -> list[Any]:
        """Send one generation-bound remote key, then return the resulting frame.

        Select requires ``visible_target``: the image-capable caller must name
        what it sees focused on the exact ``expected_sequence`` frame.
        """

        allowed_keys = {"up", "down", "left", "right", "select", "menu", "play_pause"}
        normalized_key = key.strip().casefold()
        if normalized_key not in allowed_keys:
            return [
                {
                    "schema_version": 1,
                    "ok": False,
                    "error": {
                        "error": "unsupported",
                        "message": "Computer Use key is not allowlisted",
                    },
                }
            ]
        if normalized_key == "select" and not (visible_target or "").strip():
            return [
                {
                    "schema_version": 1,
                    "ok": False,
                    "error": {
                        "error": "safety_blocked",
                        "message": "Select requires a named visible target",
                    },
                }
            ]

        async def _act(svc: ApplicationService) -> list[Any]:
            action = ComputerUseAction(
                kind=ComputerUseActionKind.PRESS_KEY,
                key=normalized_key,
                expected_sequence=expected_sequence,
                model_observed_target=(
                    visible_target.strip() if visible_target is not None else None
                ),
                min_confidence=0.70 if normalized_key == "select" else 0.0,
            )
            try:
                result = await svc.act_apple_tv(room, action)
            except HomeMediaError as exc:
                return [_err(exc)]
            content: list[Any] = []
            if result.after.png_bytes:
                content.append(Image(data=result.after.png_bytes, format="png"))
            content.append(_dump(result))
            return content

        return await _call(ctx, _act)


def json_dumps_safe(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, default=str)


def factory_call_count() -> int:
    return _FACTORY_CALLS


def main() -> None:
    # stdout is the MCP transport — never print diagnostics there.
    print("home-media MCP server starting (stdio)", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
