"""Discovered Apple TV endpoints keyed by stable device identity."""

from __future__ import annotations

from dataclasses import dataclass, field

from home_media.models import DiscoveredEndpoint


@dataclass
class DeviceRegistry:
    endpoints: dict[str, DiscoveredEndpoint] = field(default_factory=dict)

    def update_endpoint(self, endpoint: DiscoveredEndpoint) -> None:
        """Bind a discovered address to a known stable id; never invent devices from IPs."""
        existing = self.endpoints.get(endpoint.device_id)
        if existing and existing.address != endpoint.address:
            # IP drift is expected; identity stays the same.
            endpoint.raw = {**existing.raw, **endpoint.raw, "previous_address": existing.address}
        self.endpoints[endpoint.device_id] = endpoint

    def endpoint_for(self, device_id: str) -> DiscoveredEndpoint | None:
        return self.endpoints.get(device_id)
