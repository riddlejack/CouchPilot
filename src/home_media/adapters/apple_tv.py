"""Apple TV adapter backed by pyatv 0.18 FileStorage credentials."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from urllib.parse import urlparse

import pyatv
from pyatv.const import (
    FeatureName,
    FeatureState,
    OperatingSystem,
    Protocol,
)
from pyatv.const import (
    PowerState as PyPowerState,
)
from pyatv.exceptions import (
    AuthenticationError,
    ConnectionFailedError,
    InvalidCredentialsError,
    NoCredentialsError,
    OperationTimeoutError,
    PairingError,
)
from pyatv.exceptions import (
    NotSupportedError as PyNotSupportedError,
)
from pyatv.interface import AppleTV, BaseConfig, PairingHandler
from pyatv.storage.file_storage import FileStorage

from home_media.config import ensure_private_file, pyatv_storage_path
from home_media.errors import (
    AuthFailedError,
    AuthRequiredError,
    DeviceRejectedError,
    NetworkError,
    StaleEndpointError,
    TimeoutError_,
    UnsupportedError,
)
from home_media.models import (
    AppInfo,
    Capability,
    CapabilitySemantics,
    DeviceKind,
    DeviceStatus,
    DiscoveredEndpoint,
    NowPlaying,
    PairingSession,
    PowerState,
    SupportLevel,
)
from home_media.registry import RoomRegistry

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

_CONNECT_TIMEOUT_S = 15.0
_COMMAND_TIMEOUT_S = 12.0
_PAIR_TIMEOUT_S = 30.0

_PROTOCOL_BY_NAME: dict[str, Protocol] = {
    "companion": Protocol.Companion,
    "airplay": Protocol.AirPlay,
    "raop": Protocol.RAOP,
}

_OS_LABEL: dict[OperatingSystem, str] = {
    OperatingSystem.TvOS: "tvOS",
    OperatingSystem.MacOS: "MacOS",
    OperatingSystem.AirPortOS: "AirPortOS",
    OperatingSystem.Legacy: "ATV SW",
    OperatingSystem.Unknown: "Unknown OS",
}

_SONOS_MODEL_TOKENS = ("beam", "arc", "port", "amp", "one", "five", "sub", "roam", "move")

_TRANSPORT_ACTIONS: dict[str, str] = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "next": "next",
    "previous": "previous",
}

_KEY_METHODS: dict[str, str] = {
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "select": "select",
    "menu": "menu",
    "home": "home",
    "home_hold": "home_hold",
    "top_menu": "top_menu",
    "play_pause": "play_pause",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "skip_forward": "skip_forward",
    "skip_backward": "skip_backward",
    "channel_up": "channel_up",
    "channel_down": "channel_down",
    "control_center": "control_center",
    "guide": "guide",
    "screensaver": "screensaver",
}


@dataclass
class _PairingState:
    session: PairingSession
    handler: PairingHandler
    protocol: Protocol


class AppleTVAdapter:
    """Live Apple TV control via pyatv Companion + AirPlay."""

    name = "apple_tv"

    def __init__(self, registry: RoomRegistry, *, scan_timeout: float = 5.0) -> None:
        self.registry = registry
        self.scan_timeout = scan_timeout
        self._storage: FileStorage | None = None
        self._storage_lock = asyncio.Lock()
        self._pairings: dict[str, _PairingState] = {}
        self._connection_manager: Any | None = None
        self._manager_lock = asyncio.Lock()

    async def discover(self) -> list[DiscoveredEndpoint]:
        storage = await self._ensure_storage()
        loop = asyncio.get_running_loop()
        try:
            configs = await asyncio.wait_for(
                pyatv.scan(loop, timeout=self._scan_timeout_int(), storage=storage),
                timeout=self.scan_timeout + 2.0,
            )
        except TimeoutError as exc:
            raise TimeoutError_("Apple TV discovery timed out") from exc
        except Exception as exc:  # noqa: BLE001
            raise self._map_network(exc, "Apple TV discovery failed") from exc

        endpoints: list[DiscoveredEndpoint] = []
        for conf in configs:
            endpoint = self._endpoint_from_config(conf)
            self.registry.update_endpoint(endpoint)
            endpoints.append(endpoint)
        return endpoints

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        conf = await self._resolve_config(device_id)
        if not self._has_usable_credentials(conf):
            return self._static_capabilities(paired=False)

        async def _probe(atv: AppleTV, _conf: BaseConfig) -> list[Capability]:
            return self._capabilities_from_features(atv)

        try:
            return await self._with_connection(conf, device_id, _probe)
        except AuthRequiredError:
            return self._static_capabilities(paired=False)

    async def get_status(self, device_id: str) -> DeviceStatus:
        conf = await self._resolve_config(device_id)
        if not self._has_usable_credentials(conf):
            raise AuthRequiredError(device_id, protocol="companion")

        async def _status(atv: AppleTV, live: BaseConfig) -> DeviceStatus:
            power = self._map_power(atv.power.power_state)
            current_app: str | None = None
            now_playing: NowPlaying | None = None
            evidence = ["pyatv_status"]

            try:
                app = atv.metadata.app
                if app is not None:
                    current_app = app.identifier
            except Exception:  # noqa: BLE001
                evidence.append("app_unavailable")

            try:
                playing = await asyncio.wait_for(
                    atv.metadata.playing(),
                    timeout=_COMMAND_TIMEOUT_S,
                )
                now_playing = NowPlaying(
                    title=playing.title,
                    artist=playing.artist,
                    album=playing.album,
                    app_id=current_app,
                    device_state=playing.device_state.name.lower()
                    if playing.device_state
                    else None,
                    position_s=float(playing.position)
                    if playing.position is not None
                    else None,
                    total_time_s=float(playing.total_time)
                    if playing.total_time is not None
                    else None,
                )
            except Exception:  # noqa: BLE001
                evidence.append("now_playing_unavailable")

            return DeviceStatus(
                device_id=device_id,
                kind=DeviceKind.APPLE_TV,
                name=live.name,
                address=str(live.address),
                power=power,
                current_app=current_app,
                now_playing=now_playing,
                paired=True,
                evidence=evidence,
            )

        return await self._with_connection(conf, device_id, _status)

    async def pair_start(self, device_id: str, protocol: str) -> PairingSession:
        proto = self._parse_protocol(protocol)
        conf = await self._resolve_config(device_id)
        storage = await self._ensure_storage()
        loop = asyncio.get_running_loop()

        try:
            handler = await asyncio.wait_for(
                pyatv.pair(conf, proto, loop, storage=storage),
                timeout=_PAIR_TIMEOUT_S,
            )
            await asyncio.wait_for(handler.begin(), timeout=_PAIR_TIMEOUT_S)
        except TimeoutError as exc:
            raise TimeoutError_("Pairing start timed out") from exc
        except PairingError as exc:
            raise AuthFailedError("Pairing could not be started") from exc
        except Exception as exc:  # noqa: BLE001
            raise self._map_network(exc, "Pairing start failed") from exc

        session = PairingSession(
            room_key="",
            device_id=device_id,
            protocol=protocol.lower(),
            message="Enter PIN shown on Apple TV",
        )
        self._pairings[session.session_id] = _PairingState(
            session=session,
            handler=handler,
            protocol=proto,
        )
        return session

    async def pair_finish(self, session_id: str, pin: str) -> PairingSession:
        state = self._pairings.get(session_id)
        if state is None:
            raise NetworkError(f"Unknown pairing session {session_id}", retryable=False)

        session = state.session
        handler = state.handler
        # PIN is passed through only; never stored on session or logged here.
        try:
            handler.pin(pin)
            await asyncio.wait_for(handler.finish(), timeout=_PAIR_TIMEOUT_S)
            if handler.has_paired:
                storage = await self._ensure_storage()
                await storage.save()
                ensure_private_file(pyatv_storage_path())
                session.state = "completed"
                session.message = "Paired"
            else:
                session.state = "failed"
                session.message = "Pairing did not complete"
        except TimeoutError:
            session.state = "failed"
            session.message = "Pairing timed out"
            raise TimeoutError_("Pairing finish timed out") from None
        except PairingError as exc:
            session.state = "failed"
            session.message = "Invalid PIN or pairing rejected"
            raise AuthFailedError("Pairing failed") from exc
        except Exception as exc:  # noqa: BLE001
            session.state = "failed"
            session.message = "Pairing failed"
            raise self._map_network(exc, "Pairing finish failed") from exc
        finally:
            try:
                await handler.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("pairing handler close failed", exc_info=True)
            self._pairings.pop(session_id, None)

        return session

    async def set_power(self, device_id: str, state: PowerState) -> DeviceStatus:
        conf = await self._resolve_config(device_id)

        async def _power(atv: AppleTV, _live: BaseConfig) -> None:
            if state == PowerState.ON:
                await atv.power.turn_on()
            elif state in {PowerState.OFF, PowerState.STANDBY}:
                await atv.power.turn_off()
            else:
                raise UnsupportedError("power", reason=f"Unsupported power state {state}")

        await self._with_connection(conf, device_id, _power)
        return await self.get_status(device_id)

    async def list_apps(self, device_id: str) -> list[AppInfo]:
        conf = await self._resolve_config(device_id)

        async def _apps(atv: AppleTV, _live: BaseConfig) -> list[AppInfo]:
            if not atv.features.in_state(FeatureState.Available, FeatureName.AppList):
                raise UnsupportedError("apps.list", reason="AppList unavailable")
            apps = await atv.apps.app_list()
            return [AppInfo(name=a.name or a.identifier, identifier=a.identifier) for a in apps]

        return await self._with_connection(conf, device_id, _apps)

    async def open_app(self, device_id: str, app_id: str) -> DeviceStatus:
        conf = await self._resolve_config(device_id)

        async def _launch(atv: AppleTV, _live: BaseConfig) -> None:
            if not atv.features.in_state(FeatureState.Available, FeatureName.LaunchApp):
                raise UnsupportedError("apps.launch", reason="LaunchApp unavailable")
            await atv.apps.launch_app(app_id)

        await self._with_connection(conf, device_id, _launch)
        return await self.get_status(device_id)

    async def open_url(self, device_id: str, url: str) -> DeviceStatus:
        from home_media.content.urls import validate_content_url

        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme == "airplay":
            raise DeviceRejectedError(
                "Raw AirPlay URL streaming is rejected; use Companion deep links"
            )
        # Provider-allowlisted http(s) plus Netflix-only nflx:// title links.
        validated = validate_content_url(url)
        launch_url = validated.url

        conf = await self._resolve_config(device_id)

        async def _open(atv: AppleTV, _live: BaseConfig) -> None:
            if not atv.features.in_state(FeatureState.Available, FeatureName.LaunchApp):
                raise UnsupportedError("content.deep_link", reason="LaunchApp unavailable")
            await atv.apps.launch_app(launch_url)

        await self._with_connection(conf, device_id, _open)
        return await self.get_status(device_id)

    async def control_transport(self, device_id: str, action: str) -> DeviceStatus:
        method_name = _TRANSPORT_ACTIONS.get(action.lower())
        if method_name is None:
            raise UnsupportedError("transport", reason=f"Unknown action '{action}'")

        feature = getattr(FeatureName, method_name.capitalize(), None)
        conf = await self._resolve_config(device_id)

        async def _transport(atv: AppleTV, _live: BaseConfig) -> None:
            if feature is not None and not atv.features.in_state(
                FeatureState.Available, feature
            ):
                raise UnsupportedError("transport", reason=f"{action} unavailable")
            method = getattr(atv.remote_control, method_name)
            await method()

        await self._with_connection(conf, device_id, _transport)
        return await self.get_status(device_id)

    async def press_key(self, device_id: str, key: str) -> None:
        method_name = _KEY_METHODS.get(key.lower())
        if method_name is None:
            raise UnsupportedError("remote.key", reason=f"Unknown key '{key}'")

        conf = await self._resolve_config(device_id)

        async def _key(atv: AppleTV, _live: BaseConfig) -> None:
            method = getattr(atv.remote_control, method_name, None)
            if method is None:
                raise UnsupportedError("remote.key", reason=f"Key '{key}' not supported")
            result = method()
            if asyncio.iscoroutine(result):
                await result

        await self._with_connection(conf, device_id, _key)

    async def enter_text(self, device_id: str, text: str) -> None:
        conf = await self._resolve_config(device_id)

        async def _text(atv: AppleTV, _live: BaseConfig) -> None:
            if atv.features.in_state(FeatureState.Available, FeatureName.TextSet):
                await atv.keyboard.text_set(text)
                return
            if atv.features.in_state(FeatureState.Available, FeatureName.TextAppend):
                await atv.keyboard.text_append(text)
                return
            raise UnsupportedError("keyboard.text", reason="Text entry unavailable")

        await self._with_connection(conf, device_id, _text)

    async def keyboard_text_get(self, device_id: str) -> str | None:
        conf = await self._resolve_config(device_id)

        async def _get(atv: AppleTV, _live: BaseConfig) -> str | None:
            if not atv.features.in_state(FeatureState.Available, FeatureName.TextGet):
                raise UnsupportedError("keyboard.text_get", reason="TextGet unavailable")
            return await atv.keyboard.text_get()

        return await self._with_connection(conf, device_id, _get)

    async def keyboard_text_clear(self, device_id: str) -> None:
        conf = await self._resolve_config(device_id)

        async def _clear(atv: AppleTV, _live: BaseConfig) -> None:
            if atv.features.in_state(FeatureState.Available, FeatureName.TextClear):
                await atv.keyboard.text_clear()
                return
            if atv.features.in_state(FeatureState.Available, FeatureName.TextSet):
                await atv.keyboard.text_set("")
                return
            raise UnsupportedError("keyboard.text_clear", reason="TextClear unavailable")

        await self._with_connection(conf, device_id, _clear)

    async def keyboard_focus_state(self, device_id: str) -> str:
        from pyatv.const import KeyboardFocusState

        conf = await self._resolve_config(device_id)

        async def _focus(atv: AppleTV, _live: BaseConfig) -> str:
            state = atv.keyboard.text_focus_state
            if state == KeyboardFocusState.Focused:
                return "focused"
            if state == KeyboardFocusState.Unfocused:
                return "unfocused"
            return "unknown"

        return await self._with_connection(conf, device_id, _focus)

    async def wait_keyboard_focused(
        self,
        device_id: str,
        *,
        timeout_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> str:
        """Poll until keyboard focus is Focused or timeout."""
        from pyatv.const import KeyboardFocusState

        conf = await self._resolve_config(device_id)
        deadline = asyncio.get_running_loop().time() + timeout_s

        async def _wait(atv: AppleTV, _live: BaseConfig) -> str:
            while True:
                state = atv.keyboard.text_focus_state
                if state == KeyboardFocusState.Focused:
                    return "focused"
                if asyncio.get_running_loop().time() >= deadline:
                    if state == KeyboardFocusState.Unfocused:
                        return "unfocused"
                    return "unknown"
                await asyncio.sleep(poll_s)

        return await self._with_connection(conf, device_id, _wait)

    async def aclose(self) -> None:
        """Close pairing handlers and any cached resources."""
        for session_id in list(self._pairings):
            state = self._pairings.pop(session_id, None)
            if state is None:
                continue
            try:
                await state.handler.close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("pairing handler close failed", exc_info=True)
        if self._connection_manager is not None:
            await self._connection_manager.aclose()
            self._connection_manager = None

    async def get_volume(self, device_id: str) -> dict[str, Any]:
        conf = await self._resolve_config(device_id)

        async def _get(atv: AppleTV, _live: BaseConfig) -> dict[str, Any]:
            if not atv.features.in_state(FeatureState.Available, FeatureName.Volume):
                raise UnsupportedError(
                    "volume.set_absolute",
                    reason="Absolute volume unavailable (CEC relative only)",
                )
            level = atv.audio.volume
            return {
                "level": int(round(level)),
                "semantics": CapabilitySemantics.ABSOLUTE.value,
                "verified": True,
            }

        return await self._with_connection(conf, device_id, _get)

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]:
        if not 0 <= level <= 100:
            raise DeviceRejectedError("Volume level must be 0-100")
        conf = await self._resolve_config(device_id)

        async def _set(atv: AppleTV, _live: BaseConfig) -> dict[str, Any]:
            if not atv.features.in_state(FeatureState.Available, FeatureName.SetVolume):
                raise UnsupportedError(
                    "volume.set_absolute",
                    reason="Absolute volume unavailable (CEC relative only)",
                )
            await atv.audio.set_volume(float(level))
            return {
                "level": level,
                "semantics": CapabilitySemantics.ABSOLUTE.value,
                "verified": False,
            }

        return await self._with_connection(conf, device_id, _set)

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]:
        if delta == 0:
            return {"semantics": CapabilitySemantics.RELATIVE.value, "delta": 0, "verified": False}

        conf = await self._resolve_config(device_id)

        async def _change(atv: AppleTV, _live: BaseConfig) -> dict[str, Any]:
            up = FeatureName.VolumeUp
            down = FeatureName.VolumeDown
            steps = abs(delta)
            # CEC / relative path: one feature call ≈ one remote step, not 1%.
            if delta > 0:
                if not atv.features.in_state(FeatureState.Available, up):
                    raise UnsupportedError(
                        "volume.relative",
                        reason="Relative volume up unavailable",
                    )
                for _ in range(min(steps, 20)):
                    await atv.audio.volume_up()
            else:
                if not atv.features.in_state(FeatureState.Available, down):
                    raise UnsupportedError(
                        "volume.relative",
                        reason="Relative volume down unavailable",
                    )
                for _ in range(min(steps, 20)):
                    await atv.audio.volume_down()
            return {
                "semantics": CapabilitySemantics.RELATIVE.value,
                "delta": delta,
                "verified": False,
            }

        return await self._with_connection(conf, device_id, _change)

    async def set_input(self, device_id: str, source: str) -> DeviceStatus:
        raise UnsupportedError("tv.input", reason="Apple TV has no physical input switch")

    # --- internals ---------------------------------------------------------

    async def _ensure_storage(self) -> FileStorage:
        async with self._storage_lock:
            if self._storage is not None:
                return self._storage
            path = ensure_private_file(pyatv_storage_path())
            loop = asyncio.get_running_loop()
            storage = FileStorage(str(path), loop)
            await storage.load()
            ensure_private_file(path)
            self._storage = storage
            return storage

    def _scan_timeout_int(self) -> int:
        return max(1, int(round(self.scan_timeout)))

    async def _ensure_connection_manager(self) -> Any:
        async with self._manager_lock:
            if self._connection_manager is not None:
                return self._connection_manager
            from home_media.connection import AppleTVConnectionManager

            storage = await self._ensure_storage()
            self._connection_manager = AppleTVConnectionManager(
                storage=storage,
                scan_timeout=self.scan_timeout,
            )
            return self._connection_manager

    async def _resolve_config(self, device_id: str) -> BaseConfig:
        manager = await self._ensure_connection_manager()
        try:
            conf = await manager.resolve_config(device_id)
        except StaleEndpointError:
            cached = self.registry.endpoint_for(device_id)
            address = cached.address if cached else "unknown"
            raise StaleEndpointError(device_id, address) from None
        self.registry.update_endpoint(self._endpoint_from_config(conf))
        return cast(BaseConfig, conf)

    async def _with_connection(
        self,
        conf: BaseConfig,
        device_id: str,
        op: Callable[[AppleTV, BaseConfig], Awaitable[T]],
    ) -> T:
        if not self._has_usable_credentials(conf):
            raise AuthRequiredError(device_id, protocol="companion")

        manager = await self._ensure_connection_manager()

        async def _wrapped(atv: AppleTV, live: BaseConfig) -> T:
            try:
                return await asyncio.wait_for(op(atv, live), timeout=_COMMAND_TIMEOUT_S)
            except TimeoutError as exc:
                raise TimeoutError_(f"Command timed out for {device_id}") from exc
            except (
                AuthRequiredError,
                AuthFailedError,
                UnsupportedError,
                DeviceRejectedError,
                TimeoutError_,
                NetworkError,
                StaleEndpointError,
            ):
                raise
            except PyNotSupportedError as exc:
                raise UnsupportedError("apple_tv", reason="Feature not supported") from exc
            except (AuthenticationError, NoCredentialsError, InvalidCredentialsError) as exc:
                raise AuthRequiredError(device_id, protocol="companion") from exc
            except Exception as exc:  # noqa: BLE001
                raise self._map_network(exc, f"Command failed for {device_id}") from exc

        try:
            return cast(
                T,
                await manager.with_connection(
                    device_id,
                    _wrapped,
                    require_credentials=self._has_usable_credentials,
                ),
            )
        except AuthRequiredError:
            raise
        except NetworkError as exc:
            # Preserve typed mapping when possible
            raise exc

    async def _close_atv(self, atv: AppleTV) -> None:
        try:
            tasks = atv.close()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Apple TV close failed", exc_info=True)

    def _has_usable_credentials(self, conf: BaseConfig) -> bool:
        """True when Companion or AirPlay credentials are present in storage/config."""
        for protocol in (Protocol.Companion, Protocol.AirPlay):
            service = conf.get_service(protocol)
            if service is not None and service.credentials:
                return True
        return False

    @staticmethod
    def _stable_device_id(conf: BaseConfig) -> str:
        """Prefer UUID identifiers over MAC/hardware ids (IPs stay ephemeral)."""
        identifiers = list(conf.all_identifiers)
        for ident in identifiers:
            if len(ident) == 36 and ident.count("-") == 4:
                return ident
        if conf.identifier:
            return conf.identifier
        if identifiers:
            return identifiers[0]
        return conf.name

    def _endpoint_from_config(self, conf: BaseConfig) -> DiscoveredEndpoint:
        info = conf.device_info
        model = info.model_str
        os_name = _OS_LABEL.get(info.operating_system, "Unknown OS")
        kind = self._classify_kind(
            model=model,
            os_name=os_name,
            operating_system=info.operating_system,
        )
        protocols = [svc.protocol.name.lower() for svc in conf.services if svc.enabled]
        device_id = self._stable_device_id(conf)
        return DiscoveredEndpoint(
            device_id=device_id,
            kind=kind,
            name=conf.name,
            address=str(conf.address),
            protocols=protocols,
            model=model,
            os=os_name,
            os_version=info.version,
            raw={
                "all_identifiers": list(conf.all_identifiers),
                "raw_model": info.raw_model,
                "pyatv_identifier": conf.identifier,
            },
        )

    def _classify_kind(
        self,
        *,
        model: str,
        os_name: str,
        operating_system: OperatingSystem,
    ) -> DeviceKind:
        model_l = (model or "").lower()
        os_l = (os_name or "").lower()

        if operating_system == OperatingSystem.MacOS or "mac" in model_l or os_l in {
            "macos",
            "mac os",
        }:
            return DeviceKind.MAC

        if "homepod" in model_l:
            return DeviceKind.AUDIO

        if any(token in model_l for token in _SONOS_MODEL_TOKENS) and "apple tv" not in model_l:
            return DeviceKind.AUDIO

        if "apple tv" in model_l or operating_system == OperatingSystem.TvOS or os_l == "tvos":
            return DeviceKind.APPLE_TV

        return DeviceKind.UNKNOWN

    def _capabilities_from_features(self, atv: AppleTV) -> list[Capability]:
        def level(feature: FeatureName) -> SupportLevel:
            state = atv.features.get_feature(feature).state
            if state == FeatureState.Available:
                return SupportLevel.SUPPORTED
            if state == FeatureState.Unavailable:
                return SupportLevel.UNAVAILABLE
            if state == FeatureState.Unsupported:
                return SupportLevel.UNSUPPORTED
            return SupportLevel.UNKNOWN

        absolute = atv.features.in_state(FeatureState.Available, FeatureName.SetVolume)
        relative = atv.features.in_state(
            FeatureState.Available, FeatureName.VolumeUp
        ) or atv.features.in_state(FeatureState.Available, FeatureName.VolumeDown)

        if absolute:
            volume_support = SupportLevel.SUPPORTED
            volume_semantics = CapabilitySemantics.ABSOLUTE
            volume_evidence = "FeatureName.SetVolume available"
        elif relative:
            volume_support = SupportLevel.DEGRADED
            volume_semantics = CapabilitySemantics.RELATIVE
            volume_evidence = "CEC/relative volume only (SetVolume unavailable)"
        else:
            volume_support = SupportLevel.UNAVAILABLE
            volume_semantics = CapabilitySemantics.RELATIVE
            volume_evidence = "Absolute and relative volume unavailable"

        return [
            Capability(name="power", support=level(FeatureName.TurnOn), adapter=self.name),
            Capability(name="apps.list", support=level(FeatureName.AppList), adapter=self.name),
            Capability(name="apps.launch", support=level(FeatureName.LaunchApp), adapter=self.name),
            Capability(
                name="content.deep_link",
                support=level(FeatureName.LaunchApp),
                adapter=self.name,
            ),
            Capability(
                name="volume.set_absolute",
                support=volume_support if absolute else SupportLevel.UNAVAILABLE,
                adapter=self.name,
                semantics=volume_semantics,
                evidence=volume_evidence,
            ),
            Capability(
                name="volume.relative",
                support=SupportLevel.SUPPORTED if relative else SupportLevel.UNAVAILABLE,
                adapter=self.name,
                semantics=CapabilitySemantics.RELATIVE,
                evidence=volume_evidence,
            ),
            Capability(
                name="transport",
                support=level(FeatureName.Play),
                adapter=self.name,
            ),
            Capability(
                name="remote.key",
                support=level(FeatureName.Select),
                adapter=self.name,
            ),
            Capability(
                name="keyboard.text",
                support=level(FeatureName.TextSet),
                adapter=self.name,
            ),
        ]

    def _static_capabilities(self, *, paired: bool) -> list[Capability]:
        base = SupportLevel.SUPPORTED if paired else SupportLevel.UNKNOWN
        return [
            Capability(name="power", support=base, adapter=self.name),
            Capability(name="apps.list", support=base, adapter=self.name),
            Capability(name="apps.launch", support=base, adapter=self.name),
            Capability(name="content.deep_link", support=base, adapter=self.name),
            Capability(
                name="volume.set_absolute",
                support=SupportLevel.UNAVAILABLE,
                adapter=self.name,
                semantics=CapabilitySemantics.RELATIVE,
                evidence="CEC relative only until FeatureName.SetVolume proves absolute",
            ),
            Capability(
                name="volume.relative",
                support=SupportLevel.DEGRADED,
                adapter=self.name,
                semantics=CapabilitySemantics.RELATIVE,
                evidence="Assumed CEC relative; confirm after pairing",
            ),
            Capability(name="transport", support=base, adapter=self.name),
        ]

    def _parse_protocol(self, protocol: str) -> Protocol:
        key = protocol.strip().lower()
        proto = _PROTOCOL_BY_NAME.get(key)
        if proto is None:
            raise UnsupportedError(
                "pairing.protocol",
                reason="Only companion and airplay pairing are supported",
            )
        return proto

    def _map_power(self, state: PyPowerState) -> PowerState:
        if state == PyPowerState.On:
            return PowerState.ON
        if state == PyPowerState.Off:
            return PowerState.OFF
        return PowerState.UNKNOWN

    def _map_network(self, exc: BaseException, message: str) -> NetworkError | TimeoutError_:
        if isinstance(exc, OperationTimeoutError):
            return TimeoutError_(message)
        if isinstance(exc, ConnectionFailedError):
            return NetworkError(message, retryable=True)
        return NetworkError(message, retryable=True)
