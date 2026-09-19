"""Optional GPL worker for an exact Apple TV Wi-Fi screenshot binding.

The core package starts this file as a subprocess and never imports
``pymobiledevice3``.  Persistent mode accepts only typed JSONL ``capture`` and
``shutdown`` requests.  A worker is permanently bound to the one observer UDID
passed on argv; a request can never select or switch devices.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

PROTOCOL_VERSION = 1
_MAX_REQUEST_LINE_BYTES = 16_384
# pymobiledevice3 9.36 caps the entire DTX reassembly pool at 30 MiB. A normal
# 3840x2160 Apple TV PNG can legitimately exceed that by a few megabytes, which
# otherwise tears down an authenticated tunnel nondeterministically. This
# process is the isolated screenshot worker, so widen only its DTX reader and
# retain an application-level ceiling before any bytes reach disk.
MAX_SCREENSHOT_MESSAGE_BYTES = 64 * 1024 * 1024


class WorkerStartupError(Exception):
    """Safe startup failure whose code contains no device details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CaptureSession(Protocol):
    observed_udid_fingerprint: str
    startup_ms: int
    capture_count: int

    async def __aenter__(self) -> CaptureSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def capture_to(self, out: Path) -> int: ...


SessionFactory = Callable[[str], CaptureSession]
ReadLine = Callable[[], Awaitable[bytes]]
WriteMessage = Callable[[dict[str, object]], Awaitable[None]]


def configure_dtx_screenshot_limit(reader_module: Any) -> None:
    """Raise pymobiledevice3's process-local DTX pool for 4K screenshots."""
    current = getattr(reader_module, "MAX_BUFFERED_SIZE", None)
    if not isinstance(current, int) or current < MAX_SCREENSHOT_MESSAGE_BYTES:
        reader_module.MAX_BUFFERED_SIZE = MAX_SCREENSHOT_MESSAGE_BYTES


def validate_screenshot_size(png: bytes) -> None:
    """Reject malformed/oversized screenshot payloads before writing them."""
    if not png or len(png) > MAX_SCREENSHOT_MESSAGE_BYTES:
        raise RuntimeError("screenshot_payload_size_invalid")


def fingerprint_udid(udid: str) -> str:
    """Return the same safe short fingerprint used by the parent process."""
    return hashlib.sha256(udid.encode("utf-8")).hexdigest()[:12]


async def _close_service(service: Any) -> None:
    with suppress(Exception):
        await service.close()


async def select_exact_service(services: Sequence[Any], udid: str) -> Any:
    """Select one exact-identity service and close every other candidate.

    ``get_remote_pairing_tunnel_services`` can return several connected service
    objects for different addresses of one Apple TV.  Keeping those objects open
    was harmless only while the worker exited after every capture.  Persistent
    mode must close all unselected candidates explicitly.
    """
    selected: Any | None = None
    ordered = sorted(
        services,
        key=lambda item: (1 if ":" in str(getattr(item, "hostname", "")) else 0),
    )
    for candidate in ordered:
        try:
            observed = str(candidate.remote_identifier)
        except Exception:  # noqa: BLE001 - malformed candidate is not usable
            await _close_service(candidate)
            continue
        if observed != udid or selected is not None:
            await _close_service(candidate)
            continue
        selected = candidate
    if selected is None:
        raise WorkerStartupError("observer_identity_unavailable")
    return selected


class PersistentScreenshotSession:
    """Own and reuse one exact device's tunnel/RSD/DVT/screenshot stack."""

    def __init__(self, udid: str) -> None:
        self._udid = udid
        self._stack = AsyncExitStack()
        self._screenshot: Any | None = None
        self.observed_udid_fingerprint = ""
        self.startup_ms = 0
        self.capture_count = 0

    async def __aenter__(self) -> Self:
        started = time.perf_counter()
        from pymobiledevice3.dtx import _reader as dtx_reader
        from pymobiledevice3.remote import tunnel_service
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
        from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

        configure_dtx_screenshot_limit(dtx_reader)

        services = await tunnel_service.get_remote_pairing_tunnel_services(udid=self._udid)
        if not services:
            raise WorkerStartupError("no_remote_pairing_tunnel_service")
        selected = await select_exact_service(services, self._udid)
        observed_identifier = str(selected.remote_identifier)
        if observed_identifier != self._udid:
            await _close_service(selected)
            raise WorkerStartupError("observer_identity_mismatch")

        await self._stack.__aenter__()
        self._stack.callback(setattr, tunnel_service, "USE_USERSPACE_TUNNEL", False)
        self._stack.push_async_callback(selected.close)
        tunnel_service.USE_USERSPACE_TUNNEL = True
        try:
            tunnel_result = await self._stack.enter_async_context(selected.start_tcp_tunnel())
            tun = tunnel_result.client.tun
            tun.set_peer(tunnel_result.address)
            dial_plane = await self._stack.enter_async_context(
                UserspaceDialPlane(tun, tunnel_result.address)
            )
            rsd = RemoteServiceDiscoveryService(
                (tunnel_result.address, tunnel_result.port),
                open_connection=dial_plane.dial,
            )
            await rsd.connect()
            self._stack.push_async_callback(rsd.close)
            dvt = await self._stack.enter_async_context(DvtProvider(rsd))
            self._screenshot = await self._stack.enter_async_context(Screenshot(dvt))
        except BaseException:
            await self._stack.aclose()
            raise

        self.observed_udid_fingerprint = fingerprint_udid(observed_identifier)
        self.startup_ms = max(0, int((time.perf_counter() - started) * 1000))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._screenshot = None
        await self._stack.aclose()

    async def capture_to(self, out: Path) -> int:
        if self._screenshot is None:
            raise RuntimeError("screenshot_session_not_started")
        started = time.perf_counter()
        png = await self._screenshot.get_screenshot()
        validate_screenshot_size(png)
        await asyncio.to_thread(out.write_bytes, png)
        self.capture_count += 1
        return max(0, int((time.perf_counter() - started) * 1000))


