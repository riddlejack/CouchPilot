"""Private screenshot retention with short default lifetime."""

from __future__ import annotations

import os
import time
from pathlib import Path

from home_media.config import ensure_private_dir, write_private_bytes

DEFAULT_SCREENSHOT_DIR = Path.home() / ".config" / "home-media" / "screenshots"
DEFAULT_RETENTION_S = 24 * 60 * 60  # 24h


def screenshot_dir_from_env() -> Path:
    override = os.environ.get("HOME_MEDIA_SCREENSHOT_DIR")
    if override:
        return ensure_private_dir(Path(override).expanduser())
    # Prefer repo-local .private when present (dev), else config dir.
    repo_private = Path.cwd() / ".private" / "screenshots"
    if (Path.cwd() / ".private").exists() or os.environ.get("HOME_MEDIA_USE_REPO_PRIVATE") == "1":
        return ensure_private_dir(repo_private)
    return ensure_private_dir(DEFAULT_SCREENSHOT_DIR)


def save_png_private(
    png_bytes: bytes,
    *,
    room_key: str,
    sha256: str,
    directory: Path | None = None,
) -> Path:
    root = ensure_private_dir(directory or screenshot_dir_from_env())
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{room_key}_{stamp}_{sha256[:12]}.png"
    path = root / name
    return write_private_bytes(path, png_bytes)


def prune_screenshots(
    directory: Path | None = None,
    *,
    max_age_s: float = DEFAULT_RETENTION_S,
) -> int:
    root = directory or screenshot_dir_from_env()
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for path in root.glob("*.png"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > max_age_s:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
