"""Persistent exact-device screenshot worker lifecycle and protocol tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Self

import pytest
from PIL import Image

from home_media.errors import TimeoutError_
from home_media.observers.blank import synthesize_png
from home_media.observers.capture import (
    PyMobileDeviceScreenshotCapturer,
    _worker_executable,
    build_persistent_capture_argv,
    finalize_png_result,
)
from home_media.observers.pmd3_wifi_capture import (
    MAX_SCREENSHOT_MESSAGE_BYTES,
    PersistentScreenshotSession,
    WorkerStartupError,
    configure_dtx_screenshot_limit,
    select_exact_service,
    serve_jsonl,
    validate_screenshot_size,
)
from home_media.observers.screenshot import prepare_screenshot_for_analysis

UDID = "private-observer-id-living"
DEVICE_ID = "00000000-0000-4000-8000-000000000004"


def test_worker_widens_only_a_too_small_dtx_pool() -> None:
    small = SimpleNamespace(MAX_BUFFERED_SIZE=30 * 1024 * 1024)
    already_larger = SimpleNamespace(MAX_BUFFERED_SIZE=96 * 1024 * 1024)

    configure_dtx_screenshot_limit(small)
    configure_dtx_screenshot_limit(already_larger)

    assert small.MAX_BUFFERED_SIZE == MAX_SCREENSHOT_MESSAGE_BYTES
    assert already_larger.MAX_BUFFERED_SIZE == 96 * 1024 * 1024


def test_worker_enforces_its_own_screenshot_payload_ceiling() -> None:
    validate_screenshot_size(b"png")
    with pytest.raises(RuntimeError, match="screenshot_payload_size_invalid"):
        validate_screenshot_size(b"")
    with pytest.raises(RuntimeError, match="screenshot_payload_size_invalid"):
        validate_screenshot_size(b"x" * (MAX_SCREENSHOT_MESSAGE_BYTES + 1))


def test_worker_downscales_large_frames_for_fast_analysis() -> None:
    source = synthesize_png(640, 360, (20, 80, 140))

    captured = finalize_png_result(
        source,
        stable_device_id=DEVICE_ID,
        room_key="living_room",
        udid=UDID,
        latency_ms=1,
    )
    result = prepare_screenshot_for_analysis(captured, max_width=320)

    assert result.png_bytes is not None
    with Image.open(__import__("io").BytesIO(result.png_bytes)) as image:
        assert image.size == (320, 180)


@pytest.mark.asyncio
async def test_worker_preserves_original_capture_bytes(tmp_path: Path) -> None:
    source = synthesize_png(640, 360, (20, 80, 140))

    class _Screenshot:
        async def get_screenshot(self) -> bytes:
            return source

    session = PersistentScreenshotSession(UDID)
    session._screenshot = _Screenshot()  # noqa: SLF001
    output = tmp_path / "raw.png"

    await session.capture_to(output)

    assert output.read_bytes() == source


class _Candidate:
    def __init__(self, remote_identifier: str, hostname: str) -> None:
        self.remote_identifier = remote_identifier
        self.hostname = hostname
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _MalformedCandidate:
    def __init__(self) -> None:
        self.closed = 0

    @property
    def remote_identifier(self) -> str:
        raise RuntimeError("malformed")

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_worker_selects_exact_identity_and_closes_every_unselected_candidate() -> None:
    exact_v4 = _Candidate(UDID, "192.0.2.10")
    exact_v6 = _Candidate(UDID, "fd00::10")
    wrong = _Candidate("different-observer", "192.0.2.11")

    selected = await select_exact_service([exact_v6, wrong, exact_v4], UDID)

    assert selected is exact_v4
    assert exact_v4.closed == 0
    assert exact_v6.closed == 1
    assert wrong.closed == 1
    await selected.close()


@pytest.mark.asyncio
async def test_worker_identity_mismatch_closes_all_candidates() -> None:
    first = _Candidate("wrong-one", "192.0.2.20")
    second = _Candidate("wrong-two", "fd00::20")
    with pytest.raises(WorkerStartupError):
        await select_exact_service([first, second], UDID)
    assert first.closed == 1
    assert second.closed == 1


@pytest.mark.asyncio
async def test_worker_closes_candidate_without_hostname_or_identity() -> None:
    malformed = _MalformedCandidate()
    exact = _Candidate(UDID, "192.0.2.10")

    selected = await select_exact_service([malformed, exact], UDID)

    assert selected is exact
    assert malformed.closed == 1
    await selected.close()


class _FakeSession:
    def __init__(self, udid: str) -> None:
        self.observed_udid_fingerprint = hashlib.sha256(udid.encode()).hexdigest()[:12]
        self.startup_ms = 17
        self.capture_count = 0
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited += 1

    async def capture_to(self, out: Path) -> int:
        out.write_bytes(b"frame")
        self.capture_count += 1
        return 3


@pytest.mark.asyncio
async def test_jsonl_worker_reuses_one_session_and_accepts_only_typed_operations(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    requests = [
        {
            "protocol_version": 1,
            "type": "capture",
            "request_id": "one",
            "out": str(first),
        },
        {
            "protocol_version": 1,
            "type": "capture",
            "request_id": "two",
            "out": str(second),
        },
        {"protocol_version": 1, "type": "shutdown", "request_id": "stop"},
    ]
    lines = [(json.dumps(item) + "\n").encode() for item in requests]
    responses: list[dict[str, object]] = []
    session = _FakeSession(UDID)

    async def read_line() -> bytes:
        return lines.pop(0) if lines else b""

    async def write_message(message: dict[str, object]) -> None:
        responses.append(message)

    await serve_jsonl(
        UDID,
        read_line,
        write_message,
        session_factory=lambda _udid: session,
    )

    assert first.read_bytes() == b"frame"
    assert second.read_bytes() == b"frame"
    assert session.entered == 1
    assert session.exited == 1
    assert [response["type"] for response in responses] == [
        "capture_result",
        "capture_result",
        "shutdown_result",
    ]
    assert responses[0]["worker_reused"] is False
    assert responses[1]["worker_reused"] is True


def _write_fake_worker(tmp_path: Path, *, behavior: str = "ok") -> Path:
    png_b64 = base64.b64encode(synthesize_png(12, 8, (30, 180, 30))).decode()
    script = tmp_path / f"fake_capture_worker_{behavior}.py"
    script.write_text(
        f"""
