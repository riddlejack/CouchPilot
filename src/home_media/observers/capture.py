"""Exact-device Apple TV screenshots across an optional GPL subprocess boundary.

The default path keeps one lazy JSONL worker per immutable observer UDID.  The
worker owns and reuses its tunnel/RSD/DVT channel; core never imports
``pymobiledevice3``.  ``HOME_MEDIA_CAPTURE_MODE=oneshot`` is the explicit
compatibility fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from home_media.errors import ConfigError, TimeoutError_, UnsupportedError
from home_media.observers.binding import fingerprint_udid
from home_media.observers.blank import assess_blank_or_protected, is_png
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult

DEFAULT_CAPTURE_TIMEOUT_S = 45.0
DEFAULT_SHUTDOWN_TIMEOUT_S = 2.0
WORKER_PROTOCOL_VERSION = 1
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")

CaptureRunner = Callable[..., Awaitable[tuple[int, bytes, bytes]]]
WorkerArgvBuilder = Callable[[str], list[str]]


@dataclass(frozen=True)
class CaptureCommand:
    argv: list[str]
    output_path: Path


@dataclass(frozen=True)
class WorkerCaptureOutcome:
    ok: bool
    error_code: str | None = None
    observer_udid_fingerprint: str | None = None
    capture_latency_ms: int | None = None
    worker_reused: bool | None = None
    worker_startup_ms: int | None = None


def resolve_capture_python() -> str:
    """Resolve the Python used by the optional pymobiledevice3 worker."""
    override = os.environ.get("HOME_MEDIA_PYMOBILEDEVICE3_PYTHON") or os.environ.get(
        "HOME_MEDIA_PYMOBILEDEVICE3"
    )
    if override:
        return override
    sibling = Path(__file__).resolve().parents[3] / ".venv-pmd3" / "bin" / "python"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("pymobiledevice3")
    if found:
        return found
    return sys.executable


def resolve_pymobiledevice3_executable() -> str:
    """Backward-compatible alias used by unit tests."""
    return resolve_capture_python()


def _worker_executable(executable: str | None) -> str:
    exe = executable or resolve_capture_python()
    # A console-script path cannot execute the standalone worker file. Resolve
    # the interpreter that owns that script instead of silently falling back to
    # core's interpreter, where the optional pymobiledevice3 dependency may not
    # be installed.
    if Path(exe).name == "pymobiledevice3":
        with contextlib.suppress(OSError, UnicodeError):
            first_line = Path(exe).read_text(encoding="utf-8").splitlines()[0]
            if first_line.startswith("#!"):
                interpreter = first_line[2:].strip().split()[0]
                if Path(interpreter).is_file():
                    return interpreter
        for name in ("python3", "python"):
            sibling = Path(exe).with_name(name)
            if sibling.is_file():
                return str(sibling)
        return sys.executable
    return exe


def build_wifi_capture_argv(
    *,
    udid: str,
    output_path: Path,
    executable: str | None = None,
    worker_path: Path | None = None,
) -> list[str]:
    """Build the explicit one-shot fallback argv for one exact observer UDID."""
    if not udid or not udid.strip():
        raise ConfigError("capture requires an exact observer UDID")
    worker = worker_path or Path(__file__).with_name("pmd3_wifi_capture.py")
    return [
        _worker_executable(executable),
        str(worker),
        "--udid",
        udid,
        "--out",
        str(output_path),
    ]


def build_persistent_capture_argv(
    *,
    udid: str,
    executable: str | None = None,
    worker_path: Path | None = None,
) -> list[str]:
    """Build a typed JSONL worker argv permanently bound to ``udid``."""
    if not udid or not udid.strip():
        raise ConfigError("capture requires an exact observer UDID")
    worker = worker_path or Path(__file__).with_name("pmd3_wifi_capture.py")
    return [
        _worker_executable(executable),
        str(worker),
        "--udid",
        udid,
        "--serve-jsonl",
    ]


def build_dvt_screenshot_argv(
    *,
    udid: str,
    output_path: Path,
    executable: str | None = None,
    userspace: bool = True,
) -> list[str]:
    """Legacy pymobiledevice3 CLI argv retained for compatibility tests."""
    if not udid or not udid.strip():
        raise ConfigError("capture requires an exact observer UDID")
    exe = executable or resolve_capture_python()
    exe_name = Path(exe).name
    if exe == sys.executable or exe_name in {"python", "python3"} or exe_name.startswith(
        "python"
    ):
        argv = [exe, "-m", "pymobiledevice3"]
    else:
        argv = [exe]
    argv.extend(
        [
            "developer",
            "dvt",
            "screenshot",
            str(output_path),
            "--udid",
            udid,
        ]
    )
    if userspace:
        argv.append("--userspace")
    return argv


def plan_capture_command(
    *,
    udid: str,
    output_path: Path,
    executable: str | None = None,
    userspace: bool = True,
    wifi_worker: bool = True,
) -> CaptureCommand:
    if wifi_worker:
        return CaptureCommand(
            argv=build_wifi_capture_argv(
                udid=udid, output_path=output_path, executable=executable
            ),
            output_path=output_path,
        )
    return CaptureCommand(
        argv=build_dvt_screenshot_argv(
            udid=udid,
            output_path=output_path,
            executable=executable,
            userspace=userspace,
        ),
        output_path=output_path,
    )


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.communicate(), timeout=DEFAULT_SHUTDOWN_TIMEOUT_S)
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=DEFAULT_SHUTDOWN_TIMEOUT_S)


async def run_capture_subprocess(
    argv: Sequence[str],
    *,
    timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run one-shot capture with no shell and always reap on timeout/cancel."""
    if not argv:
        raise ConfigError("capture argv must be non-empty")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise UnsupportedError(
            "screenshot.capture",
            reason="pymobiledevice3 executable not found (optional GPL tool not installed)",
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        await _kill_and_reap(proc)
        raise TimeoutError_("Screenshot capture timed out") from exc
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_kill_and_reap(proc))
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


