"""macOS Vision OCR via a small Swift helper (argv, no shell).

Source ships in the package under ``observers/native/vision_ocr.swift``.
A binary is compiled on demand into a private cache when ``swiftc`` is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from home_media.errors import UnsupportedError
from home_media.observers.blank import normalize_png_for_vision
from home_media.observers.ocr_types import OcrDocument, OcrToken

_HELPER_SOURCE_NAME = "vision_ocr.swift"
_HELPER_MODULE = "home_media.observers.native"
_BINARY_NAME = "vision_ocr"


def helper_source_path() -> Path | None:
    """Resolve packaged Swift source; None if missing from the install."""
    try:
        root = resources.files(_HELPER_MODULE)
        candidate = root.joinpath(_HELPER_SOURCE_NAME)
        with resources.as_file(candidate) as path:
            if path.is_file():
                return Path(path)
    except (FileNotFoundError, ModuleNotFoundError, TypeError, AttributeError, OSError):
        pass
    # Editable / source-tree fallback next to this module.
    local = Path(__file__).resolve().parent / "native" / _HELPER_SOURCE_NAME
    return local if local.is_file() else None


def helper_cache_dir() -> Path:
    override = os.environ.get("HOME_MEDIA_VISION_OCR_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "home-media" / "vision_ocr"


def resolve_helper_binary(*, compile_if_needed: bool = True) -> Path:
    """Return path to an executable vision_ocr binary or raise UnsupportedError."""
    env_bin = os.environ.get("HOME_MEDIA_VISION_OCR")
    if env_bin:
        path = Path(env_bin).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise UnsupportedError(
            "screenshot.ocr",
            reason=f"HOME_MEDIA_VISION_OCR is set but not an executable file: {path}",
        )

    source = helper_source_path()
    if source is None:
        raise UnsupportedError(
            "screenshot.ocr",
            reason=(
                "Vision OCR helper source missing from package "
                f"({_HELPER_MODULE}/{_HELPER_SOURCE_NAME})"
            ),
        )

    cache = helper_cache_dir()
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    binary = cache / f"{_BINARY_NAME}-{source_digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary

    if not compile_if_needed:
        raise UnsupportedError(
            "screenshot.ocr",
            reason="Vision OCR helper binary not compiled and compile_if_needed=False",
        )

    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise UnsupportedError(
            "screenshot.ocr",
            reason=(
                "swiftc not found; install Command Line Tools or set "
                "HOME_MEDIA_VISION_OCR to a prebuilt vision_ocr binary"
            ),
        )

    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="home-media-vision-ocr-") as tmp:
        out = Path(tmp) / _BINARY_NAME
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                swiftc,
                "-O",
                "-framework",
                "Vision",
                "-framework",
                "AppKit",
                "-o",
                str(out),
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not out.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()[:400]
            raise UnsupportedError(
                "screenshot.ocr",
                reason=f"swiftc failed building vision_ocr: {detail or 'unknown'}",
            )
        binary.write_bytes(out.read_bytes())
        binary.chmod(0o700)
    return binary


async def run_vision_ocr(png_bytes: bytes, *, timeout_s: float = 20.0) -> OcrDocument:
    """Invoke Vision OCR helper on PNG bytes; returns portable token geometry."""
    binary = await asyncio.to_thread(resolve_helper_binary, compile_if_needed=True)
    normalized = normalize_png_for_vision(png_bytes) or png_bytes
    with tempfile.TemporaryDirectory(prefix="home-media-ocr-img-") as tmp:
        png_path = Path(tmp) / "frame.png"
        png_path.write_bytes(normalized)
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            str(png_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise UnsupportedError(
                "screenshot.ocr",
                reason=f"vision_ocr timed out after {timeout_s:.1f}s",
            ) from exc
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()[:300]
            raise UnsupportedError(
                "screenshot.ocr",
                reason=f"vision_ocr failed (exit={proc.returncode}): {err or 'no stderr'}",
            )
        return parse_ocr_payload(stdout.decode("utf-8", errors="replace"))


def parse_ocr_payload(raw: str) -> OcrDocument:
    text = raw.strip()
    if not text:
        return OcrDocument()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnsupportedError(
            "screenshot.ocr",
            reason="vision_ocr returned non-JSON output",
        ) from exc
    tokens_raw = data.get("tokens") if isinstance(data, dict) else None
    tokens: list[OcrToken] = []
    if isinstance(tokens_raw, list):
        for item in tokens_raw:
            if not isinstance(item, dict):
                continue
            tokens.append(
                OcrToken(
                    text=str(item.get("text") or ""),
                    x=float(item.get("x") or 0.0),
                    y=float(item.get("y") or 0.0),
                    w=float(item.get("w") or 0.0),
                    h=float(item.get("h") or 0.0),
                )
            )
    width = data.get("width") if isinstance(data, dict) else None
    height = data.get("height") if isinstance(data, dict) else None
    return OcrDocument(
        tokens=tokens,
        width=int(width) if isinstance(width, int) else None,
        height=int(height) if isinstance(height, int) else None,
    )
