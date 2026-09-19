"""Contract tests for the restricted Siri Shortcut SSH wrapper."""

from __future__ import annotations

import json
import os
import tempfile
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from home_media.content.prepare import PrepareContentResult, TerminalStatus
from home_media.content.router import ContentGoal
from home_media.errors import AmbiguousOutcomeError, AuthRequiredError
from home_media.shortcut import MAX_REQUEST_BYTES, ShortcutIdempotencyStore, run


class FakeShortcutService:
    def __init__(self, result: PrepareContentResult | None = None) -> None:
        self.closed = False
        self.calls: list[dict[str, Any]] = []
        self.result = result

    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: str = "search_ready",
        wake: bool = True,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult:
        self.calls.append(
            {
                "room": room_name,
                "title": title,
                "provider": provider,
                "goal": goal,
                "wake": wake,
                "idempotency_key": idempotency_key,
            }
        )
        if self.result is not None:
            return self.result
        return PrepareContentResult(
            room_key="living_room",
            title=title,
            normalized_title=title.casefold(),
            provider=provider,
            goal=ContentGoal.SEARCH_READY,
            terminal_status=TerminalStatus.QUERY_VERIFIED,
            verification_status="verified",
            selected_result=False,
            playback_started=False,
            idempotency_key=idempotency_key,
        )

    async def aclose(self) -> None:
        self.closed = True


class FailingShortcutService(FakeShortcutService):
    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: str = "search_ready",
        wake: bool = True,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult:
        _ = (room_name, title, provider, goal, wake, idempotency_key)
        raise AuthRequiredError("private-device-id")


class AmbiguousShortcutService(FakeShortcutService):
    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: str = "search_ready",
        wake: bool = True,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult:
        self.calls.append({"idempotency_key": idempotency_key})
        raise AmbiguousOutcomeError("post-send outcome unknown")


def _payload(**updates: object) -> bytes:
    request: dict[str, object] = {
        "schema_version": 1,
        "action": "prepare_content",
        "room": "living_room",
        "title": "Avatar: The Last Airbender",
        "provider": "netflix",
        "goal": "search_ready",
        "wake": True,
        "idempotency_key": "shortcut-test-1",
    }
    request.update(updates)
    return json.dumps(request).encode()


