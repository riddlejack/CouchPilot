"""Setup and diagnostics for the small agent bridge."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from home_media.agent_control import (
    AgentConfig,
    AgentDevice,
    AppleTVAgent,
    HelperConfig,
    load_agent_config,
    save_agent_config,
)
from home_media.models import DiscoveredEndpoint
from home_media.wda import WDAClient
from home_media.wda_runtime import (
    WDARuntime,
    WDARuntimeConfig,
    WDARuntimeState,
    discover_cached_xctestruns,
    discover_xcode_developer_dirs,
)

app = typer.Typer(no_args_is_help=True, help="Set up Apple TV control for your existing agent.")


class HelperBackend(StrEnum):
    XCODE = "xcode"
    NATIVE = "native"


class SetupError(RuntimeError):
    """A safe, actionable setup failure that contains no private identifiers."""

    def __init__(self, code: str, remedy: str) -> None:
        super().__init__(code)
        self.code = code
        self.remedy = remedy


def emit(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


def run(operation: Any) -> Any:
    try:
        return asyncio.run(operation)
    except SetupError as exc:
        emit({"ok": False, "error": exc.code, "remedy": exc.remedy})
        raise typer.Exit(1) from None
    except Exception as exc:
        # Pairing tokens, endpoint URLs and device identifiers stay out of errors.
        emit(
            {
                "ok": False,
                "error": type(exc).__name__,
                "remedy": "Check your device/configuration, then run apple-tv-agent doctor.",
            }
        )
        raise typer.Exit(1) from None


def run_interactive(operation: Any) -> Any:
    """Run prompt-bearing async work without asyncio.run's deferred SIGINT handling."""

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(operation)
    except typer.Abort:
        raise
    except SetupError as exc:
        emit({"ok": False, "error": exc.code, "remedy": exc.remedy})
        raise typer.Exit(1) from None
    except Exception as exc:
        emit(
            {
                "ok": False,
                "error": type(exc).__name__,
                "remedy": "Check your device/configuration, then run apple-tv-agent doctor.",
            }
        )
        raise typer.Exit(1) from None
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


_DEVICE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return (slug or "apple-tv")[:64].rstrip("-")


def _available_key(value: str, unavailable: set[str]) -> str:
    base = _slug(value)
    if base not in unavailable:
        return base
    suffix = 2
    while True:
        marker = f"-{suffix}"
        candidate = f"{base[: 64 - len(marker)].rstrip('-')}{marker}"
        if candidate not in unavailable:
            return candidate
        suffix += 1


def _prompt_device(devices: list[DiscoveredEndpoint]) -> DiscoveredEndpoint:
    typer.echo("\nApple TVs found on this local network:")
    for index, device in enumerate(devices, start=1):
        model = device.model or "model unavailable"
        typer.echo(f"  {index}. {device.name} — {model} — {device.address}")

    while True:
        selection = cast(int, typer.prompt("Select device number", type=int))
        if 1 <= selection <= len(devices):
            return devices[selection - 1]
        typer.echo(f"Choose a number from 1 to {len(devices)}.", err=True)


def _prompt_name(default: str) -> str:
    while True:
        value = cast(str, typer.prompt("Friendly name", default=default)).strip()
        if value:
            return value
        typer.echo("Friendly name cannot be empty.", err=True)


def _prompt_key(default: str, unavailable: set[str]) -> str:
    while True:
        value = cast(str, typer.prompt("Device key", default=default)).strip()
        if not _DEVICE_KEY_PATTERN.fullmatch(value):
            typer.echo(
                "Use 1-64 lowercase letters, numbers, hyphens, or underscores; "
                "start with a letter or number.",
                err=True,
            )
            continue
        if value in unavailable:
            typer.echo(
                f"The key '{value}' belongs to another configured device. Choose another key.",
                err=True,
            )
            continue
        return value


