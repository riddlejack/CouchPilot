"""Registry and identity tests."""

from __future__ import annotations

import pytest

from home_media.config import theater_seed_config
from home_media.errors import ConfigError, UnknownRoomError
from home_media.models import DeviceKind, DiscoveredEndpoint, HomeConfig, RoomConfig
from home_media.registry import RoomRegistry


def test_theater_resolves_three_distinct_targets() -> None:
    reg = RoomRegistry(theater_seed_config())
    room = reg.resolve_room("theater")
    targets = reg.room_targets(room.key)
    assert targets["apple_tv"] is not None
    assert targets["physical_tv"] is not None
    assert targets["audio"] is not None
    ids = {targets["apple_tv"].id, targets["physical_tv"].id, targets["audio"].id}
    assert len(ids) == 3
    assert targets["apple_tv"].kind == DeviceKind.APPLE_TV
    assert targets["physical_tv"].kind == DeviceKind.PHYSICAL_TV
    assert targets["audio"].kind == DeviceKind.AUDIO


def test_alias_theater_tv_resolves_room_not_device() -> None:
    reg = RoomRegistry(theater_seed_config())
    room = reg.resolve_room("theater tv")
    assert room.key == "theater"


def test_unknown_room() -> None:
    reg = RoomRegistry(theater_seed_config())
    with pytest.raises(UnknownRoomError):
        reg.resolve_room("basement cinema")


def test_duplicate_stable_id_rejected() -> None:
    cfg = theater_seed_config()
    cfg.devices.append(cfg.devices[0].model_copy())
    with pytest.raises(ConfigError, match="Duplicate"):
        RoomRegistry(cfg)


def test_ambiguous_alias_rejected() -> None:
    cfg = HomeConfig(
        rooms=[
            RoomConfig(key="a", display_name="A", aliases=["shared"]),
            RoomConfig(key="b", display_name="B", aliases=["shared"]),
        ],
        devices=[],
    )
    with pytest.raises(ConfigError, match="Ambiguous"):
        RoomRegistry(cfg)


def test_ip_drift_updates_endpoint_not_identity() -> None:
    reg = RoomRegistry(theater_seed_config())
    device_id = "00000000-0000-4000-8000-000000000001"
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id=device_id,
            kind=DeviceKind.APPLE_TV,
            name="Theater",
            address="192.0.2.11",
            model="Apple TV 4K (gen 2)",
            os="tvOS",
        )
    )
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id=device_id,
            kind=DeviceKind.APPLE_TV,
            name="Theater",
            address="192.0.2.42",
            model="Apple TV 4K (gen 2)",
            os="tvOS",
        )
    )
    ep = reg.endpoint_for(device_id)
    assert ep is not None
    assert ep.address == "192.0.2.42"
    assert ep.raw.get("previous_address") == "192.0.2.11"


def test_filter_macs_and_sonos_from_apple_tvs() -> None:
    reg = RoomRegistry(theater_seed_config())
    endpoints = [
        DiscoveredEndpoint(
            device_id="1",
            kind=DeviceKind.APPLE_TV,
            name="Theater",
            address="1.1.1.1",
            model="Apple TV 4K (gen 2)",
            os="tvOS",
        ),
        DiscoveredEndpoint(
            device_id="2",
            kind=DeviceKind.MAC,
            name="Mac",
            address="1.1.1.2",
            model="Mac17,8",
            os="MacOS",
        ),
        DiscoveredEndpoint(
            device_id="3",
            kind=DeviceKind.AUDIO,
            name="Theater (2)",
            address="1.1.1.3",
            model="Beam",
            os="Unknown OS",
        ),
    ]
    kept = reg.filter_apple_tvs(endpoints)
    assert len(kept) == 1
    assert kept[0].device_id == "1"


def test_friendly_device_name_alone_is_not_mutation_target() -> None:
    """Mutations go through room resolution; device aliases are not room keys."""
    reg = RoomRegistry(theater_seed_config())
    # "Theater Apple TV" is a device alias, not a room alias — must not resolve as room.
    with pytest.raises(UnknownRoomError):
        reg.resolve_room("Theater Apple TV")