def _safe_error_code(value: object, *, fallback: str = "worker_protocol_error") -> str:
    if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value):
        return value
    return fallback


class _PersistentCaptureWorker:
    """One serialized subprocess permanently bound to one exact UDID."""

    def __init__(
        self,
        udid: str,
        argv: list[str],
        *,
        timeout_s: float,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
    ) -> None:
        if "--udid" not in argv or argv[argv.index("--udid") + 1] != udid:
            raise ConfigError("persistent worker argv UDID mismatch")
        if "--serve-jsonl" not in argv:
            raise ConfigError("persistent worker argv missing --serve-jsonl")
        self._udid = udid
        self._fingerprint = fingerprint_udid(udid)
        self._argv = list(argv)
        self._timeout_s = timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._closed = False
        self._request_counter = 0
        self._spawn_count = 0
        self.capture_count = 0
        self.last_error_code: str | None = None
        self.state = "idle"

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._closed:
            raise ConfigError("screenshot worker is closed")
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        self.state = "starting"
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Dependency diagnostics can contain private identifiers/addresses.
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            self.state = "degraded"
            self.last_error_code = "worker_executable_not_found"
            raise UnsupportedError(
                "screenshot.capture",
                reason="persistent screenshot worker executable not found",
            ) from exc
        self._proc = proc
        self._spawn_count += 1
        return proc

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"request-{self._request_counter}"

    async def capture_to(self, output_path: Path) -> WorkerCaptureOutcome:
        async with self._lock:
            try:
                outcome = await self._capture_locked(output_path)
                # Captures are read-only. A stale tunnel or dead JSONL pipe can
                # therefore be re-established once without any ambiguous-TV-
                # mutation risk. Identity failures deliberately never retry.
                if outcome.error_code in {
                    "worker_connection_lost",
                    "worker_protocol_error",
                }:
                    outcome = await self._capture_locked(output_path)
                return outcome
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self._stop_locked(graceful=False))
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
                raise

    async def _capture_locked(self, output_path: Path) -> WorkerCaptureOutcome:
        proc = await self._ensure_started()
        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            await self._stop_locked(graceful=False)
            return self._failure("worker_pipe_unavailable")
        request_id = self._next_request_id()
        request = {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "type": "capture",
            "request_id": request_id,
            "out": str(output_path),
        }
        try:
            stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            await asyncio.wait_for(stdin.drain(), timeout=self._timeout_s)
            raw = await asyncio.wait_for(stdout.readline(), timeout=self._timeout_s)
        except TimeoutError as exc:
            self.last_error_code = "worker_timeout"
            self.state = "degraded"
            await self._stop_locked(graceful=False)
            raise TimeoutError_("Persistent screenshot worker timed out") from exc
        except (BrokenPipeError, ConnectionError):
            await self._stop_locked(graceful=False)
            return self._failure("worker_connection_lost")

        response = self._decode_response(raw)
        if response is None:
            await self._stop_locked(graceful=False)
            return self._failure("worker_protocol_error")
        if response.get("type") == "worker_error":
            code = _safe_error_code(response.get("error_code"), fallback="worker_start_failed")
            await self._stop_locked(graceful=False)
            return self._failure(code)
        if response.get("type") != "capture_result" or response.get("request_id") != request_id:
            await self._stop_locked(graceful=False)
            return self._failure("worker_protocol_error")

        observed = response.get("observer_udid_fingerprint")
        if observed != self._fingerprint:
            await self._stop_locked(graceful=False)
            # Do not claim the expected fingerprint when the worker observed another identity.
            return self._failure("worker_identity_mismatch", observed_fingerprint=None)
        if response.get("ok") is not True:
            code = _safe_error_code(response.get("error_code"), fallback="capture_failed")
            await self._stop_locked(graceful=False)
            return self._failure(code, observed_fingerprint=self._fingerprint)
        if not output_path.is_file():
            await self._stop_locked(graceful=False)
            return self._failure("capture_output_missing", observed_fingerprint=self._fingerprint)

        latency = response.get("capture_latency_ms")
        startup = response.get("worker_startup_ms")
        reused = response.get("worker_reused")
        outcome = WorkerCaptureOutcome(
            ok=True,
            observer_udid_fingerprint=self._fingerprint,
            capture_latency_ms=latency if isinstance(latency, int) and latency >= 0 else None,
            worker_reused=reused if isinstance(reused, bool) else None,
            worker_startup_ms=startup if isinstance(startup, int) and startup >= 0 else None,
        )
        self.capture_count += 1
        self.last_error_code = None
        self.state = "ready"
        return outcome

    @staticmethod
    def _decode_response(raw: bytes) -> dict[str, Any] | None:
        if not raw or len(raw) > 16_384:
            return None
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(response, dict):
            return None
        if response.get("protocol_version") != WORKER_PROTOCOL_VERSION:
            return None
        return response

    def _failure(
        self,
        code: str,
        *,
        observed_fingerprint: str | None = None,
    ) -> WorkerCaptureOutcome:
        safe = _safe_error_code(code)
        self.last_error_code = safe
        self.state = "degraded"
        return WorkerCaptureOutcome(
            ok=False,
            error_code=safe,
            observer_udid_fingerprint=observed_fingerprint,
        )

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            await self._stop_locked(graceful=True)
            self.state = "closed"

    async def _stop_locked(self, *, graceful: bool) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        stdin = proc.stdin
        stdout = proc.stdout
        if graceful and proc.returncode is None and stdin is not None and stdout is not None:
            request_id = self._next_request_id()
            request = {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "type": "shutdown",
                "request_id": request_id,
            }
            try:
                stdin.write(
                    (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
                )
                await asyncio.wait_for(stdin.drain(), timeout=self._shutdown_timeout_s)
                raw = await asyncio.wait_for(stdout.readline(), timeout=self._shutdown_timeout_s)
                response = self._decode_response(raw)
                if (
                    response is not None
                    and response.get("type") == "shutdown_result"
                    and response.get("request_id") == request_id
                ):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=self._shutdown_timeout_s)
            except (BrokenPipeError, ConnectionError, TimeoutError):
                pass

        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._shutdown_timeout_s)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=self._shutdown_timeout_s)
        if stdin is not None:
            stdin.close()
            with contextlib.suppress(Exception):
                await stdin.wait_closed()

    def health_snapshot(self) -> dict[str, object]:
        """Return only safe aggregate state and the bound identifier fingerprint."""
        status = self.state
        last_error_code = self.last_error_code
        if self._proc is not None and self._proc.returncode is not None and not self._closed:
            status = "degraded"
            last_error_code = "worker_exited"
        return {
            "observer_udid_fingerprint": self._fingerprint,
            "status": status,
            "capture_count": self.capture_count,
            "spawn_count": self._spawn_count,
            "last_error_code": last_error_code,
        }