import argparse
import base64
import hashlib
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--udid", required=True)
parser.add_argument("--serve-jsonl", action="store_true", required=True)
args = parser.parse_args()
fingerprint = hashlib.sha256(args.udid.encode()).hexdigest()[:12]
count = 0
marker = {str(tmp_path / "worker-exited-once")!r}
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "shutdown":
        response = {{
            "protocol_version": 1,
            "type": "shutdown_result",
            "request_id": request["request_id"],
            "ok": True,
        }}
        print(json.dumps(response), flush=True)
        break
    if {behavior!r} in {{"slow", "cancel"}}:
        time.sleep(60)
    if {behavior!r} == "exit_once" and not __import__("os").path.exists(marker):
        open(marker, "w").close()
        sys.exit(0)
    with open(request["out"], "wb") as output:
        output.write(base64.b64decode({png_b64!r}))
    response = {{
        "protocol_version": 1,
        "type": "capture_result",
        "request_id": request["request_id"],
        "ok": True,
        "observer_udid_fingerprint": (
            "000000000000" if {behavior!r} == "identity_mismatch" else fingerprint
        ),
        "capture_latency_ms": 4,
        "worker_reused": count > 0,
        "worker_startup_ms": 11 if count == 0 else None,
        "capture_count": count + 1,
    }}
    count += 1
    print(json.dumps(response), flush=True)
    if {behavior!r} == "exit_after_response":
        sys.exit(0)
