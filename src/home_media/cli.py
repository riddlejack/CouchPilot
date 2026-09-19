"""Human-friendly CLI over ApplicationService."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from home_media import __version__
from home_media.errors import HomeMediaError, SafetyBlockedError
from home_media.hub import HomeMediaHub
from home_media.service import ApplicationService


def _raw_remote_enabled() -> bool:
    return os.environ.get("HOME_MEDIA_ENABLE_RAW_REMOTE", "").lower() in {
        "1",
        "true",
        "yes",
    }


app = typer.Typer(
    name="home-media",
    help="Local-first room-aware control for Apple TV, TVs, and Sonos",
    no_args_is_help=True,
)
pair_app = typer.Typer(help="Pairing commands")
app_app = typer.Typer(help="App commands")
content_app = typer.Typer(help="Content commands")
transport_app = typer.Typer(help="Transport commands")
volume_app = typer.Typer(help="Volume commands")
rooms_app = typer.Typer(help="Room commands")
power_app = typer.Typer(help="Power commands")
tv_app = typer.Typer(help="Physical TV commands")
scene_app = typer.Typer(help="Scene commands")
remote_app = typer.Typer(help="Remote key commands")
text_app = typer.Typer(help="Text entry commands")
screen_app = typer.Typer(help="Screenshot observer admin/debug commands")

app.add_typer(pair_app, name="pair")
app.add_typer(app_app, name="app")
app.add_typer(content_app, name="content")
app.add_typer(transport_app, name="transport")
app.add_typer(volume_app, name="volume")
app.add_typer(rooms_app, name="rooms")
app.add_typer(power_app, name="power")
app.add_typer(tv_app, name="tv")
app.add_typer(scene_app, name="scene")
app.add_typer(remote_app, name="remote")
app.add_typer(text_app, name="text")
app.add_typer(screen_app, name="screen")

console = Console(stderr=True)
_active_services: list[ApplicationService] = []


def _build_service(use_fakes: bool | None = None) -> ApplicationService:
    if use_fakes is None:
        use_fakes = os.environ.get("HOME_MEDIA_USE_FAKES", "").lower() in {"1", "true", "yes"}
    cfg = os.environ.get("HOME_MEDIA_CONFIG")
    return ApplicationService.from_config_path(
        Path(cfg) if cfg else None,
        use_fakes=use_fakes,
    )


def _svc(use_fakes: bool | None = None) -> ApplicationService:
    service = _build_service(use_fakes)
    _active_services.append(service)
    return service


def _run(coro: Any) -> Any:
    async def _with_lifecycle() -> Any:
        try:
            return await coro
        finally:
            services = list(reversed(_active_services))
            _active_services.clear()
            for service in services:
                await service.aclose()

    return asyncio.run(_with_lifecycle())


def _serialize(data: Any) -> Any:
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json")
    if isinstance(data, list):
        return [_serialize(item) for item in data]
    if isinstance(data, dict):
        return {key: _serialize(value) for key, value in data.items()}
    return data


def _emit(
    data: Any,
    *,
    json_mode: bool,
    ok: bool = True,
    error: dict[str, Any] | None = None,
) -> None:
    payload = _serialize(data)
    env = {"schema_version": 1, "ok": ok, "data": payload, "error": error}
    if json_mode:
        typer.echo(json.dumps(env, indent=2, default=str))
    else:
        if not ok and error:
            console.print(f"[red]{error.get('error')}: {error.get('message')}[/red]")
        else:
            console.print_json(data=env)


def _handle(coro: Any, json_mode: bool) -> None:
    try:
        data = _run(coro)
        _emit(data, json_mode=json_mode, ok=True)
    except HomeMediaError as exc:
        _emit(None, json_mode=json_mode, ok=False, error=exc.to_dict())
        raise typer.Exit(code=2) from exc


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Versioned JSON envelope"),
    fakes: bool = typer.Option(False, "--fakes", help="Use in-memory fake adapters"),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output
    ctx.obj["fakes"] = fakes


@app.command("version")
def version_cmd(ctx: typer.Context) -> None:
    _emit({"version": __version__}, json_mode=ctx.obj["json"])


@app.command("health")
def health_cmd(ctx: typer.Context) -> None:
    async def _health() -> Any:
        hub = HomeMediaHub(lambda: _build_service(ctx.obj["fakes"]))
        try:
            await hub.start()
            return await hub.health()
        finally:
            await hub.aclose()

    _handle(_health(), ctx.obj["json"])


@app.command("discover")
def discover_cmd(
    ctx: typer.Context,
    include_private_inventory: bool = typer.Option(
        False,
        "--include-private-inventory",
        help="Include observed LAN addresses (do not publish)",
    ),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.discover(include_private_inventory=include_private_inventory), ctx.obj["json"])


@rooms_app.command("list")
def rooms_list(ctx: typer.Context) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.list_rooms(), ctx.obj["json"])


@app.command("capabilities")
def capabilities_cmd(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.get_room_capabilities(room), ctx.obj["json"])


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.get_room_status(room), ctx.obj["json"])


@pair_app.command("start")
def pair_start(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    protocol: str = typer.Option("companion", "--protocol"),
    device_role: str = typer.Option("apple_tv", "--device-role"),
) -> None:
    """Deprecated split flow — prefer `home-media pair interactive`."""
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.start_pairing(room, protocol=protocol, device_role=device_role), ctx.obj["json"])


@pair_app.command("finish")
def pair_finish(
    ctx: typer.Context,
    session: str = typer.Option(..., "--session"),
) -> None:
    """Finish pairing; PIN is prompted securely (never via --pin / argv)."""
    svc = _svc(ctx.obj["fakes"])
    pin = typer.prompt("PIN shown on device", hide_input=True)
    _handle(svc.finish_pairing(session, pin), ctx.obj["json"])


@pair_app.command("interactive")
def pair_interactive(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    protocol: str = typer.Option("companion", "--protocol"),
    device_role: str = typer.Option("apple_tv", "--device-role"),
) -> None:
    """Same-process pairing: start, secure PIN prompt, finish, close."""
    svc = _svc(ctx.obj["fakes"])

    async def _run() -> Any:
        session = await svc.start_pairing(room, protocol=protocol, device_role=device_role)
        pin = typer.prompt("PIN shown on device", hide_input=True)
        finished = await svc.finish_pairing(session.session_id, pin)
        await svc.aclose()
        return finished

    _handle(_run(), ctx.obj["json"])


@power_app.command("on")
def power_on(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.set_power(room, "on", dry_run=dry_run), ctx.obj["json"])


@power_app.command("off")
def power_off(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    confirm: bool = typer.Option(False, "--confirm", help="Required for power off"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(
        svc.set_power(room, "off", dry_run=dry_run, confirm_power_off=confirm),
        ctx.obj["json"],
    )


@app_app.command("list")
def app_list(ctx: typer.Context, room: str = typer.Option(..., "--room")) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.list_apps(room), ctx.obj["json"])


@app_app.command("open")
def app_open(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    app_name: str = typer.Option(..., "--app"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.open_app(room, app_name, dry_run=dry_run), ctx.obj["json"])


@content_app.command("open")
def content_open(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    url: str | None = typer.Option(None, "--url"),
    alias: str | None = typer.Option(None, "--alias"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.open_content(room, url=url, alias=alias, dry_run=dry_run), ctx.obj["json"])


@content_app.command("prepare")
def content_prepare(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    title: str = typer.Option(..., "--title"),
    provider: str | None = typer.Option(None, "--provider"),
    goal: str = typer.Option("search_ready", "--goal"),
    wake: bool = typer.Option(True, "--wake/--no-wake"),
    url: str | None = typer.Option(None, "--url"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Prepare content (search_ready / title_open). Never selects a result for search_ready."""
    svc = _svc(ctx.obj["fakes"])
    _handle(
        svc.prepare_content(
            room,
            title,
            provider=provider,
            goal=goal,
            wake=wake,
            url=url,
            dry_run=dry_run,
        ),
        ctx.obj["json"],
    )