def _protocol_error(request_id: str, code: str = "invalid_request") -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "protocol_error",
        "request_id": request_id,
        "ok": False,
        "error_code": code,
    }


def _parse_request(raw: bytes) -> dict[str, str]:
    if not raw or len(raw) > _MAX_REQUEST_LINE_BYTES:
        raise ValueError("invalid_request_line")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid_request")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("protocol_version_mismatch")
    request_type = value.get("type")
    request_id = value.get("request_id")
    if request_type not in {"capture", "shutdown"} or not isinstance(request_id, str):
        raise ValueError("invalid_request")
    if not request_id or len(request_id) > 64:
        raise ValueError("invalid_request_id")
    if request_type == "capture":
        if set(value) != {"protocol_version", "type", "request_id", "out"}:
            raise ValueError("invalid_capture_request")
        out = value.get("out")
        if not isinstance(out, str) or not out or not Path(out).is_absolute():
            raise ValueError("invalid_output_path")
        return {"type": request_type, "request_id": request_id, "out": out}
    if set(value) != {"protocol_version", "type", "request_id"}:
        raise ValueError("invalid_shutdown_request")
    return {"type": request_type, "request_id": request_id}


async def serve_jsonl(
    udid: str,
    read_line: ReadLine,
    write_message: WriteMessage,
    *,
    session_factory: SessionFactory = PersistentScreenshotSession,
) -> None:
    """Serve typed requests until shutdown, EOF, or a failed capture."""
    try:
        async with session_factory(udid) as session:
            while True:
                raw = await read_line()
                if not raw:
                    break
                request_id = "unknown"
                try:
                    request = _parse_request(raw)
                    request_id = request["request_id"]
                except Exception:  # noqa: BLE001 - emit only a fixed safe protocol error
                    await write_message(_protocol_error(request_id))
                    continue

                if request["type"] == "shutdown":
                    await write_message(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "type": "shutdown_result",
                            "request_id": request_id,
                            "ok": True,
                        }
                    )
                    break

                reused = session.capture_count > 0
                try:
                    latency_ms = await session.capture_to(Path(request["out"]))
                except Exception:  # noqa: BLE001 - dependency errors never cross stdout
                    await write_message(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "type": "capture_result",
                            "request_id": request_id,
                            "ok": False,
                            "error_code": "capture_failed",
                            "observer_udid_fingerprint": session.observed_udid_fingerprint,
                        }
                    )
                    break
                await write_message(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "type": "capture_result",
                        "request_id": request_id,
                        "ok": True,
                        "observer_udid_fingerprint": session.observed_udid_fingerprint,
                        "capture_latency_ms": latency_ms,
                        "worker_reused": reused,
                        "worker_startup_ms": None if reused else session.startup_ms,
                        "capture_count": session.capture_count,
                    }
                )
    except WorkerStartupError as exc:
        await write_message(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "worker_error",
                "ok": False,
                "error_code": exc.code,
            }
        )
    except Exception:  # noqa: BLE001 - never serialize dependency exception text
        await write_message(
            {
                "protocol_version": PROTOCOL_VERSION,
                "type": "worker_error",
                "ok": False,
                "error_code": "worker_start_failed",
            }
        )


async def capture(udid: str, out: Path) -> None:
    """Explicit one-shot fallback using the same exact-identity session."""
    async with PersistentScreenshotSession(udid) as session:
        await session.capture_to(out)


async def _stdin_readline() -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline)


async def _stdout_write(message: dict[str, object]) -> None:
    encoded = json.dumps(message, separators=(",", ":"), sort_keys=True)
    await asyncio.to_thread(_write_stdout_line, encoded)


def _write_stdout_line(encoded: str) -> None:
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pmd3_wifi_capture")
    parser.add_argument("--udid", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path)
    mode.add_argument("--serve-jsonl", action="store_true")
    args = parser.parse_args(argv)
    udid = args.udid.strip()
    if not udid:
        return 2
    if sys.version_info < (3, 13):
        if args.serve_jsonl:
            _write_stdout_line(
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "type": "worker_error",
                        "ok": False,
                        "error_code": "python_version_unsupported",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return 4

    # Dependency logging can contain addresses or identifiers. The protocol on
    # stdout is deliberately the only diagnostic surface consumed by core.
    logging.disable(logging.CRITICAL)
    if args.serve_jsonl:
        asyncio.run(serve_jsonl(udid, _stdin_readline, _stdout_write))
        return 0
    try:
        assert args.out is not None
        asyncio.run(capture(udid, args.out))
    except WorkerStartupError:
        return 3
    except Exception:  # noqa: BLE001 - one-shot parent maps exit to a safe code
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