def _discovery_identifiers(device: DiscoveredEndpoint) -> set[str]:
    """Return advertised hardware aliases without treating a display name as identity."""

    values: list[str] = [device.device_id]
    raw_identifiers = device.raw.get("all_identifiers")
    if isinstance(raw_identifiers, Sequence) and not isinstance(raw_identifiers, (str, bytes)):
        values.extend(value for value in raw_identifiers if isinstance(value, str))
    return {value.strip().casefold() for value in values if value.strip()}


def _matching_configured_devices(
    config: AgentConfig,
    selected: DiscoveredEndpoint,
) -> list[AgentDevice]:
    identifiers = _discovery_identifiers(selected)
    return [
        device
        for device in config.devices
        if device.stable_id and device.stable_id.strip().casefold() in identifiers
    ]


async def _pair_for_setup(
    agent: AppleTVAgent,
    device_id: str,
    protocol: str,
) -> None:
    try:
        session = await agent.adapter.pair_start(device_id, protocol)
    except Exception as exc:
        raise SetupError(
            f"{protocol}_pairing_failed",
            "Keep the Apple TV awake and on the same network, then request a new code "
            "and run setup again.",
        ) from exc
    label = "Companion remote control" if protocol == "companion" else "AirPlay metadata"
    pin = cast(
        str,
        typer.prompt(
            f"Code shown on the Apple TV for {label}",
            hide_input=True,
        ),
    )
    if not pin.strip():
        raise SetupError(
            "empty_pairing_code",
            "Run apple-tv-agent setup again and enter the code shown on the Apple TV.",
        )
    try:
        result = await agent.adapter.pair_finish(session.session_id, pin)
    except Exception as exc:
        raise SetupError(
            f"{protocol}_pairing_failed",
            "Keep the Apple TV awake and on the same network, then request a new code "
            "and run setup again.",
        ) from exc
    if result.state != "completed":
        raise SetupError(
            f"{protocol}_pairing_incomplete",
            "Keep the Apple TV awake and on the same network, then run setup again.",
        )


async def _start_runtime_for_configure(runtime: WDARuntime) -> None:
    """Finish the exact start attempt before propagating cancellation."""

    start_task = asyncio.create_task(asyncio.to_thread(runtime.start))
    try:
        status = await asyncio.shield(start_task)
    except BaseException:
        # A cancelled to_thread call continues running. Do not let cleanup race
        # ahead of it and miss a child that has not been published yet.
        with suppress(BaseException):
            await asyncio.shield(start_task)
        raise
    if status.state not in {WDARuntimeState.STARTING, WDARuntimeState.READY}:
        raise SetupError(
            "helper_start_failed",
            "The managed helper did not start. Run apple-tv-agent doctor and retry.",
        )


async def _stop_runtime_for_configure(runtime: WDARuntime) -> None:
    """Stop and verify the exact temporary helper before dropping its handle."""

    stop_task = asyncio.create_task(asyncio.to_thread(runtime.stop))
    try:
        status = await asyncio.shield(stop_task)
    except BaseException:
        try:
            status = await asyncio.shield(stop_task)
        except BaseException as stop_exc:
            raise SetupError(
                "helper_cleanup_failed",
                "The temporary helper could not be stopped; inspect its private runtime log.",
            ) from stop_exc
        if status.state is not WDARuntimeState.STOPPED:
            raise SetupError(
                "helper_cleanup_failed",
                "The temporary helper could not be stopped; inspect its private runtime log.",
            ) from None
        raise
    if status.state is not WDARuntimeState.STOPPED:
        raise SetupError(
            "helper_cleanup_failed",
            "The temporary helper could not be stopped; inspect its private runtime log.",
        )


