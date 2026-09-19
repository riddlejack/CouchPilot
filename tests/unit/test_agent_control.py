"""Hardware-free integration contracts for the compact Apple TV agent service."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from home_media.agent_control import (
    AgentConfig,
    AgentDevice,
    AgentObservation,
    AppleTVAgent,
    HelperConfig,
)
from home_media.content.urls import validate_content_url
from home_media.errors import ConfigError, ErrorCode, SafetyBlockedError, UnsupportedError
from home_media.models import DeviceKind, DiscoveredEndpoint
from home_media.wda import (
    WDAClient,
    WDAElement,
    WDAError,
    WDAIdentity,
    WDAImage,
    WDAMutationResult,
    WDAMutationUncertain,
    WDAObservation,
    WDAStatus,
)
from home_media.wda_runtime import WDARuntimeState, WDARuntimeStatus

PINNED_UUID = "pinned-device-uuid"
PNG = b"\x89PNG\r\n\x1a\nagent-test"
ObservationKind = Literal[
    "semantic",
    "image_error",
    "error",
    "old_semantic",
    "missing_start",
    "missing_received",
    "naive_semantic",
    "future_semantic",
    "reversed_semantic",
    "naive_image",
]


def _identity(generation: int = 0, session_id: str = "session-1") -> WDAIdentity:
    return WDAIdentity(
        helper_build_id="helper-build",
        session_id=session_id,
        generation=generation,
    )


class FakeWDAClient(WDAClient):
    """Typed fake at the WDA boundary; it never opens a network connection."""

    def __init__(
        self,
        *,
        observation_kinds: list[ObservationKind] | None = None,
        device_uuid: str = PINNED_UUID,
    ) -> None:
        self.identity = _identity()
        self.device_uuid = device_uuid
        self.observation_kinds = deque(observation_kinds or ["semantic"])
        self.status_calls = 0
        self.identity_calls = 0
        self.observe_calls = 0
        self.press_calls = 0
        self.select_calls = 0
        self.text_calls = 0
        self.activate_calls = 0
        self.closed = False
        self.press_error: Exception | None = None

    async def status(self) -> WDAStatus:
        self.status_calls += 1
        now = datetime.now(UTC)
        return WDAStatus(
            ready=True,
            os_name="tvOS",
            os_version="26.6",
            wda_version="16.12.9",
            identity=self.identity,
            request_started_at=now,
            received_at=now,
        )

    async def device_identity(self) -> str:
        self.identity_calls += 1
        return self.device_uuid

    async def observe(
        self,
        include_image: bool = False,
        expected_app: str | None = None,
        expected_label: str | None = None,
        timeout_s: float = 8.0,
    ) -> WDAObservation:
        _ = timeout_s
        self.observe_calls += 1
        kind = self.observation_kinds.popleft()
        if not self.observation_kinds:
            self.observation_kinds.append(kind)
        now = datetime.now(UTC)
        semantic = kind not in {"image_error", "error"}
        semantic_started: datetime | None = now
        semantic_received: datetime | None = now
        if kind == "old_semantic":
            semantic_started = now - timedelta(seconds=60)
            semantic_received = semantic_started
        elif kind == "missing_start":
            semantic_started = None
        elif kind == "missing_received":
            semantic_received = None
        elif kind == "naive_semantic":
            semantic_started = now.replace(tzinfo=None)
            semantic_received = now.replace(tzinfo=None)
        elif kind == "future_semantic":
            semantic_started = now + timedelta(seconds=60)
            semantic_received = semantic_started
        elif kind == "reversed_semantic":
            semantic_started = now
            semantic_received = now - timedelta(milliseconds=1)
        has_image = kind == "image_error" or (include_image and semantic)
        errors = [] if semantic else ["semantic_observation_failed"]
        image_started = datetime.now(UTC)
        image_received = datetime.now(UTC)
        if kind == "naive_image":
            image_started = image_started.replace(tzinfo=None)
            image_received = image_received.replace(tzinfo=None)
        image = (
            WDAImage(
                request_started_at=image_started,
                received_at=image_received,
                byte_count=len(PNG),
                sha256="image-sha",
                png_bytes=PNG,
            )
            if has_image
            else None
        )
        return WDAObservation(
            active_app="app.current",
            focused_label="Search" if semantic else None,
            visible=(
                [
                    WDAElement(
                        label="Search",
                        role="SearchField",
                        value=None,
                        identifier="search",
                        focused=True,
                    )
                ]
                if semantic
                else []
            ),
            expected_app=expected_app,
            expected_label=expected_label,
            expected_app_verified=expected_app is not None and semantic,
            expected_label_verified=expected_label is not None and semantic,
            identity=self.identity,
            request_started_at=semantic_started if semantic else None,
            received_at=semantic_received if semantic else None,
            image=image,
            errors=errors,
        )

    async def press(
        self,
        key: str,
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        _ = key
        self._check_identity(expected_identity)
        self.press_calls += 1
        if self.press_error is not None:
            raise self.press_error
        return self._mutation("press")

    async def select(
        self,
        label: str,
        role: str = "Cell",
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        _ = label, role
        self._check_identity(expected_identity)
        self.select_calls += 1
        return self._mutation("select")

    async def set_text(
        self,
        text: str,
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        _ = text
        self._check_identity(expected_identity)
        self.text_calls += 1
        return self._mutation("set_text")

    async def activate(self, bundle_id: str) -> WDAMutationResult:
        _ = bundle_id
        self.activate_calls += 1
        return self._mutation("activate")

    async def aclose(self) -> None:
        self.closed = True

    def _check_identity(self, expected: WDAIdentity | None) -> None:
        if expected is not None and expected != self.identity:
            raise AssertionError("agent failed to validate helper identity before dispatch")

    def _mutation(
        self,
        operation: Literal["press", "activate", "select", "set_text"],
    ) -> WDAMutationResult:
        now = datetime.now(UTC)
        return WDAMutationResult(
            operation=operation,
            identity=self.identity,
            request_started_at=now,
            received_at=now,
        )


class StartupWDAClient(FakeWDAClient):
    def __init__(
        self,
        *,
        initial_failures: int = 1,
        device_uuid: str = PINNED_UUID,
        always_fail: bool = False,
    ) -> None:
        super().__init__(device_uuid=device_uuid)
        self.initial_failures = initial_failures
        self.always_fail = always_fail

    async def status(self) -> WDAStatus:
        if self.initial_failures > 0:
            self.initial_failures -= 1
            self.status_calls += 1
            raise OSError("helper unavailable")
        if self.always_fail:
            self.status_calls += 1
            raise OSError("helper unavailable")
        return await super().status()


class BlockingStartupWDAClient(FakeWDAClient):
    def __init__(self) -> None:
        super().__init__()
        self.entered_readiness = asyncio.Event()
        self._first = True

    async def status(self) -> WDAStatus:
        if self._first:
            self._first = False
            raise OSError("helper unavailable")
        self.entered_readiness.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class TransportFailingWDAClient(FakeWDAClient):
    async def status(self) -> WDAStatus:
        self.status_calls += 1
        raise WDAError("WDA transport failed during status", code=ErrorCode.NETWORK)


class NotReadyWDAClient(FakeWDAClient):
    async def status(self) -> WDAStatus:
        status = await super().status()
        return status.model_copy(update={"ready": False})


class FakeDiscoveryAdapter:
    def __init__(self, endpoints: list[DiscoveredEndpoint]) -> None:
        self.endpoints = endpoints
        self.discover_calls = 0
        self.closed = False

    async def discover(self) -> list[DiscoveredEndpoint]:
        self.discover_calls += 1
        return self.endpoints

    async def aclose(self) -> None:
        self.closed = True


class FakeManagedRuntime:
    def __init__(self, *, stop_fails: bool = False) -> None:
        self.state = WDARuntimeState.STOPPED
        self.pid: int | None = None
        self.mark_ready_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_fails = stop_fails

    def _status(self) -> WDARuntimeStatus:
        return WDARuntimeStatus(
            state=self.state,
            pid=self.pid,
            log_path=Path("/private/test-wda.log"),
        )

    def status(self) -> WDARuntimeStatus:
        return self._status()

    def start(self) -> WDARuntimeStatus:
        self.start_calls += 1
        self.state = WDARuntimeState.STARTING
        self.pid = 4321
        return self._status()

    def mark_ready(self) -> WDARuntimeStatus:
        self.mark_ready_calls += 1
        self.state = WDARuntimeState.READY
        return self._status()

    def stop(self) -> WDARuntimeStatus:
        self.stop_calls += 1
        if self.stop_fails:
            self.state = WDARuntimeState.FAILED
            return self._status()
        self.state = WDARuntimeState.STOPPED
        self.pid = None
        return self._status()


class BlockingStartRuntime(FakeManagedRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def start(self) -> WDARuntimeStatus:
        self.start_entered.set()
        if not self.release_start.wait(timeout=1):
            raise TimeoutError("test did not release runtime start")
        return super().start()


class FakeDirectURLAdapter:
    def __init__(self) -> None:
        self.app_launches: list[tuple[str, str]] = []
        self.content_url_attempts: list[tuple[str, str]] = []
        self.content_launches: list[tuple[str, str]] = []
        self.key_presses: list[tuple[str, str]] = []

    async def open_app(self, device_id: str, app_id: str) -> dict[str, str]:
        self.app_launches.append((device_id, app_id))
        return {"route": "open_app", "value": app_id}

    async def open_url(self, device_id: str, url: str) -> dict[str, str]:
        self.content_url_attempts.append((device_id, url))
        validated = validate_content_url(url)
        self.content_launches.append((device_id, validated.url))
        return {"route": "open_url", "value": validated.url}

    async def press_key(self, device_id: str, key: str) -> None:
        self.key_presses.append((device_id, key))

    async def aclose(self) -> None:
        return None


def _screen_config(*, mutations_enabled: bool = True) -> AgentConfig:
    return AgentConfig(
        devices=[
            AgentDevice(
                key="living-room",
                name="Living Room",
                wda_endpoint="http://wda.test:8100",
                wda_identity=PINNED_UUID,
            )
        ],
        mutations_enabled=mutations_enabled,
        max_observation_age_s=2,
    )


def _managed_screen_config() -> AgentConfig:
    return AgentConfig(
        devices=[
            AgentDevice(
                key="living-room",
                name="Living Room",
                wda_endpoint="http://wda.test:8100",
                wda_identity=PINNED_UUID,
                helper=HelperConfig(
                    udid="00000000-0000-4000-8000-000000000099",
                    backend="native",
                    bundle_id="com.example.WebDriverAgentRunner.xctrunner",
                ),
            )
        ],
        max_observation_age_s=2,
    )


def _refresh_config(endpoint: str = "http://192.168.10.20:8100") -> AgentConfig:
    return AgentConfig(
        devices=[
            AgentDevice(
                key="living-room",
                name="Living Room",
                stable_id="stable-device-id",
                wda_endpoint=endpoint,
                wda_identity=PINNED_UUID,
            )
        ],
        max_observation_age_s=2,
    )


def _agent(
    fake: FakeWDAClient,
    lease_dir: Path,
    *,
    config: AgentConfig | None = None,
    factory_hook: Callable[[], None] | None = None,
) -> AppleTVAgent:
    def factory(_endpoint: str) -> WDAClient:
        if factory_hook is not None:
            factory_hook()
        return fake

    return AppleTVAgent(config or _screen_config(), client_factory=factory, lease_dir=lease_dir)


@pytest.mark.asyncio
async def test_devices_distinguishes_unconfigured_and_external_helper_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedRuntime:
        def __init__(self, _config: object) -> None:
            raise AssertionError("devices must not doctor an unconfigured or external helper")

    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", UnexpectedRuntime)
    agent = AppleTVAgent(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="direct-only",
                    name="Direct Only",
                    stable_id="direct-stable-id",
                ),
                AgentDevice(
                    key="external",
                    name="External Helper",
                    wda_endpoint="http://wda.test:8100",
                    wda_identity="external-pinned-uuid",
                ),
            ]
        ),
        lease_dir=tmp_path,
    )
    try:
        health = {item["device"]: item["helper_profile"] for item in agent.devices()}
        assert health["direct-only"] == {
            "status": "not_configured",
            "basis": "not_configured",
            "apple_account_session": "not_checked",
            "repair_hint": "Run apple-tv-agent configure if screen control is needed.",
        }
        assert health["external"] == {
            "status": "external_unknown",
            "basis": "external_helper_unchecked",
            "apple_account_session": "not_checked",
            "repair_hint": (
                "Check the profile on the external helper host; local cached evidence "
                "is unavailable."
            ),
        }
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_devices_rechecks_cached_helper_profile_health_on_every_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_at = datetime(2026, 9, 19, 12, tzinfo=UTC)
    seven_day_expiry = checked_at + timedelta(days=7)
    renewal_expiry = checked_at + timedelta(hours=47)
    exact_boundary = checked_at + timedelta(hours=48)
    just_beyond_boundary = exact_boundary + timedelta(seconds=1)
    expired_at = checked_at - timedelta(seconds=1)
    reports = deque(
        [
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_short_lived")],
                profile_expires_at=seven_day_expiry,
                profile_valid=True,
                ready=True,
            ),
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_short_lived")],
                profile_expires_at=renewal_expiry,
                profile_valid=True,
                ready=True,
            ),
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_short_lived")],
                profile_expires_at=exact_boundary,
                profile_valid=True,
                ready=True,
            ),
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_short_lived")],
                profile_expires_at=just_beyond_boundary,
                profile_valid=True,
                ready=True,
            ),
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_expired")],
                profile_expires_at=expired_at,
                profile_valid=False,
                ready=False,
            ),
            SimpleNamespace(
                issues=[SimpleNamespace(code="profile_expired")],
                profile_expires_at=None,
                profile_valid=False,
                ready=True,
            ),
        ]
    )
    doctor_calls = 0

    class FreshRuntime:
        def __init__(self, _config: object) -> None:
            pass

        def doctor(self) -> object:
            nonlocal doctor_calls
            doctor_calls += 1
            return reports.popleft()

    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", FreshRuntime)
    agent = AppleTVAgent(
        _managed_screen_config(),
        lease_dir=tmp_path,
        now=lambda: checked_at,
    )
    try:
        health = [agent.devices()[0]["helper_profile"] for _ in range(6)]
    finally:
        await agent.aclose()

    assert [item["status"] for item in health] == [
        "valid",
        "renewal_due",
        "renewal_due",
        "valid",
        "expired",
        "unknown",
    ]
    assert doctor_calls == 6
    assert health[0] == {
        "status": "valid",
        "basis": "configured_cached_profile",
        "apple_account_session": "not_checked",
        "expires_at": seven_day_expiry.isoformat(),
    }
    assert health[1]["expires_at"] == renewal_expiry.isoformat()
    assert health[2]["expires_at"] == exact_boundary.isoformat()
    assert health[3]["expires_at"] == just_beyond_boundary.isoformat()
    assert health[4]["expires_at"] == expired_at.isoformat()
    assert "apple-tv-agent skill renewal flow" in health[4]["repair_hint"]
    assert "expires_at" not in health[5]
    assert all(item["apple_account_session"] == "not_checked" for item in health)


@pytest.mark.asyncio
async def test_old_producer_timestamp_is_not_freshened_on_return(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["old_semantic"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room")
        assert "semantic_timestamps_invalid" in observed.observed.errors
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        assert caught.value.details["reason"] == "stale_observation"
        assert fake.press_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "missing_start",
        "missing_received",
        "naive_semantic",
        "future_semantic",
        "reversed_semantic",
    ],
)
async def test_invalid_semantic_intervals_cannot_authorize_actions_or_postconditions(
    tmp_path: Path,
    kind: ObservationKind,
) -> None:
    fake = FakeWDAClient(observation_kinds=[kind])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe(
            "living-room",
            expected_app="app.current",
            expected_label="Search",
        )
        assert "semantic_timestamps_invalid" in observed.observed.errors
        assert observed.observed.request_started_at is None
        assert observed.observed.received_at is None
        assert observed.observed.active_app is None
        assert observed.observed.focused_label is None
        assert observed.observed.visible == []
        assert observed.observed.expected_app_verified is False
        assert observed.observed.expected_label_verified is False

        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        assert caught.value.details["reason"] == "stale_observation"
        assert fake.press_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_invalid_semantics_preserve_independently_valid_image_for_press(
    tmp_path: Path,
) -> None:
    fake = FakeWDAClient(observation_kinds=["naive_semantic"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe(
            "living-room",
            include_image=True,
            expected_app="app.current",
            expected_label="Search",
        )
        assert observed.observed.image is not None
        assert observed.observed.visible == []
        assert observed.observed.expected_app_verified is False
        assert observed.observed.expected_label_verified is False

        result = await agent.act(
            "living-room",
            action="press",
            value="down",
            observation_id=observed.observation_id,
            include_image=True,
            expected_label="Search",
        )
        assert fake.press_calls == 1
        assert result["outcome"] == "unverified"
        assert result["after"].observed.image is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_invalid_image_interval_is_discarded_without_losing_semantics(
    tmp_path: Path,
) -> None:
    fake = FakeWDAClient(observation_kinds=["naive_image"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room", include_image=True)
        assert observed.observed.image is None
        assert "image_timestamps_invalid" in observed.observed.warnings
        assert observed.observed.errors == []
        assert observed.observed.visible

        await agent.act(
            "living-room",
            action="select",
            value="Search",
            role="SearchField",
            observation_id=observed.observation_id,
        )
        assert fake.select_calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_image_only_observation_allows_press_navigation(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["image_error", "image_error"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room", include_image=True)
        assert observed.observed.image is not None
        assert observed.observed.errors
        result = await agent.act(
            "living-room",
            action="press",
            value="down",
            observation_id=observed.observation_id,
            include_image=True,
        )
        assert fake.press_calls == 1
        assert result["outcome"] == "unverified"
        assert result["after"].observed.image is not None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["select", "type"])
async def test_image_only_observation_cannot_authorize_semantic_action(
    tmp_path: Path,
    action: str,
) -> None:
    fake = FakeWDAClient(observation_kinds=["image_error"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room", include_image=True)
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act(
                "living-room",
                action=action,
                value="Search",
                observation_id=observed.observation_id,
            )
        assert caught.value.details["reason"] == "semantic_observation_required"
        assert fake.select_calls == 0
        assert fake.text_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_semantic_failure_without_image_produces_no_action_token(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["error"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room")
        assert "semantic_observation_failed" in observed.observed.errors
        assert "semantic_timestamps_invalid" in observed.observed.errors
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        assert caught.value.details["reason"] == "stale_observation"
        assert fake.press_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_helper_restart_invalidates_observation_before_mutation(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["semantic"])
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room")
        fake.identity = _identity(generation=1, session_id="session-2")
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        assert caught.value.details["reason"] == "helper_restarted"
        assert fake.press_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_persistent_client_is_reused_across_observations(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["semantic", "semantic"])
    factory_calls = 0

    def factory_hook() -> None:
        nonlocal factory_calls
        factory_calls += 1

    agent = _agent(fake, tmp_path, factory_hook=factory_hook)
    try:
        first = await agent.observe("living-room")
        second = await agent.observe("living-room")
        assert first.observation_id != second.observation_id
        assert factory_calls == 1
        assert fake.identity_calls == 2
        assert fake.observe_calls == 2
    finally:
        await agent.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_stale_private_ip_refreshes_by_stable_identifier_alias(tmp_path: Path) -> None:
    old_endpoint = "http://192.168.10.20:8100"
    new_endpoint = "http://192.168.10.55:8100"
    stale = TransportFailingWDAClient()
    refreshed = FakeWDAClient()
    factory_calls: list[str] = []

    def factory(endpoint: str) -> WDAClient:
        factory_calls.append(endpoint)
        if endpoint == old_endpoint:
            return stale
        if endpoint == new_endpoint:
            return refreshed
        raise AssertionError(endpoint)

    discovered = DiscoveredEndpoint(
        device_id="alternate-device-id",
        kind=DeviceKind.APPLE_TV,
        name="Untrusted Display Name",
        address="192.168.10.55",
        raw={"all_identifiers": ["STABLE-DEVICE-ID", "other-alias"]},
    )
    adapter = FakeDiscoveryAdapter([discovered])
    config = _refresh_config(old_endpoint)
    agent = AppleTVAgent(config, client_factory=factory, lease_dir=tmp_path)
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        observed = await agent.observe("living-room")
        assert observed.observed.visible
        assert factory_calls == [old_endpoint, new_endpoint]
        assert adapter.discover_calls == 1
        assert stale.closed is True
        assert refreshed.closed is False
        assert refreshed.identity_calls == 1
        assert config.devices[0].wda_endpoint == old_endpoint
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_stale_private_ip_never_refreshes_from_name_only_match(tmp_path: Path) -> None:
    old_endpoint = "http://192.168.10.20:8100"
    stale = TransportFailingWDAClient()
    factory_calls: list[str] = []

    def factory(endpoint: str) -> WDAClient:
        factory_calls.append(endpoint)
        return stale

    discovered = DiscoveredEndpoint(
        device_id="different-device-id",
        kind=DeviceKind.APPLE_TV,
        name="stable-device-id",
        address="192.168.10.55",
        raw={"all_identifiers": ["different-alias"]},
    )
    adapter = FakeDiscoveryAdapter([discovered])
    agent = AppleTVAgent(
        _refresh_config(old_endpoint),
        client_factory=factory,
        lease_dir=tmp_path,
    )
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(WDAError, match="transport failed"):
            await agent.observe("living-room")
        assert adapter.discover_calls == 1
        assert factory_calls == [old_endpoint]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_nontransport_status_failure_never_triggers_endpoint_discovery(
    tmp_path: Path,
) -> None:
    endpoint = "http://192.168.10.20:8100"
    not_ready = NotReadyWDAClient()
    adapter = FakeDiscoveryAdapter(
        [
            DiscoveredEndpoint(
                device_id="stable-device-id",
                kind=DeviceKind.APPLE_TV,
                name="Living Room",
                address="192.168.10.55",
            )
        ]
    )
    agent = AppleTVAgent(
        _refresh_config(endpoint),
        client_factory=lambda _endpoint: not_ready,
        lease_dir=tmp_path,
    )
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigError, match="not ready"):
            await agent.observe("living-room")
        assert adapter.discover_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_refreshed_endpoint_wrong_uuid_blocks_before_mutation(tmp_path: Path) -> None:
    old_endpoint = "http://192.168.10.20:8100"
    new_endpoint = "http://192.168.10.55:8100"
    stale = TransportFailingWDAClient()
    wrong_device = FakeWDAClient(device_uuid="wrong-device-uuid")

    def factory(endpoint: str) -> WDAClient:
        if endpoint == old_endpoint:
            return stale
        if endpoint == new_endpoint:
            return wrong_device
        raise AssertionError(endpoint)

    adapter = FakeDiscoveryAdapter(
        [
            DiscoveredEndpoint(
                device_id="STABLE-DEVICE-ID",
                kind=DeviceKind.APPLE_TV,
                name="Any Name",
                address="192.168.10.55",
            )
        ]
    )
    agent = AppleTVAgent(
        _refresh_config(old_endpoint),
        client_factory=factory,
        lease_dir=tmp_path,
    )
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act("living-room", action="launch", value="com.example.App")
        assert caught.value.details["reason"] == "device_identity_mismatch"
        assert wrong_device.activate_calls == 0
        assert wrong_device.closed is True
        assert agent._clients["living-room"] is stale
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_managed_helper_starts_once_before_private_ip_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_endpoint = "http://192.168.10.20:8100"
    new_endpoint = "http://192.168.10.55:8100"
    stale = TransportFailingWDAClient()
    refreshed = FakeWDAClient()
    runtime = FakeManagedRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)

    def factory(endpoint: str) -> WDAClient:
        if endpoint == old_endpoint:
            return stale
        if endpoint == new_endpoint:
            return refreshed
        raise AssertionError(endpoint)

    config = AgentConfig(
        devices=[
            AgentDevice(
                key="living-room",
                name="Living Room",
                stable_id="stable-device-id",
                wda_endpoint=old_endpoint,
                wda_identity=PINNED_UUID,
                helper=HelperConfig(
                    udid="00000000-0000-4000-8000-000000000099",
                    backend="native",
                    bundle_id="com.example.WebDriverAgentRunner.xctrunner",
                ),
            )
        ]
    )
    adapter = FakeDiscoveryAdapter(
        [
            DiscoveredEndpoint(
                device_id="stable-device-id",
                kind=DeviceKind.APPLE_TV,
                name="Living Room",
                address="192.168.10.55",
            )
        ]
    )
    agent = AppleTVAgent(config, client_factory=factory, lease_dir=tmp_path)
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        observed = await agent.observe("living-room")
        assert observed.observed.visible
        assert runtime.start_calls == 1
        assert runtime.mark_ready_calls == 1
        assert adapter.discover_calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["http://localhost:8100", "http://127.0.0.1:8100"],
)
async def test_local_wda_endpoint_is_never_rewritten(
    tmp_path: Path,
    endpoint: str,
) -> None:
    stale = TransportFailingWDAClient()
    factory_calls: list[str] = []

    def factory(created_endpoint: str) -> WDAClient:
        factory_calls.append(created_endpoint)
        return stale

    adapter = FakeDiscoveryAdapter(
        [
            DiscoveredEndpoint(
                device_id="stable-device-id",
                kind=DeviceKind.APPLE_TV,
                name="Living Room",
                address="192.168.10.55",
            )
        ]
    )
    agent = AppleTVAgent(
        _refresh_config(endpoint),
        client_factory=factory,
        lease_dir=tmp_path,
    )
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(WDAError, match="transport failed"):
            await agent.observe("living-room")
        assert adapter.discover_calls == 0
        assert factory_calls == [endpoint]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_ambiguous_mutation_consumes_token_and_is_never_repeated(tmp_path: Path) -> None:
    fake = FakeWDAClient(observation_kinds=["semantic"])
    fake.press_error = WDAMutationUncertain("press")
    agent = _agent(fake, tmp_path)
    try:
        observed = await agent.observe("living-room")
        with pytest.raises(WDAMutationUncertain):
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        with pytest.raises(SafetyBlockedError) as repeated:
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=observed.observation_id,
            )
        assert repeated.value.details["reason"] == "stale_observation"
        assert fake.press_calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_post_action_observation_error_stays_unverified_and_not_reusable(
    tmp_path: Path,
) -> None:
    fake = FakeWDAClient(observation_kinds=["semantic", "error"])
    agent = _agent(fake, tmp_path)
    try:
        before = await agent.observe("living-room")
        result = await agent.act(
            "living-room",
            action="press",
            value="down",
            observation_id=before.observation_id,
            expected_label="Destination",
        )
        assert result["outcome"] == "unverified"
        after = result["after"]
        assert after.observed.errors
        with pytest.raises(SafetyBlockedError):
            await agent.act(
                "living-room",
                action="press",
                value="down",
                observation_id=after.observation_id,
            )
        assert fake.press_calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_device_uuid_mismatch_blocks_even_observation_free_launch(tmp_path: Path) -> None:
    fake = FakeWDAClient(device_uuid="different-device-uuid")
    agent = _agent(fake, tmp_path)
    try:
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.act("living-room", action="launch", value="com.example.app")
        assert caught.value.details["reason"] == "device_identity_mismatch"
        assert fake.activate_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_started_helper_is_marked_ready_only_after_pinned_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = StartupWDAClient()
    runtime = FakeManagedRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    agent = _agent(fake, tmp_path, config=_managed_screen_config())
    try:
        await agent.observe("living-room")

        assert fake.identity_calls == 1
        assert runtime.mark_ready_calls == 1
        assert runtime.stop_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_started_helper_identity_mismatch_never_marks_ready_and_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = StartupWDAClient(device_uuid="wrong-device")
    runtime = FakeManagedRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    agent = _agent(fake, tmp_path, config=_managed_screen_config())

    with pytest.raises(SafetyBlockedError) as caught:
        await agent.observe("living-room")

    assert caught.value.details["reason"] == "device_identity_mismatch"
    assert runtime.mark_ready_calls == 0
    assert runtime.stop_calls == 1
    assert agent._runtime == {}
    await agent.aclose()


@pytest.mark.asyncio
async def test_helper_readiness_timeout_is_end_to_end_bounded_and_stops_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = StartupWDAClient(always_fail=True)
    runtime = FakeManagedRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    monkeypatch.setattr("home_media.agent_control.HELPER_STARTUP_TIMEOUT_S", 0.01)
    monkeypatch.setattr("home_media.agent_control.HELPER_POLL_INTERVAL_S", 0.001)
    agent = _agent(fake, tmp_path, config=_managed_screen_config())

    with pytest.raises(ConfigError, match="did not become ready"):
        await agent.observe("living-room")

    assert runtime.mark_ready_calls == 0
    assert runtime.stop_calls == 1
    assert agent._runtime == {}
    await agent.aclose()


@pytest.mark.asyncio
async def test_cancelled_helper_startup_stops_child_before_propagating_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = BlockingStartupWDAClient()
    runtime = FakeManagedRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    agent = _agent(fake, tmp_path, config=_managed_screen_config())
    task = asyncio.create_task(agent.observe("living-room"))
    await asyncio.wait_for(fake.entered_readiness.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.mark_ready_calls == 0
    assert runtime.stop_calls == 1
    assert agent._runtime == {}
    await agent.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_threaded_process_start_cannot_detach_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = StartupWDAClient()
    runtime = BlockingStartRuntime()
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    agent = _agent(fake, tmp_path, config=_managed_screen_config())
    task = asyncio.create_task(agent.observe("living-room"))
    entered = await asyncio.to_thread(runtime.start_entered.wait, 1)
    assert entered is True

    task.cancel()
    runtime.release_start.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.stop_calls == 1
    assert agent._runtime == {}
    await agent.aclose()


def test_agent_config_rejects_duplicate_helper_udids_case_insensitively() -> None:
    helper = HelperConfig(
        udid="00000000-0000-4000-8000-0000000000AA",
        backend="native",
        bundle_id="com.example.WebDriverAgentRunner.xctrunner",
    )
    duplicate = helper.model_copy(update={"udid": helper.udid.lower()})

    with pytest.raises(ValueError, match="helper.udid"):
        AgentConfig(
            devices=[
                AgentDevice(
                    key="first",
                    name="First",
                    stable_id="stable-first",
                    helper=helper,
                ),
                AgentDevice(
                    key="second",
                    name="Second",
                    stable_id="stable-second",
                    helper=duplicate,
                ),
            ]
        )


@pytest.mark.asyncio
async def test_aclose_retains_device_ownership_when_helper_will_not_stop(
    tmp_path: Path,
) -> None:
    agent = _agent(FakeWDAClient(), tmp_path, config=_managed_screen_config())
    device = agent.device("living-room")
    agent._lease(device)
    runtime = FakeManagedRuntime(stop_fails=True)
    agent._runtime[device.key] = runtime

    with pytest.raises(ConfigError, match="ownership is retained"):
        await agent.aclose()

    assert device.key in agent._runtime
    assert device.key in agent._leases
    assert agent._leases[device.key].fd is not None

    runtime.stop_fails = False
    await agent.aclose()
    assert agent._runtime == {}
    assert agent._leases == {}


@pytest.mark.asyncio
async def test_device_lease_refuses_second_owner_and_releases_on_close(tmp_path: Path) -> None:
    first = _agent(FakeWDAClient(), tmp_path)
    second = _agent(FakeWDAClient(), tmp_path)
    device = first.device("living-room")
    try:
        first._lease(device)
        with pytest.raises(SafetyBlockedError) as caught:
            second._lease(second.device("living-room"))
        assert caught.value.details["reason"] == "device_in_use"
        await first.aclose()
        second._lease(second.device("living-room"))
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        (
            "https://apps.apple.com/us/app/vlc-media-player/id650377962?platform=tv",
            "https://apps.apple.com/us/app/vlc-media-player/id650377962?platform=tv",
        ),
        (
            "https://apps.apple.com/app/vlc-media-player/id650377962/",
            "https://apps.apple.com/app/vlc-media-player/id650377962",
        ),
    ],
)
async def test_direct_open_url_launches_only_validated_app_store_detail_url(
    tmp_path: Path,
    url: str,
    canonical: str,
) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")]
    )
    agent = _agent(FakeWDAClient(), tmp_path, config=config)
    adapter = FakeDirectURLAdapter()
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        result = await agent.direct("living-room", "open_url", url)
        assert result == {"route": "open_app", "value": canonical}
        assert adapter.app_launches == [("stable-device-id", canonical)]
        assert adapter.content_url_attempts == []
        assert adapter.content_launches == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://apps.apple.com/us/app/vlc-media-player/id650377962",
        "https://user@apps.apple.com/us/app/vlc-media-player/id650377962",
        "https://apps.apple.com:443/us/app/vlc-media-player/id650377962",
        "https://apps.apple.com:/us/app/vlc-media-player/id650377962",
        "https://apps.apple.com/us/app/vlc-media-player/id650377962#details",
        "https://apps.apple.com/us/app/vlc-media-player/id650377962#",
        "https://apps.apple.com.evil.example/us/app/vlc-media-player/id650377962",
        "https://apps.apple.com/us/app/vlc-media-player/not-an-id",
        "https://apps.apple.com/us/app/vlc-media-player/idnotdigits",
        "https://apps.apple.com/us/app/vlc-media-player/id650377962?",
        "https://apps.apple.com/us/app/vlc-media-player/id650377962?platform=tv&ref=evil",
    ],
)
async def test_direct_open_url_rejects_malformed_app_store_urls_without_dispatch(
    tmp_path: Path,
    url: str,
) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")]
    )
    agent = _agent(FakeWDAClient(), tmp_path, config=config)
    adapter = FakeDirectURLAdapter()
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(UnsupportedError):
            await agent.direct("living-room", "open_url", url)
        assert adapter.app_launches == []
        assert adapter.content_url_attempts == [("stable-device-id", url)]
        assert adapter.content_launches == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_open_url_keeps_existing_provider_validation_route(tmp_path: Path) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")]
    )
    agent = _agent(FakeWDAClient(), tmp_path, config=config)
    adapter = FakeDirectURLAdapter()
    agent.adapter = adapter  # type: ignore[assignment]
    url = "https://www.netflix.com/title/81234567"
    try:
        result = await agent.direct("living-room", "open_url", url)
        assert result == {"route": "open_url", "value": url}
        assert adapter.app_launches == []
        assert adapter.content_url_attempts == [("stable-device-id", url)]
        assert adapter.content_launches == [("stable-device-id", url)]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_press_dispatches_exact_key_and_invalidates_observation(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")]
    )
    fake_wda = FakeWDAClient()
    agent = _agent(fake_wda, tmp_path, config=config)
    adapter = FakeDirectURLAdapter()
    agent.adapter = adapter  # type: ignore[assignment]
    agent._observations["living-room"] = AgentObservation(
        observation_id="old-observation",
        device="living-room",
        observed=await fake_wda.observe(),
        obtained_monotonic=0,
    )
    try:
        result = await agent.direct("living-room", "press", "Home")
        assert result == {"sent": True, "verified": False}
        assert adapter.key_presses == [("stable-device-id", "Home")]
        assert "living-room" not in agent._observations
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_press_kill_switch_blocks_before_dispatch(tmp_path: Path) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")],
        mutations_enabled=False,
    )
    agent = _agent(FakeWDAClient(), tmp_path, config=config)
    adapter = FakeDirectURLAdapter()
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.direct("living-room", "press", "Home")
        assert caught.value.details["reason"] == "mutations_disabled"
        assert adapter.key_presses == []
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_direct_press_uses_existing_adapter_key_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")]
    )
    agent = _agent(FakeWDAClient(), tmp_path, config=config)
    resolve_calls = 0

    async def fail_if_resolved(_device_id: str) -> None:
        nonlocal resolve_calls
        resolve_calls += 1
        raise AssertionError("invalid keys must fail before device resolution")

    monkeypatch.setattr(agent.adapter, "_resolve_config", fail_if_resolved)
    try:
        with pytest.raises(UnsupportedError, match="Unknown key"):
            await agent.direct("living-room", "press", "not-a-remote-key")
        assert resolve_calls == 0
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_kill_switch_blocks_direct_mutation_before_adapter_call(tmp_path: Path) -> None:
    config = AgentConfig(
        devices=[AgentDevice(key="living-room", name="Living Room", stable_id="stable-device-id")],
        mutations_enabled=False,
    )
    fake = FakeWDAClient()
    agent = _agent(fake, tmp_path, config=config)
    try:
        with pytest.raises(SafetyBlockedError) as caught:
            await agent.direct("living-room", "wake")
        assert caught.value.details["reason"] == "mutations_disabled"
    finally:
        await agent.aclose()


def test_mutation_uncertain_is_nonretryable_partial_error() -> None:
    error = WDAMutationUncertain("press")
    assert error.code is ErrorCode.PARTIAL
    assert error.retryable is False


@pytest.mark.asyncio
async def test_endpoint_can_return_to_original_address_after_refresh(tmp_path: Path) -> None:
    class MovingClient(FakeWDAClient):
        unavailable = False

        async def status(self) -> WDAStatus:
            if self.unavailable:
                raise WDAError("moved", code=ErrorCode.NETWORK)
            return await super().status()

    original = "http://192.168.10.20:8100"
    moved = "http://192.168.10.55:8100"
    stale = TransportFailingWDAClient()
    intermediate = MovingClient()
    returned = FakeWDAClient()
    clients = deque([stale, intermediate, returned])
    addresses: list[str] = []

    def factory(endpoint: str) -> WDAClient:
        addresses.append(endpoint)
        return clients.popleft()

    adapter = FakeDiscoveryAdapter(
        [
            DiscoveredEndpoint(
                device_id="stable-device-id",
                kind=DeviceKind.APPLE_TV,
                name="TV",
                address="192.168.10.55",
            )
        ]
    )
    agent = AppleTVAgent(_refresh_config(original), client_factory=factory, lease_dir=tmp_path)
    agent.adapter = adapter  # type: ignore[assignment]
    try:
        await agent.observe("living-room")
        intermediate.unavailable = True
        adapter.endpoints[0].address = "192.168.10.20"
        await agent.observe("living-room")
        assert addresses == [original, moved, original]
        assert returned.identity_calls == 1
        assert intermediate.closed
        assert agent.config.devices[0].wda_endpoint == original
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_cancelled_start_does_not_stop_already_running_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BlockingStartRuntime()
    runtime.pid = 4321
    runtime.state = WDARuntimeState.READY
    monkeypatch.setattr("home_media.wda_runtime.WDARuntime", lambda _config: runtime)
    agent = _agent(StartupWDAClient(), tmp_path, config=_managed_screen_config())
    task = asyncio.create_task(agent.observe("living-room"))
    assert await asyncio.to_thread(runtime.start_entered.wait, 1)
    task.cancel()
    runtime.release_start.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.stop_calls == 0
    assert agent._runtime["living-room"] is runtime
    await agent.aclose()
    assert runtime.stop_calls == 1


@pytest.mark.asyncio
async def test_direct_and_visual_routes_share_normalized_physical_device_lease(
    tmp_path: Path,
) -> None:
    visual = AppleTVAgent(_refresh_config(), lease_dir=tmp_path)
    direct = AppleTVAgent(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="same-tv",
                    name="Other name",
                    stable_id="STABLE-DEVICE-ID",
                )
            ]
        ),
        lease_dir=tmp_path,
    )
    try:
        visual._lease(visual.device("living-room"))
        with pytest.raises(SafetyBlockedError):
            direct._lease(direct.device("same-tv"))
        await visual.aclose()
        direct._lease(direct.device("same-tv"))
    finally:
        await visual.aclose()
        await direct.aclose()
