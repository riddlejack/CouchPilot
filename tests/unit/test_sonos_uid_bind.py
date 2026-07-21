"""Sonos vendor_stable_id binding must win over IP/name drift."""

from __future__ import annotations

from home_media.adapters.fake import FakeSonosAdapter
from home_media.config import lounge_seed_config
from home_media.models import DeviceKind, DiscoveredEndpoint
from home_media.registry import RoomRegistry


def test_lounge_sonos_has_vendor_stable_id() -> None:
    cfg = lounge_seed_config()
    lounge_audio = next(d for d in cfg.devices if d.id == "sonos-lounge-beam")
    assert lounge_audio.vendor_stable_id == "RINCON_00000000000101400"


def test_ip_swap_does_not_change_configured_audio_id() -> None:
    reg = RoomRegistry(lounge_seed_config())
    # Simulate rediscovery: same stable device id, new IP
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id="sonos-lounge-beam",
            kind=DeviceKind.AUDIO,
            name="Lounge",
            address="192.0.2.42",
            protocols=["sonos"],
            model="Beam",
            raw={"uid": "RINCON_00000000000101400"},
        )
    )
    # Guest Room Arc claims old Lounge IP — must not become lounge audio target
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id="sonos-guest-room-arc",
            kind=DeviceKind.AUDIO,
            name="Guest Room",
            address="192.0.2.21",
            protocols=["sonos"],
            model="Arc",
            raw={"uid": "RINCON_00000000000201400"},
        )
    )
    lounge = reg.room_targets("lounge")["audio"]
    assert lounge is not None
    assert lounge.id == "sonos-lounge-beam"
    assert lounge.vendor_stable_id == "RINCON_00000000000101400"
    ep = reg.endpoint_for("sonos-lounge-beam")
    assert ep is not None
    assert ep.address == "192.0.2.42"


def test_fake_sonos_sub_not_preferred_room_target() -> None:
    fake = FakeSonosAdapter()
    fake.seed("sonos-lounge-beam", name="Lounge", address="192.0.2.21")
    # discover includes a Sub noise endpoint in fake
    import asyncio

    endpoints = asyncio.run(fake.discover())
    sub = next(e for e in endpoints if e.raw.get("is_sub"))
    assert sub.device_id != "sonos-lounge-beam"