class PersistentCaptureWorkerPool:
    """Lazy exact-UDID worker pool; workers never change their bound identity."""

    def __init__(
        self,
        argv_builder: WorkerArgvBuilder,
        *,
        timeout_s: float,
    ) -> None:
        self._argv_builder = argv_builder
        self._timeout_s = timeout_s
        self._workers: dict[str, _PersistentCaptureWorker] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def capture_to(self, udid: str, output_path: Path) -> WorkerCaptureOutcome:
        async with self._lock:
            if self._closed:
                raise ConfigError("persistent screenshot worker pool is closed")
            worker = self._workers.get(udid)
            if worker is None:
                worker = _PersistentCaptureWorker(
                    udid,
                    self._argv_builder(udid),
                    timeout_s=self._timeout_s,
                )
                self._workers[udid] = worker
        return await worker.capture_to(output_path)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            workers = list(self._workers.values())
        await asyncio.gather(*(worker.aclose() for worker in workers), return_exceptions=True)

    def health_snapshot(self) -> dict[str, object]:
        workers = [worker.health_snapshot() for worker in self._workers.values()]
        states = {str(worker["status"]) for worker in workers}
        if self._closed:
            status = "closed"
        elif "degraded" in states:
            status = "degraded"
        elif "starting" in states:
            status = "starting"
        elif "ready" in states:
            status = "ready"
        else:
            status = "idle"
        return {
            "status": status,
            "persistent": True,
            "worker_count": len(workers),
            "capture_count": sum(worker.capture_count for worker in self._workers.values()),
            "workers": workers,
        }


