"""Adapter protocol contracts."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from home_media.models import (
    AppInfo,
    Capability,
    DeviceStatus,
    DiscoveredEndpoint,
    PairingSession,
    PowerState,
)


@runtime_checkable
class DeviceAdapter(Protocol):
    name: str

    async def discover(self) -> list[DiscoveredEndpoint]: ...

    async def get_capabilities(self, device_id: str) -> list[Capability]: ...

    async def get_status(self, device_id: str) -> DeviceStatus: ...


@runtime_checkable
class PairableAdapter(Protocol):
    async def pair_start(self, device_id: str, protocol: str) -> PairingSession: ...

    async def pair_finish(self, session_id: str, pin: str) -> PairingSession: ...


@runtime_checkable
class ControllableAdapter(Protocol):
    async def set_power(self, device_id: str, state: PowerState) -> DeviceStatus: ...

    async def list_apps(self, device_id: str) -> list[AppInfo]: ...

    async def open_app(self, device_id: str, app_id: str) -> DeviceStatus: ...

    async def open_url(self, device_id: str, url: str) -> DeviceStatus: ...

    async def control_transport(self, device_id: str, action: str) -> DeviceStatus: ...

    async def press_key(self, device_id: str, key: str) -> None: ...

    async def enter_text(self, device_id: str, text: str) -> None: ...

    async def get_volume(self, device_id: str) -> dict[str, Any]: ...

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]: ...

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]: ...

    async def set_input(self, device_id: str, source: str) -> DeviceStatus: ...