def _invoke(
    payload: bytes,
    service: FakeShortcutService,
    *,
    state_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    if state_path is None:
        with tempfile.TemporaryDirectory(prefix="home-media-shortcut-test-") as tmp:
            code = run(
                stdin=BytesIO(payload),
                stdout=output,
                service_factory=lambda: service,
                state_path=Path(tmp) / "state.json",
            )
    else:
        code = run(
            stdin=BytesIO(payload),
            stdout=output,
            service_factory=lambda: service,
            state_path=state_path,
        )
    return code, json.loads(output.getvalue())


def test_verified_request_calls_only_typed_prepare_and_closes() -> None:
    service = FakeShortcutService()
    code, response = _invoke(_payload(), service)

    assert code == 0
    assert service.closed is True
    assert service.calls == [
        {
            "room": "living_room",
            "title": "Avatar: The Last Airbender",
            "provider": "netflix",
            "goal": "search_ready",
            "wake": True,
            "idempotency_key": "shortcut-test-1",
        }
    ]
    assert response == {
        "schema_version": 1,
        "ok": True,
        "status": "verified",
        "spoken_response": (
            "Netflix search is ready for Avatar: The Last Airbender in Living Room. "
            "Select a result."
        ),
        "data": {
            "action": "prepare_content",
            "idempotency_key": "shortcut-test-1",
            "room_key": "living_room",
            "title": "Avatar: The Last Airbender",
            "provider": "netflix",
            "goal": "search_ready",
            "terminal_status": "query_verified",
            "verification_status": "verified",
            "selected_result": False,
            "playback_started": False,
        },
        "error": None,
    }


def test_rejects_raw_text_without_constructing_service() -> None:
    constructed = False

    def factory() -> FakeShortcutService:
        nonlocal constructed
        constructed = True
        return FakeShortcutService()

    output = StringIO()
    code = run(
        stdin=BytesIO(b"play Avatar in Living Room"),
        stdout=output,
        service_factory=factory,
    )

    assert code == 2
    assert constructed is False
    assert json.loads(output.getvalue())["error"]["code"] == "invalid_request"


def test_rejects_non_object_extra_fields_and_duplicate_keys() -> None:
    for payload in (
        b"[]",
        _payload(command="press select"),
        (
            b'{"schema_version":1,"schema_version":1,"action":"prepare_content",'
            b'"room":"living_room","title":"Avatar","goal":"search_ready",'
            b'"idempotency_key":"key-1"}'
        ),
    ):
        code, response = _invoke(payload, FakeShortcutService())
        assert code == 2
        assert response["ok"] is False
        assert response["error"]["code"] == "invalid_request"


def test_rejects_unsupported_goal_missing_key_and_coerced_boolean() -> None:
    missing_key = json.loads(_payload())
    del missing_key["idempotency_key"]
    for payload in (
        _payload(goal="resume"),
        json.dumps(missing_key).encode(),
        _payload(wake="true"),
    ):
        code, response = _invoke(payload, FakeShortcutService())
        assert code == 2
        assert response["error"]["code"] == "invalid_request"


def test_rejects_oversized_input_before_service_construction() -> None:
    constructed = False

    def factory() -> FakeShortcutService:
        nonlocal constructed
        constructed = True
        return FakeShortcutService()

    output = StringIO()
    code = run(
        stdin=BytesIO(b"{" + b" " * MAX_REQUEST_BYTES),
        stdout=output,
        service_factory=factory,
    )
    response = json.loads(output.getvalue())

    assert code == 2
    assert constructed is False
    assert response["error"]["code"] == "request_too_large"


def test_unverified_result_never_claims_ready_or_playing() -> None:
    result = PrepareContentResult(
        room_key="living_room",
        title="Avatar",
        normalized_title="avatar",
        provider="netflix",
        goal=ContentGoal.SEARCH_READY,
        terminal_status=TerminalStatus.HANDOFF,
        verification_status="unverified",
        selected_result=False,
        playback_started=False,
        idempotency_key="shortcut-test-1",
    )
    code, response = _invoke(_payload(title="Avatar"), FakeShortcutService(result))

    assert code == 1
    assert response["ok"] is False
    assert response["status"] == "unverified"
    assert "could not be verified" in response["spoken_response"]
    assert "No result was selected" in response["spoken_response"]


def test_search_ready_rejects_backend_selection_or_playback_claim() -> None:
    result = PrepareContentResult(
        room_key="living_room",
        title="Avatar",
        normalized_title="avatar",
        provider="netflix",
        goal=ContentGoal.SEARCH_READY,
        terminal_status=TerminalStatus.QUERY_VERIFIED,
        verification_status="verified",
        selected_result=True,
        playback_started=True,
        idempotency_key="shortcut-test-1",
    )
    code, response = _invoke(_payload(title="Avatar"), FakeShortcutService(result))

    assert code == 1
    assert response["ok"] is False
    assert response["error"]["code"] == "backend_contract_violation"
    assert "search is ready" not in response["spoken_response"].casefold()
    assert "playing" not in response["spoken_response"].casefold()


def test_backend_errors_are_sanitized_non_retryable_and_service_closes() -> None:
    service = FailingShortcutService()
    code, response = _invoke(_payload(), service)
    serialized = json.dumps(response)

    assert code == 1
    assert service.closed is True
    assert response["error"] == {
        "code": "auth_required",
        "message": "The Home Media backend rejected or could not verify the request.",
        "retryable": False,
    }
    assert "private-device-id" not in serialized


def test_success_is_cached_across_process_shaped_invocations(tmp_path: Path) -> None:
    state_path = tmp_path / "shortcut-state.json"
    first = FakeShortcutService()
    second = FakeShortcutService()

    first_code, first_response = _invoke(_payload(), first, state_path=state_path)
    second_code, second_response = _invoke(_payload(), second, state_path=state_path)

    assert first_code == second_code == 0
    assert first_response == second_response
    assert len(first.calls) == 1
    assert second.calls == []
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"


def test_ambiguous_outcome_is_never_redispatched_across_invocations(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "shortcut-state.json"
    first = AmbiguousShortcutService()
    second = FakeShortcutService()

    first_code, first_response = _invoke(_payload(), first, state_path=state_path)
    second_code, second_response = _invoke(_payload(), second, state_path=state_path)

    assert first_code == second_code == 1
    assert first_response["error"]["code"] == "partial"
    assert second_response["error"]["code"] == "partial"
    assert len(first.calls) == 1
    assert second.calls == []


def test_pending_record_from_crashed_process_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "shortcut-state.json"
    request = json.loads(_payload())
    from home_media.shortcut import ShortcutRequest

    store = ShortcutIdempotencyStore(state_path)
    decision, _ = store.begin(ShortcutRequest.model_validate(request))
    assert decision == "execute"

    service = FakeShortcutService()
    code, response = _invoke(_payload(), service, state_path=state_path)
    assert code == 1
    assert response["error"]["code"] == "partial"
    assert service.calls == []


def test_reused_key_with_different_request_is_rejected(tmp_path: Path) -> None:
    state_path = tmp_path / "shortcut-state.json"
    _invoke(_payload(), FakeShortcutService(), state_path=state_path)
    second = FakeShortcutService()

    code, response = _invoke(
        _payload(title="Different title"), second, state_path=state_path
    )

    assert code == 1
    assert response["error"]["code"] == "idempotency_conflict"
    assert second.calls == []


def test_unsafe_ledger_permissions_fail_before_dispatch(tmp_path: Path) -> None:
    state_path = tmp_path / "shortcut-state.json"
    state_path.write_text('{"schema_version":1,"entries":{}}', encoding="utf-8")
    os.chmod(state_path, 0o644)
    service = FakeShortcutService()

    code, response = _invoke(_payload(), service, state_path=state_path)

    assert code == 1
    assert response["error"]["code"] == "idempotency_state_unavailable"
    assert service.calls == []


def test_dangling_ledger_symlink_fails_before_dispatch(tmp_path: Path) -> None:
    state_path = tmp_path / "shortcut-state.json"
    state_path.symlink_to(tmp_path / "missing-target")
    service = FakeShortcutService()

    code, response = _invoke(_payload(), service, state_path=state_path)

    assert code == 1
    assert response["error"]["code"] == "idempotency_state_unavailable"
    assert service.calls == []
