"""Versioned provider UI recipes with confidence decay."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from home_media.config import ensure_private_dir, ensure_private_file
from home_media.errors import SafetyBlockedError
from home_media.models import utcnow
from home_media.providers.base import ProviderState

DEFAULT_MIN_CONFIDENCE = 0.3
AGE_HALVE_DAYS = 30.0


class Recipe(BaseModel):
    provider: str
    name: str
    version: int = 1
    tvos_version: str | None = None
    app_version: str | None = None
    start_state: ProviderState
    end_state: ProviderState
    steps: list[dict[str, Any]] = Field(default_factory=list)
    evidence_anchors: list[str] = Field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    last_verified_at: datetime | None = None
    confidence: float = 1.0


def decayed_confidence(
    recipe: Recipe,
    *,
    now: datetime | None = None,
    age_halve_days: float = AGE_HALVE_DAYS,
) -> float:
    """Apply age-based decay (halve after ``age_halve_days`` without verify)."""
    current = now or utcnow()
    confidence = recipe.confidence
    if recipe.last_verified_at is None:
        return confidence
    verified = recipe.last_verified_at
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=UTC)
    age = current - verified
    if age <= timedelta(0):
        return confidence
    half_lives = age.total_seconds() / (age_halve_days * 86400.0)
    decayed: float = float(confidence) * (0.5**half_lives)
    return decayed


class RecipeStore:
    """In-memory recipe store with optional JSON persistence."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.path = path
        self.min_confidence = min_confidence
        self._recipes: dict[str, Recipe] = {}
        if path is not None:
            self._load()

    @staticmethod
    def _key(provider: str, name: str, version: int) -> str:
        return f"{provider.lower()}::{name}::v{version}"

    def get(self, provider: str, name: str, version: int | None = None) -> Recipe | None:
        if version is not None:
            return self._recipes.get(self._key(provider, name, version))
        matches = [
            r
            for r in self._recipes.values()
            if r.provider.lower() == provider.lower() and r.name == name
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version)

    def put(self, recipe: Recipe) -> Recipe:
        self._recipes[self._key(recipe.provider, recipe.name, recipe.version)] = recipe
        self._save()
        return recipe

    def record_success(self, recipe: Recipe) -> Recipe:
        updated = recipe.model_copy(
            update={
                "success_count": recipe.success_count + 1,
                "last_verified_at": utcnow(),
                "confidence": min(1.0, max(recipe.confidence, 0.5)),
            }
        )
        return self.put(updated)

    def record_failure(self, recipe: Recipe) -> Recipe:
        updated = recipe.model_copy(
            update={
                "failure_count": recipe.failure_count + 1,
                "confidence": recipe.confidence * 0.5,
            }
        )
        return self.put(updated)

    def assert_executable(self, recipe: Recipe, *, now: datetime | None = None) -> float:
        """Return effective confidence or raise if below execution floor."""
        effective = decayed_confidence(recipe, now=now)
        if effective < self.min_confidence:
            raise SafetyBlockedError(
                (
                    f"Recipe {recipe.provider}/{recipe.name}@v{recipe.version} "
                    f"confidence {effective:.3f} below {self.min_confidence}"
                ),
                reason="recipe_confidence_too_low",
            )
        return effective

    def list_for_provider(self, provider: str) -> list[Recipe]:
        return [r for r in self._recipes.values() if r.provider.lower() == provider.lower()]

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            recipe = Recipe.model_validate(item)
            self._recipes[self._key(recipe.provider, recipe.name, recipe.version)] = recipe

    def _save(self) -> None:
        if self.path is None:
            return
        ensure_private_dir(self.path.parent)
        items = [r.model_dump(mode="json") for r in self._recipes.values()]
        ensure_private_file(self.path)
        self.path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
