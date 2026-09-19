"""Credential file protections retained from the original safety suite."""

import os
from pathlib import Path

import pytest

from home_media.config import create_private_file
from home_media.errors import ConfigError


def test_create_private_file_is_exclusive_mode_0600_and_rejects_unsafe_existing(
    tmp_path: Path,
) -> None:
    private = create_private_file(tmp_path / "private.json")
    assert oct(private.stat().st_mode & 0o777) == "0o600"

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text("", encoding="utf-8")
    os.chmod(unsafe, 0o644)
    with pytest.raises(ConfigError, match="0600"):
        create_private_file(unsafe)
    assert oct(unsafe.stat().st_mode & 0o777) == "0o644"

    link = tmp_path / "private-link.json"
    link.symlink_to(private)
    with pytest.raises(ConfigError, match="symlink"):
        create_private_file(link)
