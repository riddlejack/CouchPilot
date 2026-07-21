"""Direct Android TV Remote v2 adapter via androidtvremote2 (Sony BRAVIA first)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from home_media.config import android_cert_dir, ensure_private_dir, ensure_private_file
from home_media.errors import (
    AuthFailedError,
    AuthRequiredError,
    NetworkError,
    TimeoutError_,
    UnsupportedError,
)
from home_media.models import (
    Capability,
    CapabilitySemantics,
    DeviceKind,
    DeviceStatus,
    DiscoveredEndpoint,
    PairingSession,
    PowerState,
    SupportLevel,
)
from home_media.registry import RoomRegistry

logger = logging.getLogger(__name__)

_PROTOCOL = "android_tv_remote_v2"
_CLIENT_NAME = "home-media"
_CONNECT_TIMEOUT_S = 10.0
_PAIR_TIMEOUT_S = 30.0
_ZC_TIMEOUT_S = 3.0

# Best-effort HDMI input keys from RemoteKeyCode. Not guaranteed on all firmwares.
_HDMI_INPUT_KEYS: dict[str, str] = {
    "hdmi1": "KEYCODE_TV_INPUT_HDMI_1",
    "hdmi 1": "KEYCODE_TV_INPUT_HDMI_1",
    "hdmi_1": "KEYCODE_TV_INPUT_HDMI_1",
    "hdmi2": "KEYCODE_TV_INPUT_HDMI_2",
    "hdmi 2": "KEYCODE_TV_INPUT_HDMI_2",
    "hdmi_2": "KEYCODE_TV_INPUT_HDMI_2",
    "hdmi3": "KEYCODE_TV_INPUT_HDMI_3",
    "hdmi 3": "KEYCODE_TV_INPUT_HDMI_3",
    "hdmi_3": "KEYCODE_TV_INPUT_HDMI_3",
    "hdmi4": "KEYCODE_TV_INPUT_HDMI_4",
    "hdmi 4": "KEYCODE_TV_INPUT_HDMI_4",
    "hdmi_4": "KEYCODE_TV_INPUT_HDMI_4",
}


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


class AndroidTVAdapter:
    """Pair and control physical Android TVs over Remote protocol v2."""

    name = "android_tv"

    def __init__(self, registry: RoomRegistry) -> None:
        self._registry = registry
        self._clients: dict[str, AndroidTVRemote] = {}
        self._pairing: dict[str, tuple[PairingSession, AndroidTVRemote]] = {}

    async def discover(self) -> list[DiscoveredEndpoint]:
        """Best-effort discovery: registry endpoints plus optional zeroconf."""
        by_id: dict[str, DiscoveredEndpoint] = {}

        for device in self._registry.config.devices:
            if device.adapter != self.name or device.kind != DeviceKind.PHYSICAL_TV:
                continue
            existing = self._registry.endpoint_for(device.id)
            address = existing.address if existing else ""
            name = device.aliases[0] if device.aliases else device.id
            endpoint = DiscoveredEndpoint(
                device_id=device.id,
                kind=DeviceKind.PHYSICAL_TV,
                name=name,
                address=address,
                protocols=[_PROTOCOL, "google_cast"],
                model=device.model,
                raw={
                    "source": "configured",
                    "live_observed": False,
                    "paired": self._cert_present(device.id),
                },
            )
            # Configured-only rows are retained for pairing bootstrap but must not
            # be counted as live discovery evidence until zeroconf observes them.
            if address:
                by_id[device.id] = endpoint

        try:
            zc_hits = await asyncio.wait_for(
                self._zeroconf_scan(),
                timeout=_ZC_TIMEOUT_S + 2.0,
            )
        except Exception as exc:  # noqa: BLE001 — discovery must degrade gracefully
            logger.debug("Android TV zeroconf scan skipped: %s", exc)
            zc_hits = []

        for hit in zc_hits:
            matched = self._match_tv_device(hit["name"])
            device_id = matched or hit["name"]
            endpoint = DiscoveredEndpoint(
                device_id=device_id,
                kind=DeviceKind.PHYSICAL_TV,
                name=hit["name"],
                address=hit["address"],
                protocols=[_PROTOCOL],
                model=hit.get("model"),
                raw={
                    "source": "zeroconf",
                    "live_observed": True,
                    "service_name": hit.get("service_name"),
                    "paired": self._cert_present(device_id) if matched else False,
                },
            )
            if matched:
                by_id[matched] = endpoint
                self._registry.update_endpoint(endpoint)
            elif device_id not in by_id:
                by_id[device_id] = endpoint

        known_ids = {
            d.id for d in self._registry.config.devices if d.adapter == self.name
        }
        for endpoint in by_id.values():
            if endpoint.address and endpoint.device_id in known_ids:
                self._registry.update_endpoint(endpoint)

        # Only return live-observed endpoints as discovery evidence.
        return [
            ep
            for ep in by_id.values()
            if (ep.raw or {}).get("live_observed") is True
            or (ep.raw or {}).get("source") == "zeroconf"
        ]

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        self._require_device(device_id)
        return [
            Capability(
                name="power",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                evidence="androidtvremote2 key POWER/WAKEUP",
            ),
            Capability(
                name="power.physical_tv_state",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                evidence="direct androidtvremote2 is_on",
            ),
            Capability(
                name="tv.input",
                support=SupportLevel.DEGRADED,
                adapter=self.name,
                semantics=CapabilitySemantics.BEST_EFFORT,
                evidence="HDMI1-4 key codes only; input source not reliably verified",
            ),
            Capability(
                name="volume.set_absolute",
                support=SupportLevel.UNSUPPORTED,
                adapter=self.name,
                semantics=CapabilitySemantics.RELATIVE,
                evidence=(
                    "protocol exposes volume_info callbacks but no reliable absolute set API"
                ),
            ),
        ]

    async def get_status(self, device_id: str) -> DeviceStatus:
        self._require_device(device_id)
        address = self._address_for(device_id)
        if not self._cert_present(device_id):
            raise AuthRequiredError(device_id, protocol=_PROTOCOL)

        remote = await self._ensure_connected(device_id)
        power = PowerState.UNKNOWN
        if remote.is_on is True:
            power = PowerState.ON
        elif remote.is_on is False:
            power = PowerState.OFF

        return DeviceStatus(
            device_id=device_id,
            kind=DeviceKind.PHYSICAL_TV,
            name=self._display_name(device_id),
            address=address,
            power=power,
            current_app=remote.current_app,
            paired=True,
            available=True,
            evidence=["androidtvremote2_status"],
        )

    async def pair_start(self, device_id: str, protocol: str = _PROTOCOL) -> PairingSession:
        self._require_device(device_id)
        if protocol and protocol not in {_PROTOCOL, "android_tv"}:
            raise UnsupportedError("pairing", reason=f"unsupported protocol '{protocol}'")

        address = self._address_for(device_id)
        if not address:
            raise NetworkError(
                f"No address for Android TV {device_id}; run discover first",
                retryable=True,
            )

        certfile, keyfile = self._cert_paths(device_id)
        ensure_private_dir(certfile.parent)
        self._disconnect_client(device_id)

        remote = AndroidTVRemote(
            client_name=_CLIENT_NAME,
            certfile=str(certfile),
            keyfile=str(keyfile),
            host=address,
        )
        try:
            await asyncio.wait_for(
                remote.async_generate_cert_if_missing(),
                timeout=_PAIR_TIMEOUT_S,
            )
            ensure_private_file(certfile)
            ensure_private_file(keyfile)
            await asyncio.wait_for(remote.async_start_pairing(), timeout=_PAIR_TIMEOUT_S)
        except TimeoutError as exc:
            remote.disconnect()
            raise TimeoutError_(f"Android TV pairing start timed out for {device_id}") from exc
        except CannotConnect as exc:
            remote.disconnect()
            raise NetworkError(
                f"Cannot connect to Android TV {device_id} at {address} for pairing",
                retryable=True,
            ) from exc
        except ConnectionClosed as exc:
            remote.disconnect()
            raise NetworkError(
                f"Android TV pairing connection closed for {device_id}",
                retryable=True,
            ) from exc

        device = self._registry.device(device_id)
        session = PairingSession(
            room_key=device.room_key,
            device_id=device_id,
            protocol=_PROTOCOL,
            message="Enter the pairing code shown on the TV",
        )
        self._pairing[session.session_id] = (session, remote)
        # Never log the PIN (none yet).
        logger.info("Android TV pairing started for device_id=%s", device_id)
        return session

    async def pair_finish(self, session_id: str, pin: str) -> PairingSession:
        entry = self._pairing.get(session_id)
        if entry is None:
            raise AuthFailedError("Unknown or expired pairing session")
        session, remote = entry

        # Intentionally do not log `pin`.
        try:
            await asyncio.wait_for(remote.async_finish_pairing(pin), timeout=_PAIR_TIMEOUT_S)
        except TimeoutError:
            session.state = "failed"
            session.message = "Pairing timed out"
            remote.disconnect()
            self._pairing.pop(session_id, None)
            raise TimeoutError_(
                f"Android TV pairing finish timed out for {session.device_id}"
            ) from None
        except InvalidAuth:
            session.state = "failed"
            session.message = "Invalid pairing code"
            remote.disconnect()
            self._pairing.pop(session_id, None)
            raise AuthFailedError("Invalid Android TV pairing code") from None
        except ConnectionClosed:
            session.state = "failed"
            session.message = "Pairing connection closed"
            remote.disconnect()
            self._pairing.pop(session_id, None)
            raise AuthFailedError("Android TV pairing connection closed") from None
        except CannotConnect as exc:
            session.state = "failed"
            session.message = "Cannot connect"
            remote.disconnect()
            self._pairing.pop(session_id, None)
            raise NetworkError(
                f"Cannot connect finishing pairing for {session.device_id}",
                retryable=True,
            ) from exc

        session.state = "completed"
        session.message = "Paired"
        remote.disconnect()
        self._pairing.pop(session_id, None)
        certfile, keyfile = self._cert_paths(session.device_id)
        ensure_private_file(certfile)
        ensure_private_file(keyfile)
        logger.info("Android TV pairing completed for device_id=%s", session.device_id)
        return session

    async def set_power(self, device_id: str, state: PowerState) -> DeviceStatus:
        if state not in {PowerState.ON, PowerState.OFF}:
            raise UnsupportedError("power", reason=f"unsupported power state '{state}'")

        remote = await self._ensure_connected(device_id)
        current = remote.is_on
        try:
            if state == PowerState.ON:
                if current is False:
                    remote.send_key_command("KEYCODE_WAKEUP")
                    await asyncio.sleep(0.2)
                    if remote.is_on is False:
                        remote.send_key_command("KEYCODE_POWER")
            elif current is not False:
                remote.send_key_command("KEYCODE_SLEEP")
                await asyncio.sleep(0.2)
                if remote.is_on is True:
                    remote.send_key_command("KEYCODE_POWER")
        except ConnectionClosed as exc:
            self._disconnect_client(device_id)
            raise NetworkError(
                f"Android TV connection closed while setting power on {device_id}",
                retryable=True,
            ) from exc

        await asyncio.sleep(0.4)
        return await self.get_status(device_id)

    async def set_input(self, device_id: str, source: str) -> DeviceStatus:
        key = _HDMI_INPUT_KEYS.get(_norm(source))
        if key is None:
            raise UnsupportedError(
                "tv.input",
                reason=(
                    "Only HDMI1-HDMI4 key codes are attempted; "
                    f"source '{source}' is not reliably mappable via androidtvremote2"
                ),
            )

        remote = await self._ensure_connected(device_id)
        try:
            if remote.is_on is False:
                remote.send_key_command("KEYCODE_WAKEUP")
                await asyncio.sleep(0.3)
            remote.send_key_command(key)
        except ConnectionClosed as exc:
            self._disconnect_client(device_id)
            raise NetworkError(
                f"Android TV connection closed while setting input on {device_id}",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise UnsupportedError("tv.input", reason=str(exc)) from exc

        status = await self.get_status(device_id)
        # Do NOT copy the requested source into observed state — protocol cannot read input.
        status.input_source = None
        status.warnings.append(
            "input switch sent via HDMI key code; current input is not verified by the protocol"
        )
        status.evidence.append("androidtvremote2_hdmi_key_best_effort")
        return status

    async def get_volume(self, device_id: str) -> dict[str, Any]:
        self._require_device(device_id)
        raise UnsupportedError(
            "volume.set_absolute",
            reason="androidtvremote2 does not expose a reliable absolute volume set API",
        )

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]:
        self._require_device(device_id)
        raise UnsupportedError(
            "volume.set_absolute",
            reason="androidtvremote2 does not expose a reliable absolute volume set API",
        )

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]:
        """Relative volume via key presses (not absolute)."""
        remote = await self._ensure_connected(device_id)
        steps = abs(int(delta))
        key = "KEYCODE_VOLUME_UP" if delta > 0 else "KEYCODE_VOLUME_DOWN"
        try:
            for _ in range(min(steps, 50)):
                remote.send_key_command(key)
                await asyncio.sleep(0.05)
        except ConnectionClosed as exc:
            self._disconnect_client(device_id)
            raise NetworkError(
                f"Android TV connection closed while changing volume on {device_id}",
                retryable=True,
            ) from exc
        return {"semantics": "relative", "delta": delta, "verified": False}

    def _cert_paths(self, device_id: str) -> tuple[Path, Path]:
        base = android_cert_dir()
        return base / f"{device_id}.cert.pem", base / f"{device_id}.key.pem"

    def _cert_present(self, device_id: str) -> bool:
        certfile, keyfile = self._cert_paths(device_id)
        return certfile.is_file() and keyfile.is_file()

    def _require_device(self, device_id: str) -> None:
        device = self._registry.device(device_id)
        if device.adapter != self.name:
            raise NetworkError(
                f"Device {device_id} is not an android_tv adapter target",
                retryable=False,
            )

    def _display_name(self, device_id: str) -> str:
        device = self._registry.device(device_id)
        if device.aliases:
            return device.aliases[0]
        return device_id

    def _address_for(self, device_id: str) -> str:
        endpoint = self._registry.endpoint_for(device_id)
        if endpoint and endpoint.address:
            return endpoint.address
        return ""

    def _match_tv_device(self, advertised_name: str) -> str | None:
        needle = _norm(advertised_name)
        matches: list[str] = []
        for device in self._registry.config.devices:
            if device.adapter != self.name:
                continue
            room = self._registry.room(device.room_key)
            candidates = {
                _norm(room.display_name),
                *(_norm(a) for a in room.aliases),
                *(_norm(a) for a in device.aliases),
            }
            if needle in candidates:
                matches.append(device.id)
        if len(matches) == 1:
            return matches[0]
        return None

    async def _ensure_connected(self, device_id: str) -> AndroidTVRemote:
        if not self._cert_present(device_id):
            raise AuthRequiredError(device_id, protocol=_PROTOCOL)

        address = self._address_for(device_id)
        if not address:
            # Fresh process: rediscover rather than requiring a prior CLI discover.
            await self.discover()
            address = self._address_for(device_id)
        if not address:
            raise NetworkError(
                f"No address for Android TV {device_id} after discovery",
                retryable=True,
            )

        remote = self._clients.get(device_id)
        if remote is not None and remote.host == address and remote.is_on is not None:
            return remote

        self._disconnect_client(device_id)
        certfile, keyfile = self._cert_paths(device_id)
        remote = AndroidTVRemote(
            client_name=_CLIENT_NAME,
            certfile=str(certfile),
            keyfile=str(keyfile),
            host=address,
        )
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=_CONNECT_TIMEOUT_S)
        except TimeoutError as exc:
            remote.disconnect()
            raise TimeoutError_(f"Android TV connect timed out for {device_id}") from exc
        except InvalidAuth as exc:
            remote.disconnect()
            raise AuthRequiredError(device_id, protocol=_PROTOCOL) from exc
        except (CannotConnect, ConnectionClosed) as exc:
            remote.disconnect()
            raise NetworkError(
                f"Cannot connect to Android TV {device_id} at {address}",
                retryable=True,
            ) from exc

        self._clients[device_id] = remote
        return remote

    def _disconnect_client(self, device_id: str) -> None:
        remote = self._clients.pop(device_id, None)
        if remote is not None:
            with contextlib.suppress(Exception):
                remote.disconnect()

    async def _zeroconf_scan(self) -> list[dict[str, Any]]:
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            return []

        hits: list[dict[str, Any]] = []
        zc = AsyncZeroconf()
        pending: set[asyncio.Task[None]] = set()

        async def _resolve(zeroconf: Any, service_type: str, name: str) -> None:
            info = AsyncServiceInfo(service_type, name)
            await info.async_request(zeroconf, 2000)
            addresses = list(info.parsed_scoped_addresses()) if info else []
            if not addresses:
                return
            friendly = name
            suffix = "._androidtvremote2._tcp.local."
            if friendly.endswith(suffix):
                friendly = friendly[: -len(suffix)]
            hits.append(
                {
                    "name": friendly,
                    "address": addresses[0],
                    "service_name": name,
                    "model": None,
                }
            )

        def _on_service_state_change(
            zeroconf: Any,
            service_type: str,
            name: str,
            state_change: Any,
        ) -> None:
            if state_change is not ServiceStateChange.Added:
                return
            task = asyncio.create_task(_resolve(zeroconf, service_type, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

        browser = AsyncServiceBrowser(
            zc.zeroconf,
            ["_androidtvremote2._tcp.local."],
            handlers=[_on_service_state_change],
        )
        try:
            await asyncio.sleep(_ZC_TIMEOUT_S)
            if pending:
                await asyncio.wait(pending, timeout=2.0)
        finally:
            await browser.async_cancel()
            await zc.async_close()
        return hits

    async def aclose(self) -> None:
        for device_id in list(self._clients):
            self._disconnect_client(device_id)
        for session_id, (_session, remote) in list(self._pairing.items()):
            with contextlib.suppress(Exception):
                remote.disconnect()
            self._pairing.pop(session_id, None)