@app.command()
def setup(
    no_pair: Annotated[
        bool,
        typer.Option(
            "--no-pair",
            help="Advanced/testing: save the selected device without starting pairing",
        ),
    ] = False,
    use_existing_pairing: Annotated[
        bool,
        typer.Option(
            "--use-existing-pairing",
            help="Skip Companion pairing because this Mac already has working credentials",
        ),
    ] = False,
    pair_airplay: Annotated[
        bool,
        typer.Option(
            "--pair-airplay",
            help="Also pair AirPlay for richer playback metadata when apps expose it",
        ),
    ] = False,
) -> None:
    """Discover, select, name, and pair an Apple TV through one guided flow."""
    if no_pair and (use_existing_pairing or pair_airplay):
        raise typer.BadParameter(
            "--no-pair cannot be combined with pairing options",
            param_hint="--no-pair",
        )

    async def operation() -> Any:
        config = load_agent_config()
        agent = AppleTVAgent(AgentConfig())
        try:
            try:
                discovered = await agent.adapter.discover()
            except Exception as exc:
                raise SetupError(
                    "discovery_failed",
                    "Wake the Apple TV, put this Mac on the same local network, "
                    "and run setup again.",
                ) from exc
            devices = [device for device in discovered if device.os == "tvOS"]
            if not devices:
                raise SetupError(
                    "no_tvos_devices",
                    "Wake the Apple TV, put this Mac on the same local network, "
                    "and run setup again.",
                )

            selected = _prompt_device(devices)
            matching_devices = _matching_configured_devices(config, selected)
            if len(matching_devices) > 1:
                raise SetupError(
                    "ambiguous_device_identifiers",
                    "Multiple configured devices match identifiers advertised by this Apple TV. "
                    "No changes were made; review the private agent configuration and retry.",
                )
            existing = matching_devices[0] if matching_devices else None
            friendly_name = _prompt_name(existing.name if existing else selected.name)
            unavailable = {
                device.key
                for device in config.devices
                if existing is None or device.key != existing.key
            }
            default_key = existing.key if existing else _available_key(friendly_name, unavailable)
            key = _prompt_key(default_key, unavailable)

            companion_state = "not_requested"
            airplay_state = "not_requested"
            if not no_pair:
                skip_companion = use_existing_pairing or typer.confirm(
                    "Use an existing Companion pairing already stored on this Mac?",
                    default=False,
                )
                if skip_companion:
                    companion_state = "existing_unverified"
                else:
                    if "companion" not in selected.protocols:
                        raise SetupError(
                            "companion_unavailable",
                            "Wake and update the Apple TV, then retry on the same local network.",
                        )
                    await _pair_for_setup(agent, selected.device_id, "companion")
                    companion_state = "paired"

                wants_airplay = pair_airplay or typer.confirm(
                    "Also pair AirPlay? This enables richer playback metadata when apps expose it.",
                    default=False,
                )
                if wants_airplay:
                    if "airplay" not in selected.protocols:
                        raise SetupError(
                            "airplay_unavailable",
                            "Finish direct setup without AirPlay, or retry after "
                            "AirPlay is available.",
                        )
                    await _pair_for_setup(agent, selected.device_id, "airplay")
                    airplay_state = "paired"

            configured = AgentDevice(
                key=key,
                name=friendly_name,
                stable_id=existing.stable_id if existing else selected.device_id,
                wda_endpoint=existing.wda_endpoint if existing else None,
                wda_identity=existing.wda_identity if existing else None,
                helper=existing.helper if existing else None,
            )
            updated = config.model_dump()
            updated["devices"] = [
                device
                for device in config.devices
                if existing is None or device.stable_id != existing.stable_id
            ] + [configured]
            save_agent_config(AgentConfig.model_validate(updated))

            visual_preserved = bool(configured.wda_endpoint)
            return {
                "ok": True,
                "device": configured.key,
                "direct_control": {
                    "stable_device_bound": True,
                    "companion_pairing": companion_state,
                    "airplay_pairing": airplay_state,
                },
                "visual_control": {
                    "route_preserved": visual_preserved,
                    "readiness_checked": False,
                    "note": (
                        "Existing visual-helper binding preserved; setup did not reverify "
                        "signing or readiness."
                        if visual_preserved
                        else "Optional visual control needs a separately signed and installed WDA "
                        "helper; setup did not install or sign one."
                    ),
                },
                "next": {
                    "check": "apple-tv-agent doctor",
                    "installed_mcp_command": "apple-tv-agent-mcp",
                    "codex": "codex mcp add apple-tv-agent -- apple-tv-agent-mcp",
                    "claude": "claude mcp add apple-tv-agent -- apple-tv-agent-mcp",
                },
            }
        finally:
            await agent.aclose()

    emit(run_interactive(operation()))


