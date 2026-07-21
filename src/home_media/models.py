"""Domain models shared by CLI, MCP, planner, and adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

JSON_SCHEMA_VERSION = 1


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


class EvidenceLabel(StrEnum):
    AUTOMATED = "automated"
    LIVE_VERIFIED = "live_verified"
    DEGRADED = "degraded"
    UNTESTED = "untested"
    UNSUPPORTED = "unsupported"


class ExecutionStatus(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class VerificationStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DEGRADED = "degraded"
    FAILED = "failed"


class ContentOutcome(StrEnum):
    VERIFIED_PLAYBACK = "verified_playback"
    OPENED_TARGET = "opened_target"
    OPENED_APP_ONLY = "opened_app_only"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class PowerState(StrEnum):
    ON = "on"
    OFF = "off"
    STANDBY = "standby"
    UNKNOWN = "unknown"


class DeviceRef(BaseModel):
    id: str
    kind: DeviceKind
    room_key: str
    adapter: str
    aliases: list[str] = Field(default_factory=list)
    vendor: str | None = None
    model: str | None = None
    preferred: bool = False
    # Vendor-stable identity (Sonos RINCON UID, etc.). IPs are never identities.
    vendor_stable_id: str | None = None


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


class RoomConfig(BaseModel):
    key: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    apple_tv_id: str | None = None
    physical_tv_id: str | None = None
    audio_id: str | None = None
    preferred_volume_target: Literal["sonos", "physical_tv", "apple_tv_cec", "none"] = "sonos"
    preferred_power_path: Literal["apple_tv_cec", "physical_tv", "both"] = "both"


class HomeConfig(BaseModel):
    schema_version: int = 1
    home_name: str = "home"
    mutations_enabled: bool = True
    volume_ceiling: int = 40
    volume_ceiling_override_required: bool = True
    rooms: list[RoomConfig] = Field(default_factory=list)
    devices: list[DeviceRef] = Field(default_factory=list)
    content_aliases: dict[str, str] = Field(default_factory=dict)
    app_aliases: dict[str, str] = Field(default_factory=dict)
    # Provider UI preferences for navigation fallbacks (1-based profile index, etc.)
    provider_prefs: dict[str, dict[str, object]] = Field(default_factory=dict)

    @field_validator("volume_ceiling")
    @classmethod
    def _ceiling_range(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("volume_ceiling must be 0-100")
        return value


class PlanStep(BaseModel):
    step_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    action: str
    target_device_id: str
    adapter: str
    params: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class ActionPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    intent: str
    room_key: str
    steps: list[PlanStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class StepResult(BaseModel):
    step_id: str
    action: str
    target_device_id: str
    execution_status: ExecutionStatus
    verification_status: VerificationStatus = VerificationStatus.NOT_APPLICABLE
    observed_before: dict[str, Any] = Field(default_factory=dict)
    observed_after: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    latency_ms: int | None = None
    warnings: list[str] = Field(default_factory=list)


class ActionResult(BaseModel):
    schema_version: int = JSON_SCHEMA_VERSION
    action_id: str = Field(default_factory=lambda: uuid4().hex)
    requested_intent: str
    room_key: str | None = None
    targets: list[str] = Field(default_factory=list)
    plan: ActionPlan | None = None
    steps: list[StepResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    execution_status: ExecutionStatus = ExecutionStatus.STARTED
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    observed_before: dict[str, Any] = Field(default_factory=dict)
    observed_after: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    retryable: bool = False
    idempotency_key: str | None = None
    dry_run: bool = False
    content_outcome: ContentOutcome | None = None
    error: dict[str, Any] | None = None

    def finish(
        self,
        *,
        execution_status: ExecutionStatus,
        verification_status: VerificationStatus | None = None,
    ) -> ActionResult:
        self.finished_at = utcnow()
        self.execution_status = execution_status
        if verification_status is not None:
            self.verification_status = verification_status
        return self


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


class RoomStatus(BaseModel):
    schema_version: int = JSON_SCHEMA_VERSION
    room_key: str
    display_name: str
    apple_tv: DeviceStatus | None = None
    physical_tv: DeviceStatus | None = None
    audio: DeviceStatus | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    # Per-role failures preserved when DeviceStatus could not be built.
    device_errors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utcnow)


class ContentTarget(BaseModel):
    provider: str | None = None
    url: str | None = None
    app_bundle_id: str | None = None
    title: str | None = None
    series: str | None = None
    season: int | None = None
    episode: int | None = None
    resolution_source: Literal["direct_url", "alias", "provider", "app_only"] = "direct_url"
    confidence: float = 1.0
    expected_app: str | None = None


class AppInfo(BaseModel):
    name: str
    identifier: str


class Envelope(BaseModel):
    """Versioned JSON envelope for CLI/MCP responses."""

    schema_version: int = JSON_SCHEMA_VERSION
    ok: bool = True
    data: Any = None
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