""".lstrip(),
        encoding="utf-8",
    )
    return script


def test_persistent_worker_argv_is_exact_and_has_no_output_or_device_request() -> None:
    argv = build_persistent_capture_argv(
        udid=UDID,
        executable=sys.executable,
        worker_path=Path("/tmp/fake-worker.py"),
    )
    assert argv[argv.index("--udid") + 1] == UDID
    assert "--serve-jsonl" in argv
    assert "--out" not in argv


def test_console_script_resolves_own_python_interpreter(tmp_path: Path) -> None:
    interpreter = tmp_path / "python3.14"
    interpreter.write_text("", encoding="utf-8")
    console = tmp_path / "pymobiledevice3"
    console.write_text(f"#!{interpreter}\n", encoding="utf-8")

    assert _worker_executable(str(console)) == str(interpreter)


@pytest.mark.asyncio
async def test_core_reuses_one_exact_worker_and_health_is_redacted(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path)
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=2.0,
    )
    try:
        first = await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
        second = await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
        health = capturer.health_snapshot()
        assert first.png_bytes is not None and second.png_bytes is not None
        assert first.metadata.worker_reused is False
        assert first.metadata.worker_startup_ms == 11
        assert second.metadata.worker_reused is True
        assert health["worker_count"] == 1
        assert health["capture_count"] == 2
        assert UDID not in json.dumps(health)
        assert hashlib.sha256(UDID.encode()).hexdigest()[:12] in json.dumps(health)
    finally:
        await capturer.aclose()
    assert capturer.health_snapshot()["status"] == "closed"


@pytest.mark.asyncio
async def test_read_only_capture_reconnects_once_after_stale_worker_pipe(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path, behavior="exit_once")
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=2.0,
    )
    try:
        result = await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
        health = capturer.health_snapshot()
        assert result.png_bytes is not None
        assert health["workers"][0]["spawn_count"] == 2
    finally:
        await capturer.aclose()


@pytest.mark.asyncio
async def test_health_marks_unexpectedly_exited_worker_degraded(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path, behavior="exit_after_response")
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=2.0,
    )
    try:
        result = await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
        assert result.png_bytes is not None
        await asyncio.sleep(0.05)
        health = capturer.health_snapshot()
        assert health["status"] == "degraded"
        assert health["workers"][0]["last_error_code"] == "worker_exited"
    finally:
        await capturer.aclose()


@pytest.mark.asyncio
async def test_wrong_observed_worker_identity_fails_closed(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path, behavior="identity_mismatch")
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=2.0,
    )
    try:
        result = await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
        assert result.png_bytes is None
        assert result.error == "worker_identity_mismatch"
        assert result.observer_udid_fingerprint is None
        assert UDID not in json.dumps(capturer.health_snapshot())
    finally:
        await capturer.aclose()


@pytest.mark.asyncio
async def test_worker_timeout_kills_and_reaps_process(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path, behavior="slow")
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=0.05,
    )
    with pytest.raises(TimeoutError_):
        await capturer.capture_udid(
            UDID, stable_device_id=DEVICE_ID, room_key="living_room"
        )
    pool = capturer._worker_pool  # noqa: SLF001 - assert subprocess cleanup contract
    assert pool is not None
    client = pool._workers[UDID]  # noqa: SLF001
    assert client._proc is None  # noqa: SLF001
    assert client.state == "degraded"
    await capturer.aclose()


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_process(tmp_path: Path) -> None:
    worker = _write_fake_worker(tmp_path, behavior="cancel")
    capturer = PyMobileDeviceScreenshotCapturer(
        executable=sys.executable,
        worker_path=worker,
        persistent=True,
        timeout_s=5.0,
    )
    task = asyncio.create_task(
        capturer.capture_udid(UDID, stable_device_id=DEVICE_ID, room_key="living_room")
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pool = capturer._worker_pool  # noqa: SLF001 - assert subprocess cleanup contract
    assert pool is not None
    assert pool._workers[UDID]._proc is None  # noqa: SLF001
    await capturer.aclose()
