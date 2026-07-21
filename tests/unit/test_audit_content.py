"""Redaction and content resolver tests."""

from __future__ import annotations

import pytest

from home_media.audit import REDACTED, redact_obj, redact_text
from home_media.content.direct_url import DirectURLResolver
from home_media.errors import UnsupportedError
from home_media.models import ContentOutcome
from home_media.service import ApplicationService


def test_redacts_pin_and_tokens() -> None:
    text = "pin=1234&access_token=abc&Authorization=Bearer secrettoken"
    out = redact_text(text)
    assert "1234" not in out
    assert "secrettoken" not in out
    assert REDACTED in out


def test_redact_obj_nested() -> None:
    payload = {"credentials": "secret", "ok": {"token": "x", "title": "Show"}}
    out = redact_obj(payload)
    assert out["credentials"] == REDACTED
    assert out["ok"]["token"] == REDACTED
    assert out["ok"]["title"] == "Show"


def test_direct_url_netflix_low_confidence() -> None:
    resolver = DirectURLResolver()
    target = resolver.resolve(url="https://www.netflix.com/title/80234304")
    assert target.provider == "netflix"
    assert target.expected_app == "com.netflix.Netflix"
    assert target.confidence < 0.5
    assert resolver.warning_for(target)


def test_direct_url_apple_tv_plus_high_confidence() -> None:
    target = DirectURLResolver().resolve(
        url="https://tv.apple.com/show/severance/umc.cmc.1srk2goyh2q2zdxcx605w8vtx"
    )
    assert target.provider == "apple_tv_plus"
    assert target.confidence >= 0.9


def test_alias_resolution() -> None:
    resolver = DirectURLResolver({"drive": "https://www.netflix.com/title/80234304"})
    target = resolver.resolve(alias="drive")
    assert target.resolution_source == "alias"


def test_rejects_airplay_scheme() -> None:
    with pytest.raises(UnsupportedError, match="airplay"):
        DirectURLResolver().resolve(url="airplay://movie")


@pytest.mark.asyncio
async def test_resume_does_not_claim_exact_without_evidence_gap() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    # Fake open_url sets now_playing — verified_playback allowed when evidence exists
    result = await svc.open_content(
        "theater",
        url="https://www.netflix.com/title/80234304",
        resume=True,
    )
    assert result.content_outcome in {
        ContentOutcome.VERIFIED_PLAYBACK,
        ContentOutcome.OPENED_TARGET,
        ContentOutcome.OPENED_APP_ONLY,
    }
    assert any("resume" in w.lower() or "progress" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_open_app_only_outcome_when_no_url() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    result = await svc.open_app("theater", "youtube")
    assert result.execution_status.value == "succeeded"
    assert result.steps[0].observed_after["current_app"]
