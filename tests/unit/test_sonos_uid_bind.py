"""Sonos vendor_stable_id binding must win over IP/name drift."""

from __future__ import annotations

from home_media.adapters.fake import FakeSonosAdapter
from home_media.adapters.sonos import SonosAdapter
from home_media.config import theater_seed_config
from home_media.errors import NetworkError
from home_media.models import DeviceKind, DiscoveredEndpoint
from home_media.registry import RoomRegistry


def test_theater_sonos_has_vendor_stable_id() -> None:
    cfg = theater_seed_config()
    theater_audio = next(d for d in cfg.devices if d.id == "sonos-theater-beam")
    assert theater_audio.vendor_stable_id == "RINCON_00000000000101400"


def test_ip_swap_does_not_change_configured_audio_id() -> None:
    reg = RoomRegistry(theater_seed_config())
    # Simulate rediscovery: same stable device id, new IP
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id="sonos-theater-beam",
            kind=DeviceKind.AUDIO,
            name="Theater",
            address="192.0.2.42",
            protocols=["sonos"],
            model="Beam",
            raw={"uid": "RINCON_00000000000101400"},
        )
    )
    # Family Room Arc claims old Theater IP — must not become theater audio target
    reg.update_endpoint(
        DiscoveredEndpoint(
            device_id="sonos-family-room-arc",
            kind=DeviceKind.AUDIO,
            name="Family Room",
            address="192.0.2.21",
            protocols=["sonos"],
            model="Arc",
            raw={"uid": "RINCON_00000000000201400"},
        )
    )
    theater = reg.room_targets("theater")["audio"]
    assert theater is not None
    assert theater.id == "sonos-theater-beam"
    assert theater.vendor_stable_id == "RINCON_00000000000101400"
    ep = reg.endpoint_for("sonos-theater-beam")
    assert ep is not None
    assert ep.address == "192.0.2.42"


def test_fake_sonos_sub_not_preferred_room_target() -> None:
    fake = FakeSonosAdapter()
    fake.seed("sonos-theater-beam", name="Theater", address="192.0.2.21")
    # discover includes a Sub noise endpoint in fake
    import asyncio

    endpoints = asyncio.run(fake.discover())
    sub = next(e for e in endpoints if e.raw.get("is_sub"))
    assert sub.device_id != "sonos-theater-beam"


def test_configured_sonos_uid_never_falls_back_to_friendly_name() -> None:
    reg = RoomRegistry(theater_seed_config())
    adapter = SonosAdapter(reg)
    refs = [d for d in reg.config.devices if d.adapter == "sonos"]
    assert (
        adapter._match_audio_device_id(  # noqa: SLF001
            "RINCON_WRONG", "Theater", refs
        )
        is None
    )
    assert (
        adapter._match_audio_device_id(  # noqa: SLF001
            "RINCON_00000000000101400", "Renamed Theater", refs
        )
        == "sonos-theater-beam"
    )


def test_cached_sonos_mapping_is_revalidated_by_uid() -> None:
    from types import SimpleNamespace

    reg = RoomRegistry(theater_seed_config())
    adapter = SonosAdapter(reg)
    wrong = SimpleNamespace(uid="RINCON_WRONG")
    adapter._by_device_id["sonos-theater-beam"] = wrong  # type: ignore[assignment]  # noqa: SLF001
    try:
        adapter._validate_stable_uid(wrong, "sonos-theater-beam")  # type: ignore[arg-type]  # noqa: SLF001
    except NetworkError:
        pass
    else:
        raise AssertionError("wrong cached Sonos UID was accepted")
    assert "sonos-theater-beam" not in adapter._by_device_id  # noqa: SLF001
