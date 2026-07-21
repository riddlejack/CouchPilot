"""Configuration loading and credential path conventions."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import yaml

from home_media.errors import ConfigError
from home_media.models import DeviceKind, DeviceRef, HomeConfig, RoomConfig

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "home-media"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "homes.yaml"
DEFAULT_CREDENTIALS_DIR = DEFAULT_CONFIG_DIR / "credentials"
DEFAULT_PYATV_STORAGE = DEFAULT_CONFIG_DIR / "pyatv.conf"
DEFAULT_ANDROID_CERT_DIR = DEFAULT_CREDENTIALS_DIR / "androidtv"


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


def config_path_from_env() -> Path:
    override = os.environ.get("HOME_MEDIA_CONFIG")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def credentials_dir() -> Path:
    override = os.environ.get("HOME_MEDIA_CREDENTIALS_DIR")
    if override:
        return ensure_private_dir(Path(override).expanduser())
    return ensure_private_dir(DEFAULT_CREDENTIALS_DIR)


def pyatv_storage_path() -> Path:
    override = os.environ.get("HOME_MEDIA_PYATV_STORAGE")
    if override:
        return ensure_private_file(Path(override).expanduser())
    return ensure_private_file(DEFAULT_PYATV_STORAGE)


def android_cert_dir() -> Path:
    return ensure_private_dir(DEFAULT_ANDROID_CERT_DIR)


def load_home_config(path: Path | None = None) -> HomeConfig:
    cfg_path = path or config_path_from_env()
    if not cfg_path.exists():
        raise ConfigError(
            f"Config not found at {cfg_path}. Copy config/homes.example.yaml to "
            f"{DEFAULT_CONFIG_PATH} or set HOME_MEDIA_CONFIG."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")
    try:
        return HomeConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — surface validation clearly
        raise ConfigError(f"Invalid config: {exc}") from exc


def theater_seed_config() -> HomeConfig:
    """Deterministic, sanitized Theater-first seed for fakes and examples."""
    rooms = [
        RoomConfig(
            key="theater",
            display_name="Theater",
            aliases=["theater tv", "workout room", "the theater"],
            apple_tv_id="00000000-0000-4000-8000-000000000001",
            physical_tv_id="sony-theater-tv",
            audio_id="sonos-theater-beam",
            preferred_volume_target="sonos",
            preferred_power_path="both",
        ),
        RoomConfig(
            key="family_room",
            display_name="Family Room",
            aliases=["family room"],
            apple_tv_id="00000000-0000-4000-8000-000000000002",
            audio_id="sonos-family-room-arc",
            preferred_volume_target="sonos",
        ),
        RoomConfig(
            key="bedroom",
            display_name="Bedroom",
            aliases=["bedroom"],
            apple_tv_id="00000000-0000-4000-8000-000000000003",
            audio_id="sonos-bedroom-arc",
            preferred_volume_target="sonos",
        ),
        RoomConfig(
            key="living_room",
            display_name="Living Room",
            aliases=["living room"],
            apple_tv_id="00000000-0000-4000-8000-000000000004",
            audio_id="sonos-living-room-beam",
            preferred_volume_target="sonos",
        ),
        RoomConfig(
            key="office",
            display_name="Office",
            aliases=["office", "office 2", "office (2)"],
            apple_tv_id="00000000-0000-4000-8000-000000000005",
            preferred_volume_target="none",
        ),
        RoomConfig(
            key="study",
            display_name="Study",
            aliases=["study"],
            physical_tv_id="sony-study",
            preferred_volume_target="none",
        ),
    ]
    devices = [
        DeviceRef(
            id="00000000-0000-4000-8000-000000000001",
            kind=DeviceKind.APPLE_TV,
            room_key="theater",
            adapter="apple_tv",
            aliases=["Theater Apple TV"],
            vendor="Apple",
            model="Apple TV 4K (gen 2)",
        ),
        DeviceRef(
            id="sony-theater-tv",
            kind=DeviceKind.PHYSICAL_TV,
            room_key="theater",
            adapter="android_tv",
            aliases=["Theater TV", "Theater Sony"],
            vendor="Sony",
            model="BRAVIA 4K GB",
        ),
        DeviceRef(
            id="sonos-theater-beam",
            kind=DeviceKind.AUDIO,
            room_key="theater",
            adapter="sonos",
            aliases=["Theater", "Theater Beam", "Theater Sonos"],
            vendor="Sonos",
            model="Beam",
            vendor_stable_id="RINCON_00000000000101400",
        ),
        DeviceRef(
            id="00000000-0000-4000-8000-000000000002",
            kind=DeviceKind.APPLE_TV,
            room_key="family_room",
            adapter="apple_tv",
            aliases=["Family Room Apple TV"],
            vendor="Apple",
            model="Apple TV 4K (gen 2)",
        ),
        DeviceRef(
            id="sonos-family-room-arc",
            kind=DeviceKind.AUDIO,
            room_key="family_room",
            adapter="sonos",
            aliases=["Family Room", "Family Room Arc"],
            vendor="Sonos",
            model="Arc",
            vendor_stable_id="RINCON_00000000000201400",
        ),
        DeviceRef(
            id="00000000-0000-4000-8000-000000000003",
            kind=DeviceKind.APPLE_TV,
            room_key="bedroom",
            adapter="apple_tv",
            aliases=["Bedroom Apple TV"],
            vendor="Apple",
            model="Apple TV 4K (gen 2)",
        ),
        DeviceRef(
            id="sonos-bedroom-arc",
            kind=DeviceKind.AUDIO,
            room_key="bedroom",
            adapter="sonos",
            aliases=["Bedroom", "Bedroom Arc"],
            vendor="Sonos",
            model="Arc",
            vendor_stable_id="RINCON_00000000000301400",
        ),
        DeviceRef(
            id="00000000-0000-4000-8000-000000000004",
            kind=DeviceKind.APPLE_TV,
            room_key="living_room",
            adapter="apple_tv",
            aliases=["Living Room Apple TV"],
            vendor="Apple",
            model="Apple TV 4K (gen 3)",
        ),
        DeviceRef(
            id="sonos-living-room-beam",
            kind=DeviceKind.AUDIO,
            room_key="living_room",
            adapter="sonos",
            aliases=["Living Room", "Living Room Beam"],
            vendor="Sonos",
            model="Beam",
            vendor_stable_id="RINCON_00000000000401400",
        ),
        DeviceRef(
            id="00000000-0000-4000-8000-000000000005",
            kind=DeviceKind.APPLE_TV,
            room_key="office",
            adapter="apple_tv",
            aliases=["Office (2)", "Office Apple TV"],
            vendor="Apple",
            model="Apple TV 4K",
        ),
        DeviceRef(
            id="sony-study",
            kind=DeviceKind.PHYSICAL_TV,
            room_key="study",
            adapter="android_tv",
            aliases=["Study"],
            vendor="Sony",
            model="BRAVIA 4K GB",
        ),
    ]
    return HomeConfig(
        home_name="primary",
        mutations_enabled=True,
        volume_ceiling=40,
        rooms=rooms,
        devices=devices,
        app_aliases={
            "netflix": "com.netflix.Netflix",
            "youtube": "com.google.ios.youtube",
            "disney": "com.disney.disneyplus",
            "disney+": "com.disney.disneyplus",
            "max": "com.hbo.hbonow",
            "hbo": "com.hbo.hbonow",
            "appletv": "com.apple.TVWatchList",
            "apple tv": "com.apple.TVWatchList",
            "apple tv+": "com.apple.TVWatchList",
        },
        content_aliases={
            "avatar-tla": "https://www.netflix.com/title/70142405",
            "avatar the last airbender": "https://www.netflix.com/title/70142405",
        },
        provider_prefs={
            "netflix": {
                "profile_name": "primary",
                "profile_index": 2,  # 1-based audit metadata only
                "profile_select": "second",
            }
        },
    )


def write_example_config(path: Path) -> None:
    cfg = theater_seed_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(mode="json")
    path.write_text(
        "# Sanitized example / fake seed. Replace identifiers from private live discovery.\n"
        "# Zero-prefixed UUIDs below are not real household devices.\n"
        + yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
