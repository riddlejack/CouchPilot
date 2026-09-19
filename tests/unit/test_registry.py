"""Stable identity survives discovered IP changes."""

from home_media.models import DeviceKind, DiscoveredEndpoint
from home_media.registry import DeviceRegistry


def test_ip_drift_updates_endpoint_not_identity() -> None:
    reg = DeviceRegistry()
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
