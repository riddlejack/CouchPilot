"""Configuration loading and credential path conventions."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from home_media.errors import ConfigError

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "home-media"
DEFAULT_PYATV_STORAGE = DEFAULT_CONFIG_DIR / "pyatv.conf"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    ensure_private_dir(path.parent)
    if path.exists():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return path


def create_private_file(path: Path) -> Path:
    """Create an empty mode-0600 file, or validate an existing private file."""
    ensure_private_dir(path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return validate_private_file(path)
    except OSError as exc:
        raise ConfigError("Private runtime file could not be created safely") from exc
    else:
        os.close(fd)
    return validate_private_file(path)


def validate_private_file(path: Path, *, label: str = "private runtime file") -> Path:
    """Fail closed before reading secrets with unsafe ownership/type/mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ConfigError(f"{label} not found") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{label} must be a regular file, not a symlink")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ConfigError(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError(f"{label} permissions must be 0600 or stricter")
    return path


def write_private_bytes(path: Path, payload: bytes) -> Path:
    """Atomically replace a private file without a world-readable creation window."""
    ensure_private_dir(path.parent)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        ensure_private_file(path)
        return path
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_private_text(path: Path, text: str) -> Path:
    return write_private_bytes(path, text.encode("utf-8"))


def pyatv_storage_path() -> Path:
    override = os.environ.get("HOME_MEDIA_PYATV_STORAGE")
    path = Path(override).expanduser() if override else DEFAULT_PYATV_STORAGE
    ensure_private_dir(path.parent)
    return path
