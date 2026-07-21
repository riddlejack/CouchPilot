"""Content resolution contracts."""

from __future__ import annotations

from typing import Protocol

from home_media.models import ContentTarget


class ContentResolver(Protocol):
    name: str

    def resolve(
        self,
        *,
        url: str | None = None,
        alias: str | None = None,
        title: str | None = None,
    ) -> ContentTarget: ...