@app.command()
def discover() -> None:
    """Discover local devices. Private IDs/addresses are shown only for local setup."""

    async def operation() -> Any:
        agent = AppleTVAgent(AgentConfig())
        try:
            devices = await agent.adapter.discover()
            return [
                {
                    "name": d.name,
                    "stable_id": d.device_id,
                    "address": d.address,
                    "model": d.model,
                    "os": d.os,
                    "protocols": d.protocols,
                }
                for d in devices
                if d.os == "tvOS"
            ]
        finally:
            await agent.aclose()

    emit(run(operation()))


@app.command()
def configure(
    key: str = typer.Argument(help="A short device key, such as living-room"),
    name: str = typer.Option(..., help="Your label for the device"),
    endpoint: str | None = typer.Option(None, help="Confirmed device's WDA endpoint"),
    stable_id: str | None = typer.Option(None, help="Stable ID from discover; enables pyatv"),
    udid: str | None = typer.Option(None, help="Exact developer device ID for managed helper"),
    backend: Annotated[
        HelperBackend,
        typer.Option(help="Managed helper launcher: xcode or native"),
    ] = HelperBackend.XCODE,
    bundle_id: str | None = typer.Option(
        None,
        help="Exact installed WDA runner bundle ID; required for native",
    ),
    xctestrun: Annotated[Path | None, typer.Option(help="Signed helper test bundle")] = None,
    developer_dir: Annotated[Path | None, typer.Option(help="Xcode Contents/Developer")] = None,
) -> None:
    """Bind a device privately. A running helper's identity is pinned, never guessed by name."""
    helper_requested = bool(
        udid
        or bundle_id
        or xctestrun
        or developer_dir
        or backend is HelperBackend.NATIVE
    )
    helper: HelperConfig | None = None
    if helper_requested:
        if not udid:
            raise typer.BadParameter(
                "Managed helper requires an exact device ID",
                param_hint="--udid",
            )
        if backend is HelperBackend.NATIVE:
            if not bundle_id:
                raise typer.BadParameter(
                    "Native helper requires its exact installed bundle ID",
                    param_hint="--bundle-id",
                )
            if developer_dir is not None:
                raise typer.BadParameter(
                    "Native helper does not use an Xcode developer directory",
                    param_hint="--developer-dir",
                )
        else:
            if bundle_id is not None:
                raise typer.BadParameter(
                    "Bundle ID is used only with the native helper backend",
                    param_hint="--bundle-id",
                )
            if xctestrun is None or developer_dir is None:
                raise typer.BadParameter(
                    "Xcode helper requires xctestrun and developer-dir",
                    param_hint="--xctestrun/--developer-dir",
                )
        helper = HelperConfig(
            udid=udid,
            backend=backend.value,
            bundle_id=bundle_id,
            xctestrun_path=xctestrun,
            developer_dir=developer_dir,
        )

    async def operation() -> Any:
        runtime: WDARuntime | None = None
        client: WDAClient | None = None
        identity = None
        try:
            if endpoint:
                if helper:
                    runtime = WDARuntime(
                        WDARuntimeConfig(
                            udid=helper.udid,
                            backend=helper.backend,
                            bundle_id=helper.bundle_id,
                            xctestrun_path=helper.xctestrun_path,
                            developer_dir=helper.developer_dir,
                        )
                    )
                client = WDAClient(endpoint)
                try:
                    await client.status()
                except Exception:
                    if runtime is None:
                        raise
                    await _start_runtime_for_configure(runtime)
                    async with asyncio.timeout(40):
                        while True:
                            try:
                                await client.status()
                                ready = runtime.mark_ready()
                                if ready.state is not WDARuntimeState.READY:
                                    raise SetupError(
                                        "helper_readiness_failed",
                                        "The helper exited before readiness; run "
                                        "apple-tv-agent doctor.",
                                    )
                                break
                            except SetupError:
                                raise
                            except Exception:
                                await asyncio.sleep(0.3)
                identity = await client.device_identity()
        finally:
            try:
                if client:
                    await client.aclose()
            finally:
                if runtime:
                    await _stop_runtime_for_configure(runtime)

        device = AgentDevice(
            key=key,
            name=name,
            stable_id=stable_id,
            wda_endpoint=endpoint,
            wda_identity=identity,
            helper=helper,
        )
        config = load_agent_config()
        updated = config.model_dump()
        updated["devices"] = [d for d in config.devices if d.key != key] + [device]
        save_agent_config(AgentConfig.model_validate(updated))
        return {
            "ok": True,
            "device": key,
            "screen_identity_pinned": bool(identity),
            "direct_control_configured": bool(stable_id),
            "next": "apple-tv-agent pair " + key if stable_id else "apple-tv-agent serve",
        }

    emit(run(operation()))


