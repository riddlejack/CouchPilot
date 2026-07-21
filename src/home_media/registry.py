"""Room/device registry keyed by stable identifiers."""

from __future__ import annotations

from dataclasses import dataclass, field

from home_media.errors import AmbiguousRoomError, ConfigError, UnknownRoomError
from home_media.models import DeviceKind, DeviceRef, DiscoveredEndpoint, HomeConfig, RoomConfig


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


@dataclass
class RoomRegistry:
    config: HomeConfig
    endpoints: dict[str, DiscoveredEndpoint] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        seen_ids: set[str] = set()
        for device in self.config.devices:
            if device.id in seen_ids:
                raise ConfigError(f"Duplicate stable device id: {device.id}")
            seen_ids.add(device.id)

        alias_map: dict[str, str] = {}
        for room in self.config.rooms:
            names = [room.key, room.display_name, *room.aliases]
            for name in names:
                key = _norm(name)
                if key in alias_map and alias_map[key] != room.key:
                    raise ConfigError(
                        f"Ambiguous room alias '{name}' maps to both "
                        f"{alias_map[key]} and {room.key}"
                    )
                alias_map[key] = room.key

        for room in self.config.rooms:
            for attr, kind in (
                ("apple_tv_id", DeviceKind.APPLE_TV),
                ("physical_tv_id", DeviceKind.PHYSICAL_TV),
                ("audio_id", DeviceKind.AUDIO),
            ):
                device_id = getattr(room, attr)
                if not device_id:
                    continue
                device = self.device(device_id)
                if device.kind != kind:
                    raise ConfigError(
                        f"Room {room.key} {attr} points to {device.id} of kind {device.kind}"
                    )
                if device.room_key != room.key:
                    raise ConfigError(
                        f"Device {device.id} room_key {device.room_key} != room {room.key}"
                    )

    def rooms(self) -> list[RoomConfig]:
        return list(self.config.rooms)

    def room(self, room_key: str) -> RoomConfig:
        for room in self.config.rooms:
            if room.key == room_key:
                return room
        raise UnknownRoomError(room_key)

    def resolve_room(self, name: str) -> RoomConfig:
        """Resolve a room by key, display name, or unique alias.

        Friendly device names alone never resolve a mutation target.
        """
        needle = _norm(name)
        matches: list[RoomConfig] = []
        for room in self.config.rooms:
            candidates = {
                _norm(room.key),
                _norm(room.display_name),
                *(_norm(a) for a in room.aliases),
            }
            if needle in candidates:
                matches.append(room)
        if not matches:
            raise UnknownRoomError(name)
        if len(matches) > 1:
            raise AmbiguousRoomError(
                f"Room name '{name}' is ambiguous",
                matches=[m.key for m in matches],
            )
        return matches[0]

    def device(self, device_id: str) -> DeviceRef:
        for device in self.config.devices:
            if device.id == device_id:
                return device
        raise ConfigError(f"Unknown device id: {device_id}")

    def devices_for_room(self, room_key: str) -> list[DeviceRef]:
        self.room(room_key)
        return [d for d in self.config.devices if d.room_key == room_key]

    def room_targets(self, room_key: str) -> dict[str, DeviceRef | None]:
        room = self.room(room_key)
        return {
            "apple_tv": self.device(room.apple_tv_id) if room.apple_tv_id else None,
            "physical_tv": self.device(room.physical_tv_id) if room.physical_tv_id else None,
            "audio": self.device(room.audio_id) if room.audio_id else None,
        }

    def update_endpoint(self, endpoint: DiscoveredEndpoint) -> None:
        """Bind a discovered address to a known stable id; never invent devices from IPs."""
        existing = self.endpoints.get(endpoint.device_id)
        if existing and existing.address != endpoint.address:
            # IP drift is expected; identity stays the same.
            endpoint.raw = {**existing.raw, **endpoint.raw, "previous_address": existing.address}
        self.endpoints[endpoint.device_id] = endpoint

    def endpoint_for(self, device_id: str) -> DiscoveredEndpoint | None:
        return self.endpoints.get(device_id)

    def filter_apple_tvs(self, endpoints: list[DiscoveredEndpoint]) -> list[DiscoveredEndpoint]:
        """Keep real tvOS Apple TVs; drop Macs and Sonos AirPlay speakers."""
        kept: list[DiscoveredEndpoint] = []
        for ep in endpoints:
            model = (ep.model or "").lower()
            os_name = (ep.os or "").lower()
            if "mac" in model or os_name in {"macos", "mac os"}:
                continue
            if any(
                token in model for token in ("beam", "arc", "port", "amp", "one", "five", "sub")
            ) and "apple tv" not in model:
                continue
            if "apple tv" in model or os_name == "tvos":
                kept.append(ep)
        return kept
