"""Tests for the authenticated Siri broker and parse-only Fast fallback."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from home_media.broker import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    BrokerAsyncRuntime,
    BrokerCommand,
    BrokerCore,
    BrokerHTTPApplication,
    BrokerIdempotencyStore,
    BrokerRequest,
    BrokerResponse,
    CodexFastIntentParser,
    DeterministicIntentParser,
    IntentNotUnderstood,
    RoomCatalog,
    RoomEntry,
    _prepare_response,
)
from home_media.content.prepare import PrepareContentResult, TerminalStatus
from home_media.content.router import ContentGoal
from home_media.models import ActionResult, ExecutionStatus, VerificationStatus
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult


def _rooms() -> RoomCatalog:
    return RoomCatalog(
        [
            RoomEntry("living_room", "Living Room", ("the den", "living room tv")),
            RoomEntry("theater", "Theater", ("workout room", "theater tv")),
        ]
    )


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        (
            "Turn on the Living Room TV and pull up Breaking Bad on Netflix",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "breaking bad",
                "provider": "netflix",
                "goal": "title_open",
            },
        ),
        (
            "Resume where I'm at in Archer on Hulu in Living Room",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "archer",
                "provider": "hulu",
                "goal": "resume",
            },
        ),
        (
            "Search Netflix for Avatar The Last Airbender in the den",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "avatar the last airbender",
                "provider": "netflix",
                "goal": "search_ready",
            },
        ),
        (
            "Set the volume to 22 in Living Room",
            {"action": "set_volume", "room": "living_room", "level": 22},
        ),
        (
            "Wake up the Theater TV",
            {"action": "power_on", "room": "theater"},
        ),
        (
            "Turn on LivingRoom Apple TV and pull up Severance on Apple TV Plus",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "severance",
                "provider": "apple_tv",
                "goal": "title_open",
            },
        ),
        (
            "Play Mad Max in Living Room",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "mad max",
                "provider": None,
                "goal": "resume",
            },
        ),
        (
            "Search for Google Play Movies in Living Room",
            {
                "action": "prepare_content",
                "room": "living_room",
                "title": "google play movies",
                "provider": None,
                "goal": "search_ready",
            },
        ),
    ],
)
async def test_deterministic_parser_handles_primary_siri_phrases(
    utterance: str, expected: dict[str, Any]
) -> None:
    command = await DeterministicIntentParser(_rooms()).parse(utterance)

    for key, value in expected.items():
        assert getattr(command, key) == value
    assert command.parser == "deterministic"


@pytest.mark.parametrize(
    "utterance",
    [
        "Turn off the Living Room TV",
        "Press down twice and click select in Living Room",
        "Play Archer",
        "Play Archer in Living Room and Theater",
        "Set the volume to 101 in Living Room",
        "Play Archer and set the volume to 20 in Living Room",
        "Turn on Living Room and set the volume to 20",
    ],
)
async def test_deterministic_parser_rejects_destructive_raw_or_ambiguous_requests(
    utterance: str,
) -> None:
    with pytest.raises(IntentNotUnderstood):
        await DeterministicIntentParser(_rooms()).parse(utterance)


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: ContentGoal | str = ContentGoal.SEARCH_READY,
        wake: bool = True,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult:
        self.calls.append(
            {
                "action": "prepare_content",
                "room": room_name,
                "title": title,
                "provider": provider,
                "goal": str(goal),
                "wake": wake,
                "idempotency_key": idempotency_key,
            }
        )
        return PrepareContentResult(
            room_key=room_name,
            title=title,
            normalized_title=title.casefold(),
            provider=provider,
            goal=ContentGoal(goal),
            terminal_status=TerminalStatus.QUERY_VERIFIED,
            verification_status="verified",
            selected_result=False,
            playback_started=False,
            idempotency_key=idempotency_key,
        )

    async def set_power(
        self,
        room_name: str,
        state: str,
        *,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        self.calls.append(
            {
                "action": "power_on",
                "room": room_name,
                "state": state,
                "idempotency_key": idempotency_key,
            }
        )
        return ActionResult(
            requested_intent="power.on",
            room_key=room_name,
            execution_status=ExecutionStatus.SUCCEEDED,
            verification_status=VerificationStatus.VERIFIED,
            idempotency_key=idempotency_key,
        )

    async def set_volume(
        self,
        room_name: str,
        level: int,
        *,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        self.calls.append(
            {
                "action": "set_volume",
                "room": room_name,
                "level": level,
                "idempotency_key": idempotency_key,
            }
        )
        return ActionResult(
            requested_intent="volume.set",
            room_key=room_name,
            execution_status=ExecutionStatus.SUCCEEDED,
            verification_status=VerificationStatus.VERIFIED,
            idempotency_key=idempotency_key,
        )

    async def aclose(self) -> None:
        self.closed = True


class _FakeScreenshotService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def capture_room(
        self, room: str, *, save: bool, prune: bool
    ) -> ScreenshotResult:
        assert save is False
        assert prune is False
        self.calls.append(room)
        return ScreenshotResult(
            device_id="fake-atv",
            room_key=room,
            blank_or_protected=False,
            png_bytes=b"png",
            metadata=CaptureMetadata(worker_reused=True),
        )

    def health_snapshot(self) -> dict[str, object]:
        return {"status": "configured", "worker": {"persistent": True}}


class _RecoveringScreenshotService(_FakeScreenshotService):
    def __init__(self) -> None:
        super().__init__()
        self.status = "configured"

    async def capture_room(
        self, room: str, *, save: bool, prune: bool
    ) -> ScreenshotResult:
        assert save is False
        assert prune is False
        self.calls.append(room)
        if len(self.calls) == 1:
            self.status = "degraded"
            return ScreenshotResult(
                device_id="fake-atv",
                room_key=room,
                error="capture_failed",
                metadata=CaptureMetadata(worker_reused=False),
            )
        self.status = "ready"
        return ScreenshotResult(
            device_id="fake-atv",
            room_key=room,
            blank_or_protected=False,
            png_bytes=b"png",
            metadata=CaptureMetadata(worker_reused=True),
        )

    def health_snapshot(self) -> dict[str, object]:
        return {"status": self.status, "worker": {"persistent": True}}


class _FlappingScreenshotService(_FakeScreenshotService):
    def __init__(self) -> None:
        super().__init__()
        self.status = "configured"

    async def capture_room(
        self, room: str, *, save: bool, prune: bool
    ) -> ScreenshotResult:
        result = await super().capture_room(room, save=save, prune=prune)
        self.status = "ready"
        return result

    def health_snapshot(self) -> dict[str, object]:
        return {"status": self.status, "worker": {"persistent": True}}


def _request(
    utterance: str = "Search Netflix for Avatar in Living Room",
    key: str = "broker-test-001",
) -> BrokerRequest:
    return BrokerRequest(
        schema_version=1,
        utterance=utterance,
        idempotency_key=key,
    )


async def test_core_dispatches_only_semantic_service_method_and_caches_retry(
    tmp_path: Path,
) -> None:
    service = _FakeService()
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )

    first = await core.execute(_request())
    second = await core.execute(_request())

    assert first.ok is True
    assert second == first
    assert service.calls == [
        {
            "action": "prepare_content",
            "room": "living_room",
            "title": "avatar",
            "provider": "netflix",
            "goal": "search_ready",
            "wake": True,
            "idempotency_key": "broker-test-001",
        }
    ]
    assert (tmp_path / "ledger.json").stat().st_mode & 0o077 == 0


def test_broker_speaks_verified_resume_then_pause_outcome() -> None:
    broker_command = BrokerCommand(
        action="prepare_content",
        room="living_room",
        title="Avatar",
        provider="netflix",
        goal="resume",
        parser="deterministic",
    )
    result = PrepareContentResult(
        room_key="living_room",
        title="Avatar",
        normalized_title="avatar",
        provider="netflix",
        goal=ContentGoal.RESUME,
        terminal_status=TerminalStatus.PLAYBACK_PAUSED_VERIFIED,
        verification_status="verified",
        selected_result=True,
        playback_started=True,
    )

    response = _prepare_response(broker_command, result, 0.0)

    assert response.ok is True
    assert "ready and paused" in response.spoken_response
    assert response.data is not None
    assert response.data.terminal_status == "playback_paused_verified"


async def test_same_key_with_different_command_is_rejected(tmp_path: Path) -> None:
    service = _FakeService()
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    await core.execute(_request())

    response = await core.execute(_request("Search Netflix for Dark in Living Room"))

    assert response.error is not None
    assert response.error.code == "idempotency_conflict"
    assert len(service.calls) == 1


class _FakeSubmitter:
    def __init__(self) -> None:
        self.requests: list[BrokerRequest] = []

    def submit(self, request: BrokerRequest, *, timeout: float) -> BrokerResponse:
        assert timeout == DEFAULT_HTTP_TIMEOUT_SECONDS
        self.requests.append(request)
        return BrokerResponse(ok=True, status="verified", spoken_response="Done.")


class _HealthSubmitter(_FakeSubmitter):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status

    def health(self) -> dict[str, object]:
        return {"status": self.status, "observer": {"status": self.status}}


def _http_body(**updates: object) -> bytes:
    value: dict[str, object] = {
        "schema_version": 1,
        "utterance": "Wake up Living Room",
        "idempotency_key": "shortcut-12345678",
    }
    value.update(updates)
    return json.dumps(value).encode()


def test_http_requires_bearer_auth_before_parsing_or_dispatch() -> None:
    submitter = _FakeSubmitter()
    app = BrokerHTTPApplication("x" * 32, submitter)

    reply = app.handle(
        "POST",
        "/v1/intent",
        {"content-type": "application/json"},
        _http_body(),
    )

    assert reply.status == 401
    assert submitter.requests == []
    assert json.loads(reply.body)["error"]["code"] == "unauthorized"


def test_http_accepts_one_bounded_request_and_returns_json() -> None:
    submitter = _FakeSubmitter()
    app = BrokerHTTPApplication("x" * 32, submitter)

    reply = app.handle(
        "POST",
        "/v1/intent",
        {
            "authorization": f"Bearer {'x' * 32}",
            "content-type": "application/json; charset=utf-8",
        },
        _http_body(),
    )

    assert reply.status == 200
    assert len(submitter.requests) == 1
    assert json.loads(reply.body)["spoken_response"] == "Done."
    assert ("Cache-Control", "no-store") in reply.headers


@pytest.mark.parametrize("status", ["degraded", "warming"])
def test_http_health_ok_requires_ready_runtime(status: str) -> None:
    app = BrokerHTTPApplication("x" * 32, _HealthSubmitter(status))

    reply = app.handle("GET", "/healthz", {}, b"")

    payload = json.loads(reply.body)
    assert reply.status == 200
    assert payload["status"] == status
    assert payload["ok"] is False


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (_http_body(command="press select"), "application/json"),
        (
            b'{"schema_version":1,"schema_version":1,"utterance":"wake Living Room",'
            b'"idempotency_key":"shortcut-12345678"}',
            "application/json",
        ),
        (_http_body(), "text/plain"),
        (b"[]", "application/json"),
    ],
)
def test_http_rejects_extra_duplicate_nonobject_and_wrong_content_type(
    body: bytes, content_type: str
) -> None:
    submitter = _FakeSubmitter()
    app = BrokerHTTPApplication("x" * 32, submitter)

    reply = app.handle(
        "POST",
        "/v1/intent",
        {"authorization": f"Bearer {'x' * 32}", "content-type": content_type},
        body,
    )

    assert reply.status in {400, 415}
    assert submitter.requests == []


async def test_fast_parser_is_parse_only_and_explicitly_uses_fast_not_priority() -> None:
    captured: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["input"] = kwargs["input"]
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "action": "prepare_content",
                    "room": "living_room",
                    "title": "Archer",
                    "provider": "hulu",
                    "goal": "resume",
                    "level": None,
                    "wake": True,
                    "confidence": 0.98,
                    "reason": "clear request",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    parser = CodexFastIntentParser(_rooms(), executable="/opt/codex", runner=runner)
    command = await parser.parse("Put the thing I was watching in the den back on")

    assert command.parser == "codex_fast"
    assert command.room == "living_room"
    argv = captured["argv"]
    assert "gpt-5.6-sol" in argv
    assert 'model_reasoning_effort="low"' in argv
    assert 'service_tier="fast"' in argv
    assert not any("priority" in value for value in argv)
    assert "--ephemeral" in argv
    assert "--sandbox" in argv and "read-only" in argv


async def test_fast_parser_rejects_unconfigured_room_even_at_high_confidence() -> None:
    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "action": "power_on",
                    "room": "bedroom",
                    "title": None,
                    "provider": None,
                    "goal": None,
                    "level": None,
                    "wake": True,
                    "confidence": 1.0,
                    "reason": "claimed room",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(IntentNotUnderstood):
        await CodexFastIntentParser(
            _rooms(), executable="/opt/codex", runner=runner
        ).parse("Wake the bedroom")


def test_async_runtime_keeps_one_core_alive(tmp_path: Path) -> None:
    service = _FakeService()
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    runtime = BrokerAsyncRuntime(core)
    try:
        response = runtime.submit(_request("Wake up Living Room"), timeout=3)
        assert response.ok is True
        assert service.calls[0]["action"] == "power_on"
    finally:
        runtime.close()
    assert service.closed is True


async def test_core_timeout_cancels_inflight_device_operation(tmp_path: Path) -> None:
    class SlowService(_FakeService):
        cancelled = False

        async def set_power(
            self,
            room_name: str,
            state: str,
            *,
            idempotency_key: str | None = None,
        ) -> ActionResult:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    service = SlowService()
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
        operation_timeout_seconds=0.01,
    )
    response = await core.execute(_request("Wake up Living Room"))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "operation_timeout"
    assert service.cancelled is True


async def test_core_operation_budget_includes_parser_time(tmp_path: Path) -> None:
    class SlowParser:
        async def parse(self, utterance: str) -> BrokerCommand:
            await asyncio.sleep(0.04)
            return await DeterministicIntentParser(_rooms()).parse(utterance)

    class SlowService(_FakeService):
        cancelled = False

        async def set_power(
            self,
            room_name: str,
            state: str,
            *,
            idempotency_key: str | None = None,
        ) -> ActionResult:
            try:
                await asyncio.sleep(0.04)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return await super().set_power(
                room_name,
                state,
                idempotency_key=idempotency_key,
            )

    service = SlowService()
    core = BrokerCore(
        service,
        SlowParser(),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
        operation_timeout_seconds=0.06,
    )

    response = await core.execute(_request("Wake up Living Room"))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "operation_timeout"
    assert service.cancelled is True


async def test_prewarm_exercises_exact_room_without_saving_frame(tmp_path: Path) -> None:
    service = _FakeService()
    screenshots = _FakeScreenshotService()
    service.screenshot_service = screenshots  # type: ignore[attr-defined]
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )

    result = await core.prewarm_observer("living_room")

    assert result["status"] == "ready"
    assert result["worker_reused"] is True
    assert screenshots.calls == ["living_room"]


def test_runtime_retries_failed_prewarm_until_observer_recovers(tmp_path: Path) -> None:
    service = _FakeService()
    screenshots = _RecoveringScreenshotService()
    service.screenshot_service = screenshots  # type: ignore[attr-defined]
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    runtime = BrokerAsyncRuntime(
        core,
        prewarm_rooms=("living_room",),
        prewarm_retry_seconds=0.01,
    )
    try:
        deadline = time.monotonic() + 2
        health = runtime.health()
        while time.monotonic() < deadline:
            health = runtime.health()
            if health["status"] == "ready":
                break
            time.sleep(0.01)

        assert health["status"] == "ready"
        prewarm = health["prewarm"]
        assert isinstance(prewarm, list) and len(prewarm) == 1
        assert prewarm[0]["room"] == "living_room"
        assert prewarm[0]["status"] == "ready"
        assert prewarm[0]["error"] is None
        assert prewarm[0]["worker_reused"] is True
        assert prewarm[0]["attempts"] == 2
        assert screenshots.calls == ["living_room", "living_room"]
    finally:
        runtime.close()


def test_failed_prewarm_stays_degraded_during_retry_wait(tmp_path: Path) -> None:
    service = _FakeService()
    screenshots = _RecoveringScreenshotService()
    service.screenshot_service = screenshots  # type: ignore[attr-defined]
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    runtime = BrokerAsyncRuntime(
        core,
        prewarm_rooms=("living_room",),
        prewarm_retry_seconds=10,
    )
    try:
        deadline = time.monotonic() + 2
        health = runtime.health()
        while time.monotonic() < deadline:
            health = runtime.health()
            prewarm = health["prewarm"]
            if isinstance(prewarm, list) and prewarm[0].get("status") == "degraded":
                break
            time.sleep(0.01)

        assert health["status"] == "degraded"
        assert health["prewarm"][0]["retrying"] is True  # type: ignore[index]
        assert screenshots.calls == ["living_room"]
    finally:
        runtime.close()


def test_runtime_rearms_prewarm_after_a_later_worker_failure(tmp_path: Path) -> None:
    service = _FakeService()
    screenshots = _FlappingScreenshotService()
    service.screenshot_service = screenshots  # type: ignore[attr-defined]
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    runtime = BrokerAsyncRuntime(core, prewarm_rooms=("living_room",))
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and runtime.health()["status"] != "ready":
            time.sleep(0.01)
        assert screenshots.calls == ["living_room"]

        screenshots.status = "degraded"
        degraded = runtime.health()
        assert degraded["status"] == "degraded"
        assert degraded["prewarm"][0]["retrying"] is True  # type: ignore[index]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and runtime.health()["status"] != "ready":
            time.sleep(0.01)
        assert screenshots.calls == ["living_room", "living_room"]
        assert runtime.health()["status"] == "ready"
    finally:
        runtime.close()


def test_runtime_health_propagates_observer_failure_without_exception_text(
    tmp_path: Path,
) -> None:
    service = _FakeService()

    class BrokenScreenshotHealth:
        def health_snapshot(self) -> dict[str, object]:
            raise RuntimeError("private-observer-secret")

    service.screenshot_service = BrokenScreenshotHealth()  # type: ignore[attr-defined]
    core = BrokerCore(
        service,
        DeterministicIntentParser(_rooms()),
        _rooms(),
        ledger=BrokerIdempotencyStore(tmp_path / "ledger.json"),
    )
    runtime = BrokerAsyncRuntime(core)
    try:
        health = runtime.health()
        assert health["status"] == "degraded"
        assert health["observer"] == {
            "status": "degraded",
            "last_error_code": "health_snapshot_failed",
        }
        assert "private-observer-secret" not in json.dumps(health)
    finally:
        runtime.close()