class PyMobileDeviceScreenshotCapturer:
    """Capture PNG bytes for exact UDIDs through persistent or one-shot workers."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        userspace: bool = True,
        timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
        runner: CaptureRunner | None = None,
        persistent: bool | None = None,
        worker_path: Path | None = None,
    ) -> None:
        mode = os.environ.get("HOME_MEDIA_CAPTURE_MODE", "persistent").strip().lower()
        if mode not in {"persistent", "oneshot"}:
            raise ConfigError("HOME_MEDIA_CAPTURE_MODE must be 'persistent' or 'oneshot'")
        if persistent is None:
            persistent = mode == "persistent" and runner is None
        if persistent and runner is not None:
            raise ConfigError("custom one-shot runner cannot be used with persistent=True")
        self.executable = executable
        self.userspace = userspace
        self.timeout_s = timeout_s
        self._runner: CaptureRunner = runner or run_capture_subprocess
        self._persistent = persistent
        self._worker_path = worker_path
        self._worker_pool = (
            PersistentCaptureWorkerPool(
                lambda udid: build_persistent_capture_argv(
                    udid=udid,
                    executable=self.executable,
                    worker_path=self._worker_path,
                ),
                timeout_s=timeout_s,
            )
            if persistent
            else None
        )

    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ) -> ScreenshotResult:
        if not udid.strip():
            raise ConfigError("capture requires an exact observer UDID")
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="home-media-shot-") as tmp:
            out_path = Path(tmp) / "capture.png"
            if self._worker_pool is not None:
                outcome = await self._worker_pool.capture_to(udid, out_path)
                total_latency_ms = max(0, int((time.perf_counter() - started) * 1000))
                if not outcome.ok:
                    return _capture_failure_result(
                        stable_device_id=stable_device_id,
                        room_key=room_key,
                        udid=udid,
                        observed_fingerprint=outcome.observer_udid_fingerprint,
                        error_code=outcome.error_code or "capture_failed",
                        latency_ms=total_latency_ms,
                        worker_reused=outcome.worker_reused,
                        worker_startup_ms=outcome.worker_startup_ms,
                    )
                png = out_path.read_bytes()
                return finalize_png_result(
                    png,
                    stable_device_id=stable_device_id,
                    room_key=room_key,
                    udid=udid,
                    latency_ms=total_latency_ms,
                    worker_reused=outcome.worker_reused,
                    worker_startup_ms=outcome.worker_startup_ms,
                )
            return await self._capture_one_shot(
                udid,
                stable_device_id=stable_device_id,
                room_key=room_key,
                out_path=out_path,
                started=started,
            )

    async def _capture_one_shot(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
        out_path: Path,
        started: float,
    ) -> ScreenshotResult:
        planned = plan_capture_command(
            udid=udid,
            output_path=out_path,
            executable=self.executable,
            userspace=self.userspace,
            wifi_worker=True,
        )
        if "--udid" not in planned.argv:
            raise ConfigError("capture argv missing --udid")
        udid_idx = planned.argv.index("--udid") + 1
        if planned.argv[udid_idx] != udid:
            raise ConfigError("capture argv UDID mismatch")

        child_env = os.environ.copy()
        try:
            if self._runner is run_capture_subprocess:
                rc, _stdout, _stderr = await self._runner(
                    planned.argv, timeout_s=self.timeout_s, env=child_env
                )
            else:
                rc, _stdout, _stderr = await self._runner(
                    planned.argv, timeout_s=self.timeout_s
                )
        except TimeoutError as exc:
            raise TimeoutError_("Screenshot capture timed out") from exc
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        if rc != 0:
            # Never forward arbitrary dependency stdout/stderr into public results.
            return _capture_failure_result(
                stable_device_id=stable_device_id,
                room_key=room_key,
                udid=udid,
                observed_fingerprint=fingerprint_udid(udid),
                error_code=f"capture_exit_{rc}" if 0 < rc < 1000 else "capture_failed",
                latency_ms=latency_ms,
                worker_reused=False,
                worker_startup_ms=None,
            )
        if not out_path.exists():
            return _capture_failure_result(
                stable_device_id=stable_device_id,
                room_key=room_key,
                udid=udid,
                observed_fingerprint=fingerprint_udid(udid),
                error_code="capture_output_missing",
                latency_ms=latency_ms,
                worker_reused=False,
                worker_startup_ms=None,
            )
        return finalize_png_result(
            out_path.read_bytes(),
            stable_device_id=stable_device_id,
            room_key=room_key,
            udid=udid,
            latency_ms=latency_ms,
            worker_reused=False,
            worker_startup_ms=None,
        )

    async def aclose(self) -> None:
        if self._worker_pool is not None:
            await self._worker_pool.aclose()

    def health_snapshot(self) -> dict[str, object]:
        if self._worker_pool is None:
            return {
                "status": "one_shot",
                "persistent": False,
                "worker_count": 0,
                "capture_count": 0,
            }
        return self._worker_pool.health_snapshot()


def _capture_failure_result(
    *,
    stable_device_id: str,
    room_key: str,
    udid: str,
    observed_fingerprint: str | None,
    error_code: str,
    latency_ms: int,
    worker_reused: bool | None,
    worker_startup_ms: int | None,
) -> ScreenshotResult:
    return ScreenshotResult(
        device_id=stable_device_id,
        room_key=room_key,
        blank_or_protected=True,
        png_bytes=None,
        error=_safe_error_code(error_code, fallback="capture_failed"),
        tunnel_identity=None,
        observer_udid_fingerprint=(
            observed_fingerprint
            if observed_fingerprint is not None
            else (None if error_code == "worker_identity_mismatch" else fingerprint_udid(udid))
        ),
        metadata=CaptureMetadata(
            latency_ms=latency_ms,
            capture_backend="pymobiledevice3_wifi_remote_pair_tcp",
            worker_reused=worker_reused,
            worker_startup_ms=worker_startup_ms,
            argv_has_udid=True,
            # The worker uses an in-process userspace tunnel; this is not an argv flag.
            argv_has_userspace=False,
        ),
    )


def finalize_png_result(
    png: bytes,
    *,
    stable_device_id: str,
    room_key: str,
    udid: str | None,
    latency_ms: int,
    worker_reused: bool | None = None,
    worker_startup_ms: int | None = None,
    capture_backend: str = "pymobiledevice3_wifi_remote_pair_tcp",
    captured_at: str | None = None,
) -> ScreenshotResult:
    if not png or not is_png(png):
        return ScreenshotResult(
            device_id=stable_device_id,
            room_key=room_key,
            blank_or_protected=True,
            png_bytes=None,
            error="malformed_or_empty_png",
            observer_udid_fingerprint=fingerprint_udid(udid) if udid else None,
            metadata=CaptureMetadata(
                latency_ms=latency_ms,
                sha256=None,
                capture_backend=capture_backend,
                worker_reused=worker_reused,
                worker_startup_ms=worker_startup_ms,
                argv_has_udid=bool(udid),
                argv_has_userspace=False,
            ),
        )
    assessment = assess_blank_or_protected(png)
    digest = hashlib.sha256(png).hexdigest()
    return ScreenshotResult(
        device_id=stable_device_id,
        room_key=room_key,
        blank_or_protected=assessment.blank_or_protected,
        png_bytes=png,
        error=None,
        observer_udid_fingerprint=fingerprint_udid(udid) if udid else None,
        metadata=CaptureMetadata(
            latency_ms=latency_ms,
            sha256=digest,
            width=assessment.width,
            height=assessment.height,
            mean_luminance=assessment.mean_luminance,
            blank_reason=assessment.reason,
            capture_backend=capture_backend,
            worker_reused=worker_reused,
            worker_startup_ms=worker_startup_ms,
            argv_has_udid=bool(udid),
            argv_has_userspace=False,
            captured_at=(
                captured_at or datetime.now(UTC).isoformat()
            ),
        ),
    )