@content_app.command("resume")
def content_resume(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    url: str | None = typer.Option(None, "--url"),
    alias: str | None = typer.Option(None, "--alias"),
    service: str | None = typer.Option(None, "--service"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    if service and not url and not alias:
        _handle(svc.open_app(room, service, dry_run=dry_run), ctx.obj["json"])
        return
    _handle(
        svc.open_content(room, url=url, alias=alias, resume=True, dry_run=dry_run),
        ctx.obj["json"],
    )


@transport_app.command("play")
@transport_app.command("pause")
@transport_app.command("stop")
@transport_app.command("next")
@transport_app.command("previous")
def transport_cmd(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    action = ctx.command.name if ctx.command is not None else "play"
    assert action is not None
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.control_playback(room, action, dry_run=dry_run), ctx.obj["json"])


@remote_app.command("press")
def remote_press(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    key: str = typer.Option(..., "--key"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Debug-only raw remote key. Disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1."""
    if not _raw_remote_enabled():
        _emit(
            None,
            json_mode=ctx.obj["json"],
            ok=False,
            error=SafetyBlockedError(
                "CLI remote press disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1",
                reason="raw_remote_disabled",
            ).to_dict(),
        )
        raise typer.Exit(code=2)
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.press_remote_key(room, key, dry_run=dry_run), ctx.obj["json"])


@text_app.command("enter")
def text_enter(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    text: str = typer.Option(..., "--text"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Debug-only raw text entry. Disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1."""
    if not _raw_remote_enabled():
        _emit(
            None,
            json_mode=ctx.obj["json"],
            ok=False,
            error=SafetyBlockedError(
                "CLI text enter disabled unless HOME_MEDIA_ENABLE_RAW_REMOTE=1",
                reason="raw_remote_disabled",
            ).to_dict(),
        )
        raise typer.Exit(code=2)
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.enter_text(room, text, dry_run=dry_run), ctx.obj["json"])


@volume_app.command("get")
def volume_get(ctx: typer.Context, room: str = typer.Option(..., "--room")) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.get_volume(room), ctx.obj["json"])


@volume_app.command("set")
def volume_set(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    level: int = typer.Option(..., "--level"),
    override_ceiling: bool = typer.Option(False, "--override-ceiling"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(
        svc.set_volume(
            room,
            level,
            override_ceiling=override_ceiling,
            dry_run=dry_run,
        ),
        ctx.obj["json"],
    )


@volume_app.command("change")
def volume_change(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    delta: int = typer.Option(..., "--delta"),
    override_ceiling: bool = typer.Option(False, "--override-ceiling"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(
        svc.change_volume(
            room,
            delta,
            override_ceiling=override_ceiling,
            dry_run=dry_run,
        ),
        ctx.obj["json"],
    )


@tv_app.command("input")
def tv_input(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    source: str = typer.Option(..., "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(svc.set_tv_input(room, source, dry_run=dry_run), ctx.obj["json"])


@scene_app.command("watch")
def scene_watch(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    service: str | None = typer.Option(None, "--service"),
    url: str | None = typer.Option(None, "--url"),
    volume: int | None = typer.Option(None, "--volume"),
    override_ceiling: bool = typer.Option(False, "--override-ceiling"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    svc = _svc(ctx.obj["fakes"])
    _handle(
        svc.execute_watch_scene(
            room,
            service=service,
            url=url,
            volume=volume,
            override_ceiling=override_ceiling,
            dry_run=dry_run,
        ),
        ctx.obj["json"],
    )


@screen_app.command("capture")
def screen_capture(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
) -> None:
    """Capture a room-bound Apple TV screenshot into a private ignored directory."""
    svc = _svc(ctx.obj["fakes"])
    if svc.screenshot_service is None:
        _emit(
            None,
            json_mode=ctx.obj["json"],
            ok=False,
            error={
                "error": "unsupported",
                "message": "Screenshot service unavailable",
                "retryable": False,
                "details": {},
            },
        )
        raise typer.Exit(code=2)

    screen = svc.screenshot_service

    async def _run() -> Any:
        assert screen is not None
        result = await screen.capture_room(room, save=True, prune=True)
        meta = result.public_metadata()
        await svc.aclose()
        return meta

    _handle(_run(), ctx.obj["json"])


@screen_app.command("bindings")
def screen_bindings(ctx: typer.Context) -> None:
    """List redacted observer bindings (fingerprints only)."""
    svc = _svc(ctx.obj["fakes"])
    if svc.screenshot_service is None:
        _emit([], json_mode=ctx.obj["json"], ok=True)
        return
    _emit(svc.screenshot_service.bindings.public_view(), json_mode=ctx.obj["json"])


@screen_app.command("bind")
def screen_bind(
    ctx: typer.Context,
    room: str = typer.Option(..., "--room"),
    udid: str = typer.Option(..., "--udid", help="Exact pymobiledevice3 UDID (not logged)"),
    notes: str | None = typer.Option(None, "--notes"),
    allow_unproven_binding: bool = typer.Option(
        False,
        "--allow-unproven-binding",
        help="Narrow migration/debug override: persist without a nonblank capture proof",
    ),
) -> None:
    """Persist a one-time user-confirmed room → Apple TV → observer UDID binding.

    Normal flow captures from the exact UDID in this session and requires a
    nonblank frame before persisting. Raw UDID is never emitted in success logs.
    """

    async def _bind() -> dict[str, Any]:
        svc = _svc(ctx.obj["fakes"])
        if svc.screenshot_service is None:
            raise SafetyBlockedError(
                "Screenshot service unavailable",
                reason="screenshot_unavailable",
            )
        capture_proof = False
        try:
            if not allow_unproven_binding:
                room_key, stable_id = svc.screenshot_service.apple_tv_id_for_room(room)
                svc.screenshot_service.bindings.confirm(
                    room_key=room_key,
                    stable_device_id=stable_id,
                    observer_udid=udid,
                    notes=notes,
                )
                svc.screenshot_service._provider = None  # noqa: SLF001 — rebuild mapping
                shot = await svc.screenshot_service.capture_room(room, save=True, prune=True)
                if shot.error or shot.png_bytes is None or shot.blank_or_protected:
                    svc.screenshot_service.bindings.bindings.pop(stable_id, None)
                    raise SafetyBlockedError(
                        "Binding not persisted: capture from exact UDID was blank, "
                        "missing, or failed",
                        reason="binding_capture_proof_failed",
                    )
                capture_proof = True
            data = svc.screenshot_service.confirm_binding(
                room=room,
                observer_udid=udid,
                notes=notes,
                persist=True,
                capture_proof_nonblank=capture_proof,
                allow_unproven=allow_unproven_binding,
            )
            return dict(data)
        finally:
            await svc.aclose()

    _handle(_bind(), ctx.obj["json"])


@screen_app.command("pair")
def screen_pair(
    ctx: typer.Context,
    name: str = typer.Option(
        ...,
        "--name",
        help="Exact on-screen Apple TV display name passed to pymobiledevice3",
    ),
    confirm_physical_presence: bool = typer.Option(
        False,
        "--confirm-physical-presence",
        help="Confirm you are at that television and it is safe to show a pairing code",
    ),
) -> None:
    """Interactive developer remote-pair. PIN is typed into the child process, never argv.

    Prerequisite: on the Apple TV open Settings → Remotes and Devices →
    Remote App and Devices and leave that screen open.
    """
    import subprocess

    if not confirm_physical_presence:
        exc = SafetyBlockedError(
            "Screenshot pairing requires physical presence at the named television",
            reason="physical_presence_confirmation_required",
        )
        _emit(None, json_mode=ctx.obj["json"], ok=False, error=exc.to_dict())
        raise typer.Exit(code=2)

    from home_media.observers.capture import resolve_capture_python

    exe = resolve_capture_python()
    exe_name = Path(exe).name
    if exe_name == "pymobiledevice3":
        argv = [exe, "remote", "pair"]
    else:
        argv = [exe, "-m", "pymobiledevice3", "remote", "pair"]
    argv.extend(["--name", name])
    console.print(
        "[yellow]Starting interactive pymobiledevice3 remote pair. "
        "Enter the six-digit PIN in this terminal when prompted. "
        "PIN is never accepted on argv and is not persisted by home-media.[/yellow]"
    )
    # Inherit stdio so the user types the PIN directly into pymobiledevice3.
    completed = subprocess.run(argv, check=False)  # noqa: S603 — fixed argv, no shell
    if completed.returncode != 0:
        raise typer.Exit(code=completed.returncode or 2)
    _emit(
        {
            "ok": True,
            "note": "Pairing process exited successfully; confirm room binding via "
            "`home-media screen bind --room … --udid …` after capture",
        },
        json_mode=ctx.obj["json"],
    )


# Silence unused Table import if human formatting expands later
_ = Table

if __name__ == "__main__":
    app()
