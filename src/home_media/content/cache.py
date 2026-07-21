"""In-memory (optional JSON-backed) cache of verified content targets."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from home_media.config import ensure_private_dir, ensure_private_file
from home_media.models import utcnow

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip light punctuation, collapse whitespace."""
    lowered = title.casefold().strip()
    stripped = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).strip()


class VerifiedTarget(BaseModel):
    normalized_title: str
    provider: str
    provider_content_id: str
    canonical_url: str
    successful_url_form: str
    episode_url: str | None = None
    resolution_source: str
    confidence: float = 1.0
    last_verified_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)


class VerifiedTargetCache:
    """Cache of previously verified deep-link targets.

    When ``path`` is set, entries are persisted as JSON under the config directory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._by_content_id: dict[str, VerifiedTarget] = {}
        self._by_title: dict[str, VerifiedTarget] = {}
        if path is not None:
            self._load()

    @staticmethod
    def _content_key(provider: str, provider_content_id: str) -> str:
        return f"{provider.lower()}::{provider_content_id}"

    @staticmethod
    def _title_key(provider: str, title: str) -> str:
        return f"{provider.lower()}::{normalize_title(title)}"

    def get(self, provider: str, provider_content_id: str) -> VerifiedTarget | None:
        return self._by_content_id.get(self._content_key(provider, provider_content_id))

    def put(self, target: VerifiedTarget) -> VerifiedTarget:
        stored = target.model_copy(
            update={"normalized_title": normalize_title(target.normalized_title)}
        )
        self._by_content_id[self._content_key(stored.provider, stored.provider_content_id)] = stored
        self._by_title[self._title_key(stored.provider, stored.normalized_title)] = stored
        self._save()
        return stored

    def lookup_by_title(self, provider: str, title: str) -> VerifiedTarget | None:
        return self._by_title.get(self._title_key(provider, title))

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            target = VerifiedTarget.model_validate(item)
            self._by_content_id[self._content_key(target.provider, target.provider_content_id)] = (
                target
            )
            self._by_title[self._title_key(target.provider, target.normalized_title)] = target

    def _save(self) -> None:
        if self.path is None:
            return
        ensure_private_dir(self.path.parent)
        items = [t.model_dump(mode="json") for t in self._by_content_id.values()]
        ensure_private_file(self.path)
        self.path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