@app.command()
def pair(key: str, protocol: str = "companion") -> None:
    """Pair normal remote control once; this is separate from developer provisioning."""

    async def operation() -> Any:
        agent = AppleTVAgent(load_agent_config())
        try:
            device = agent.device(key)
            if not device.stable_id:
                raise ValueError("Configure the stable discovery ID first")
            session = await agent.adapter.pair_start(device.stable_id, protocol)
            pin = cast(
                str,
                typer.prompt("Code shown on the Apple TV", hide_input=True),
            )
            result = await agent.adapter.pair_finish(session.session_id, pin)
            return {"ok": True, "device": key, "state": result.state}
        finally:
            await agent.aclose()

    emit(run_interactive(operation()))


@app.command()
def doctor() -> None:
    """Inspect setup, signed helper expiry, and prerequisites without controlling the TV."""
    config = load_agent_config()
    reports: list[dict[str, Any]] = []
    for device in config.devices:
        entry: dict[str, Any] = {
            "device": device.key,
            "screen_control_configured": bool(device.wda_endpoint),
            "paired_control_configured": bool(device.stable_id),
        }
        if device.helper:
            runtime = WDARuntime(
                WDARuntimeConfig(
                    udid=device.helper.udid,
                    backend=device.helper.backend,
                    bundle_id=device.helper.bundle_id,
                    xctestrun_path=device.helper.xctestrun_path,
                    developer_dir=device.helper.developer_dir,
                )
            )
            entry["helper"] = asdict(runtime.doctor())
        elif device.wda_endpoint:
            entry["helper"] = {"managed": False, "note": "External helper must already be running"}
        reports.append(entry)
    emit(
        {
            "devices": reports,
            "xcode_installations": len(discover_xcode_developer_dirs()),
            "cached_build_candidates": len(discover_cached_xctestruns()),
            "note": "Configured does not mean connected. Verify with the agent status tool.",
        }
    )


@app.command()
def preferences(
    country: str = "US", subscription: list[str] | None = None, profile: str | None = None
) -> None:
    """Set availability region and subscriptions; does not read or modify provider accounts."""
    config = load_agent_config()
    config.country = country.upper()
    if profile is not None:
        config.preferred_profile = profile.strip() or None
    if subscription is not None:
        config.subscriptions = subscription
    save_agent_config(config)
    emit({
        "country": config.country,
        "subscriptions": config.subscriptions,
        "preferred_profile": config.preferred_profile,
    })


@app.command()
def serve() -> None:
    """Run the six-tool stdio MCP server. Helper startup/shutdown is managed when configured."""
    from home_media.agent_mcp import main

    main()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
