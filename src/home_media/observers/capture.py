"""Subprocess-boundary Apple TV screenshot capture via pymobiledevice3.

pymobiledevice3 is GPL-3.0. home-media never imports it. Capture uses an
explicit argv list (no shell) with ``--userspace --udid <exact-id>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from home_media.errors import ConfigError, TimeoutError_, UnsupportedError
from home_media.observers.binding import fingerprint_udid, redact_udid
from home_media.observers.blank import assess_blank_or_protected, is_png
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult

DEFAULT_CAPTURE_TIMEOUT_S = 45.0

CaptureRunner = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


@dataclass(frozen=True)
class CaptureCommand:
    argv: list[str]
    output_path: Path


def resolve_capture_python() -> str:
    """Python used to run the optional GPL capture worker.

    tvOS 26 Wi-Fi TCP tunnels need native TLS-PSK (Python >= 3.13). Prefer an
    explicit override, then a sibling ``.venv-pmd3``, then the current interpreter.
    """
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


def build_wifi_capture_argv(
    *,
    udid: str,
    output_path: Path,
    executable: str | None = None,
) -> list[str]:
    """Argv for the Wi-Fi remote-pair userspace worker (exact ``--udid``, no shell).

    Runs the worker file directly so the optional 3.14 interpreter does not import
    the full ``home_media.observers`` package (which needs core deps).
    """
    if not udid or not udid.strip():
        raise ConfigError("capture requires an exact observer UDID")
    exe = executable or resolve_capture_python()
    exe_name = Path(exe).name
    if exe_name == "pymobiledevice3":
        exe = sys.executable
    worker = Path(__file__).with_name("pmd3_wifi_capture.py")
    return [
        exe,
        str(worker),
        "--udid",
        udid,
        "--out",
        str(output_path),
    ]


def build_dvt_screenshot_argv(
    *,
    udid: str,
    output_path: Path,
    executable: str | None = None,
    userspace: bool = True,
) -> list[str]:
    """Legacy CLI argv (kept for tests). Prefer :func:`build_wifi_capture_argv` live."""
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


async def run_capture_subprocess(
    argv: Sequence[str],
    *,
    timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run capture argv with no shell. Returns (returncode, stdout, stderr)."""
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
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise TimeoutError_("Screenshot capture timed out") from exc
    return proc.returncode or 0, stdout or b"", stderr or b""


def _redact_capture_error(message: str, udid: str) -> str:
    redacted = message.replace(udid, redact_udid(udid) or "udid_fp:unknown")
    parts = redacted.split()
    cleaned: list[str] = []
    for part in parts:
        if ":" in part and any(ch.isdigit() for ch in part) and len(part) > 8:
            cleaned.append("[redacted_addr]")
        else:
            cleaned.append(part)
    return " ".join(cleaned)[:500]


class PyMobileDeviceScreenshotCapturer:
    """Capture PNG bytes for an exact UDID via subprocess boundary."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        userspace: bool = True,
        timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
        runner: CaptureRunner | None = None,
    ) -> None:
        self.executable = executable
        self.userspace = userspace
        self.timeout_s = timeout_s
        self._runner: CaptureRunner = runner or run_capture_subprocess

    async def capture_udid(
        self,
        udid: str,
        *,
        stable_device_id: str,
        room_key: str,
    ) -> ScreenshotResult:
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="home-media-shot-") as tmp:
            out_path = Path(tmp) / "capture.png"
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

            # Ensure the optional 3.14 interpreter can import home_media worker module.
            src_root = str(Path(__file__).resolve().parents[2])
            child_env = os.environ.copy()
            existing = child_env.get("PYTHONPATH", "")
            child_env["PYTHONPATH"] = (
                src_root if not existing else f"{src_root}{os.pathsep}{existing}"
            )
            try:
                if self._runner is run_capture_subprocess:
                    rc, stdout, stderr = await self._runner(
                        planned.argv, timeout_s=self.timeout_s, env=child_env
                    )
                else:
                    rc, stdout, stderr = await self._runner(
                        planned.argv, timeout_s=self.timeout_s
                    )
            except TimeoutError as exc:
                raise TimeoutError_("Screenshot capture timed out") from exc
            latency_ms = int((time.perf_counter() - t0) * 1000)
            if rc != 0:
                err_txt = (stderr or stdout).decode("utf-8", errors="replace")
                return ScreenshotResult(
                    device_id=stable_device_id,
                    room_key=room_key,
                    blank_or_protected=True,
                    png_bytes=None,
                    error=_redact_capture_error(err_txt or f"capture_exit_{rc}", udid),
                    tunnel_identity=None,
                    observer_udid_fingerprint=fingerprint_udid(udid),
                    metadata=CaptureMetadata(
                        latency_ms=latency_ms,
                        capture_backend="pymobiledevice3_wifi_remote_pair_tcp",
                        argv_has_udid=True,
                        argv_has_userspace=True,
                    ),
                )

            if not out_path.exists():
                return ScreenshotResult(
                    device_id=stable_device_id,
                    room_key=room_key,
                    blank_or_protected=True,
                    png_bytes=None,
                    error="capture produced no output file",
                    observer_udid_fingerprint=fingerprint_udid(udid),
                    metadata=CaptureMetadata(latency_ms=latency_ms),
                )
            png = out_path.read_bytes()
            return finalize_png_result(
                png,
                stable_device_id=stable_device_id,
                room_key=room_key,
                udid=udid,
                latency_ms=latency_ms,
            )


def finalize_png_result(
    png: bytes,
    *,
    stable_device_id: str,
    room_key: str,
    udid: str | None,
    latency_ms: int,
) -> ScreenshotResult:
    if not png or not is_png(png):
        return ScreenshotResult(
            device_id=stable_device_id,
            room_key=room_key,
            blank_or_protected=True,
            png_bytes=None,
            error="malformed_or_empty_png",
            observer_udid_fingerprint=fingerprint_udid(udid) if udid else None,
            metadata=CaptureMetadata(latency_ms=latency_ms, sha256=None),
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
            capture_backend="pymobiledevice3_dvt_userspace",
            argv_has_udid=bool(udid),
            argv_has_userspace=True,
            captured_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        ),
    )
