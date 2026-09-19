"""In-memory fake adapters for unit/contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from home_media.errors import (
    AuthRequiredError,
    DeviceRejectedError,
    NetworkError,
    StaleEndpointError,
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
    utcnow,
)


class FakeAppleTVAdapter:
    name = "apple_tv"

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, PairingSession] = {}
        self.mutations: list[dict[str, Any]] = []
        self.fail_next: str | None = None
        self.stale_ids: set[str] = set()
        self.provider_state: dict[str, str] = {}
        self.highlighted_profile: dict[str, str] = {}
        self.connect_count: int = 0
        self.scan_count: int = 0

    def seed(
        self,
        device_id: str,
        *,
        name: str,
        address: str,
        paired: bool = False,
        model: str = "Apple TV 4K (gen 2)",
        os_version: str = "26.5",
    ) -> None:
        self.devices[device_id] = {
            "name": name,
            "address": address,
            "paired": paired,
            "model": model,
            "os": "tvOS",
            "os_version": os_version,
            "power": PowerState.ON if paired else PowerState.UNKNOWN,
            "apps": [
                AppInfo(name="Netflix", identifier="com.netflix.Netflix"),
                AppInfo(name="YouTube", identifier="com.google.ios.youtube"),
                AppInfo(name="Search", identifier="com.apple.TVSearch"),
            ],
            "current_app": None,
            "now_playing": None,
            "volume_absolute": False,
            "keyboard_focus": False,
            "keyboard_text": "",
        }

    async def discover(self) -> list[DiscoveredEndpoint]:
        out: list[DiscoveredEndpoint] = []
        for device_id, data in self.devices.items():
            out.append(
                DiscoveredEndpoint(
                    device_id=device_id,
                    kind=DeviceKind.APPLE_TV,
                    name=data["name"],
                    address=data["address"],
                    protocols=["companion", "airplay", "raop"],
                    model=data["model"],
                    os=data["os"],
                    os_version=data["os_version"],
                )
            )
        # Noise that must be filtered by registry helpers in real discovery path.
        out.append(
            DiscoveredEndpoint(
                device_id="mac-noise",
                kind=DeviceKind.MAC,
                name="Personal MacBook Pro",
                address="192.0.2.40",
                protocols=["companion", "airplay"],
                model="Mac17,8",
                os="MacOS",
            )
        )
        out.append(
            DiscoveredEndpoint(
                device_id="sonos-noise",
                kind=DeviceKind.AUDIO,
                name="Theater (2)",
                address="192.0.2.21",
                protocols=["airplay", "raop"],
                model="Beam",
                os="Unknown OS",
            )
        )
        return out

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        data = self._require(device_id)
        volume_support = (
            SupportLevel.UNAVAILABLE if not data["volume_absolute"] else SupportLevel.SUPPORTED
        )
        return [
            Capability(name="power", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(name="apps.list", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(name="apps.launch", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(name="content.deep_link", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(
                name="volume.set_absolute",
                support=volume_support,
                adapter=self.name,
                semantics=CapabilitySemantics.RELATIVE,
                evidence="CEC relative only in fake",
            ),
            Capability(name="transport", support=SupportLevel.SUPPORTED, adapter=self.name),
        ]

    async def get_status(self, device_id: str) -> DeviceStatus:
        self._maybe_fail("status")
        if device_id in self.stale_ids:
            raise StaleEndpointError(device_id, self.devices[device_id]["address"])
        data = self._require(device_id)
        if not data["paired"]:
            raise AuthRequiredError(device_id, protocol="companion")
        return DeviceStatus(
            device_id=device_id,
            kind=DeviceKind.APPLE_TV,
            name=data["name"],
            address=data["address"],
            power=data["power"],
            current_app=data["current_app"],
            now_playing=deepcopy(data["now_playing"]),
            paired=True,
            evidence=["fake_status"],
        )

    async def pair_start(self, device_id: str, protocol: str) -> PairingSession:
        self._require(device_id)
        session = PairingSession(
            room_key="",
            device_id=device_id,
            protocol=protocol,
            message="Enter PIN shown on Apple TV",
        )
        self.sessions[session.session_id] = session
        return session

    async def pair_finish(self, session_id: str, pin: str) -> PairingSession:
        session = self.sessions[session_id]
        if pin == "0000":
            session.state = "failed"
            session.message = "Invalid PIN"
            return session
        self.devices[session.device_id]["paired"] = True
        self.devices[session.device_id]["power"] = PowerState.ON
        session.state = "completed"
        session.message = "Paired"
        # Never persist the PIN on the session object.
        return session

    async def set_power(self, device_id: str, state: PowerState) -> DeviceStatus:
        self._record("set_power", device_id, state=state.value)
        data = self._require_paired(device_id)
        data["power"] = state
        return await self.get_status(device_id)

    async def list_apps(self, device_id: str) -> list[AppInfo]:
        data = self._require_paired(device_id)
        return list(data["apps"])

    async def open_app(self, device_id: str, app_id: str) -> DeviceStatus:
        self._record("open_app", device_id, app_id=app_id)
        self.connect_count += 1
        data = self._require_paired(device_id)
        data["current_app"] = app_id
        data["now_playing"] = None
        if app_id == "com.apple.TVSearch":
            data["keyboard_focus"] = True
            data["keyboard_text"] = ""
        if app_id == "com.netflix.Netflix":
            current = self.provider_state.get(device_id)
            if current is None or current in {"unknown", "apple_home"}:
                # Cold launch defaults to profile picker; tests may pre-inject home/playing.
                self.provider_state[device_id] = "profile_picker"
        return await self.get_status(device_id)

    async def open_url(self, device_id: str, url: str) -> DeviceStatus:
        from home_media.content.urls import validate_content_url

        self._record("open_url", device_id, url=url)
        self.connect_count += 1
        data = self._require_paired(device_id)
        if url.startswith("airplay://"):
            raise DeviceRejectedError("Raw AirPlay URL streaming rejected in fake")
        validated = validate_content_url(url)
        if validated.provider == "netflix":
            data["current_app"] = "com.netflix.Netflix"
            # Title-detail open does not auto-play in the fake.
            data["now_playing"] = None
        else:
            data["current_app"] = data["current_app"]
            data["now_playing"] = NowPlaying(
                title="Fake Title",
                app_id=data["current_app"],
                device_state="playing",
            )
        return await self.get_status(device_id)

    async def control_transport(self, device_id: str, action: str) -> DeviceStatus:
        self._record("control_transport", device_id, transport_action=action)
        data = self._require_paired(device_id)
        if data["now_playing"] is None:
            data["now_playing"] = NowPlaying(title="Unknown", device_state=action)
        else:
            data["now_playing"].device_state = action
        return await self.get_status(device_id)

    async def press_key(self, device_id: str, key: str) -> None:
        self._record("press_key", device_id, key=key)
        data = self._require_paired(device_id)
        state = self.provider_state.get(device_id, "unknown")
        if key == "select" and state == "profile_picker":
            self.provider_state[device_id] = "home"
        elif key in {"up", "left", "down"} and state == "home":
            # Candidate Netflix Home → Search navigation: Up, Left, Down.
            if key == "down":
                self.provider_state[device_id] = "search_keyboard"
                data["keyboard_focus"] = True
        elif key == "select" and state == "unknown":
            raise UnsupportedError("remote.key", reason="Select forbidden from UNKNOWN in tests")

    async def enter_text(self, device_id: str, text: str) -> None:
        self._record("enter_text", device_id, text=text)
        data = self._require_paired(device_id)
        # Fakes model focus explicitly — never invent keyboard focus on text entry.
        if data.get("keyboard_focus"):
            data["keyboard_text"] = text

    async def keyboard_text_get(self, device_id: str) -> str | None:
        data = self._require_paired(device_id)
        return str(data.get("keyboard_text") or "")

    async def keyboard_text_clear(self, device_id: str) -> None:
        data = self._require_paired(device_id)
        data["keyboard_text"] = ""

    async def keyboard_focus_state(self, device_id: str) -> str:
        data = self._require_paired(device_id)
        return "focused" if data.get("keyboard_focus") else "unfocused"

    async def wait_keyboard_focused(
        self,
        device_id: str,
        *,
        timeout_s: float = 8.0,
        poll_s: float = 0.25,
    ) -> str:
        _ = timeout_s, poll_s
        return await self.keyboard_focus_state(device_id)

    def set_keyboard_focus(self, device_id: str, focused: bool) -> None:
        data = self._require(device_id)
        data["keyboard_focus"] = focused

    def set_provider_state(self, device_id: str, state: str) -> None:
        self.provider_state[device_id] = state

    async def aclose(self) -> None:
        self.sessions.clear()

    async def get_volume(self, device_id: str) -> dict[str, Any]:
        self._require_paired(device_id)
        raise UnsupportedError("volume.set_absolute", reason="CEC relative only")

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]:
        raise UnsupportedError("volume.set_absolute", reason="CEC relative only")

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]:
        self._record("change_volume", device_id, delta=delta)
        return {"semantics": "relative", "delta": delta, "verified": False}

    async def set_input(self, device_id: str, source: str) -> DeviceStatus:
        raise UnsupportedError("tv.input", reason="Apple TV has no physical input switch")

    def _require(self, device_id: str) -> dict[str, Any]:
        if device_id not in self.devices:
            raise NetworkError(f"Unknown fake device {device_id}", retryable=False)
        return self.devices[device_id]

    def _require_paired(self, device_id: str) -> dict[str, Any]:
        data = self._require(device_id)
        if not data["paired"]:
            raise AuthRequiredError(device_id, protocol="companion")
        return data

    def _record(self, action: str, device_id: str, **params: Any) -> None:
        self.mutations.append({"action": action, "device_id": device_id, **params})

    def _maybe_fail(self, op: str) -> None:
        if self.fail_next == op:
            self.fail_next = None
            raise NetworkError("Injected transient failure", retryable=True)


class FakeSonosAdapter:
    name = "sonos"

    def __init__(self) -> None:
        self.zones: dict[str, dict[str, Any]] = {}
        self.mutations: list[dict[str, Any]] = []

    def seed(self, device_id: str, *, name: str, address: str, volume: int = 15) -> None:
        self.zones[device_id] = {
            "name": name,
            "address": address,
            "volume": volume,
            "muted": False,
            "coordinator": True,
            "model": "Beam",
            "uid": f"RINCON_FAKE{device_id[-6:]}",
        }

    async def discover(self) -> list[DiscoveredEndpoint]:
        # Include a bonded Sub that must not be treated as the room audio target.
        endpoints = [
            DiscoveredEndpoint(
                device_id=device_id,
                kind=DeviceKind.AUDIO,
                name=data["name"],
                address=data["address"],
                protocols=["sonos"],
                model=data["model"],
                raw={"uid": data["uid"], "is_coordinator": True, "is_sub": False},
            )
            for device_id, data in self.zones.items()
        ]
        endpoints.append(
            DiscoveredEndpoint(
                device_id="sonos-theater-sub",
                kind=DeviceKind.AUDIO,
                name="Sub",
                address="192.0.2.41",
                protocols=["sonos"],
                model="Sub",
                raw={"uid": "RINCON_SUB", "is_coordinator": False, "is_sub": True},
            )
        )
        return endpoints

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        self._require(device_id)
        return [
            Capability(
                name="volume.set_absolute",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                semantics=CapabilitySemantics.ABSOLUTE,
            ),
            Capability(name="transport", support=SupportLevel.SUPPORTED, adapter=self.name),
        ]

    async def get_status(self, device_id: str) -> DeviceStatus:
        data = self._require(device_id)
        return DeviceStatus(
            device_id=device_id,
            kind=DeviceKind.AUDIO,
            name=data["name"],
            address=data["address"],
            volume=data["volume"],
            muted=data["muted"],
            power=PowerState.ON,
            evidence=["fake_sonos"],
        )

    async def get_volume(self, device_id: str) -> dict[str, Any]:
        data = self._require(device_id)
        return {
            "level": data["volume"],
            "muted": data["muted"],
            "semantics": "absolute",
            "coordinator": data["coordinator"],
        }

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]:
        data = self._require(device_id)
        self.mutations.append({"action": "set_volume", "device_id": device_id, "level": level})
        data["volume"] = level
        return await self.get_volume(device_id)

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]:
        data = self._require(device_id)
        return await self.set_volume(device_id, max(0, min(100, data["volume"] + delta)))

    async def control_transport(self, device_id: str, action: str) -> DeviceStatus:
        self.mutations.append({"action": action, "device_id": device_id})
        return await self.get_status(device_id)

    def _require(self, device_id: str) -> dict[str, Any]:
        if device_id not in self.zones:
            raise NetworkError(f"Unknown sonos zone {device_id}", retryable=False)
        return self.zones[device_id]


class FakePhysicalTVAdapter:
    name = "android_tv"

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.mutations: list[dict[str, Any]] = []
        self.sessions: dict[str, PairingSession] = {}

    def seed(self, device_id: str, *, name: str, address: str, paired: bool = False) -> None:
        self.devices[device_id] = {
            "name": name,
            "address": address,
            "paired": paired,
            "power": PowerState.OFF,
            "input": "HDMI1",
        }

    async def discover(self) -> list[DiscoveredEndpoint]:
        return [
            DiscoveredEndpoint(
                device_id=device_id,
                kind=DeviceKind.PHYSICAL_TV,
                name=data["name"],
                address=data["address"],
                protocols=["android_tv_remote_v2", "google_cast"],
                model="BRAVIA 4K GB",
            )
            for device_id, data in self.devices.items()
        ]

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        self._require(device_id)
        return [
            Capability(name="power", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(name="tv.input", support=SupportLevel.SUPPORTED, adapter=self.name),
            Capability(
                name="power.physical_tv_state",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                evidence="direct androidtvremote2",
            ),
        ]

    async def get_status(self, device_id: str) -> DeviceStatus:
        data = self._require(device_id)
        if not data["paired"]:
            raise AuthRequiredError(device_id, protocol="android_tv_remote_v2")
        return DeviceStatus(
            device_id=device_id,
            kind=DeviceKind.PHYSICAL_TV,
            name=data["name"],
            address=data["address"],
            power=data["power"],
            input_source=data["input"],
            paired=True,
            evidence=["fake_physical_tv"],
        )

    async def pair_start(
        self,
        device_id: str,
        protocol: str = "android_tv_remote_v2",
    ) -> PairingSession:
        self._require(device_id)
        session = PairingSession(
            session_id=uuid4().hex,
            room_key="",
            device_id=device_id,
            protocol=protocol,
            message="Confirm pairing code on the TV",
        )
        self.sessions[session.session_id] = session
        return session

    async def pair_finish(self, session_id: str, pin: str) -> PairingSession:
        session = self.sessions[session_id]
        self.devices[session.device_id]["paired"] = True
        session.state = "completed"
        return session

    async def set_power(self, device_id: str, state: PowerState) -> DeviceStatus:
        data = self._require_paired(device_id)
        self.mutations.append({"action": "set_power", "device_id": device_id, "state": state.value})
        data["power"] = state
        return await self.get_status(device_id)

    async def set_input(self, device_id: str, source: str) -> DeviceStatus:
        data = self._require_paired(device_id)
        self.mutations.append({"action": "set_input", "device_id": device_id, "source": source})
        data["power"] = PowerState.ON
        status = await self.get_status(device_id)
        # Do not echo the requested source as observed input.
        status.input_source = None
        status.warnings.append("fake input switch unverified")
        return status

    def _require(self, device_id: str) -> dict[str, Any]:
        if device_id not in self.devices:
            raise NetworkError(f"Unknown TV {device_id}", retryable=False)
        return self.devices[device_id]

    def _require_paired(self, device_id: str) -> dict[str, Any]:
        data = self._require(device_id)
        if not data["paired"]:
            raise AuthRequiredError(device_id, protocol="android_tv_remote_v2")
        return data


# silence unused import if any
_ = utcnow
