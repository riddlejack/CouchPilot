"""Apple TV discovery, paired control, and status models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class DeviceKind(StrEnum):
    APPLE_TV = "apple_tv"
    PHYSICAL_TV = "physical_tv"
    AUDIO = "audio"
    RECEIVER = "receiver"
    MAC = "mac"
    UNKNOWN = "unknown"


class SupportLevel(StrEnum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class CapabilitySemantics(StrEnum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"
    BEST_EFFORT = "best_effort"


class PowerState(StrEnum):
    ON = "on"
    OFF = "off"
    STANDBY = "standby"
    UNKNOWN = "unknown"


class DiscoveredEndpoint(BaseModel):
    device_id: str
    kind: DeviceKind
    name: str
    address: str
    protocols: list[str] = Field(default_factory=list)
    model: str | None = None
    os: str | None = None
    os_version: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Capability(BaseModel):
    name: str
    support: SupportLevel
    adapter: str
    semantics: CapabilitySemantics = CapabilitySemantics.BEST_EFFORT
    evidence: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)


class PairingSession(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    room_key: str
    device_id: str
    protocol: str
    started_at: datetime = Field(default_factory=utcnow)
    state: Literal["awaiting_pin", "completed", "failed", "cancelled"] = "awaiting_pin"
    message: str | None = None


class NowPlaying(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    app_id: str | None = None
    app_name: str | None = None
    device_state: str | None = None
    position_s: float | None = None
    total_time_s: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class DeviceStatus(BaseModel):
    device_id: str
    kind: DeviceKind
    name: str | None = None
    address: str | None = None
    power: PowerState = PowerState.UNKNOWN
    current_app: str | None = None
    now_playing: NowPlaying | None = None
    volume: int | None = None
    muted: bool | None = None
    input_source: str | None = None
    paired: bool | None = None
    available: bool = True
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AppInfo(BaseModel):
    name: str
    identifier: str
