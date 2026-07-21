"""Provider-allowlisted URL validation for Companion deep links.

Only http(s) hosts from PROVIDER_HINTS are accepted in general. Netflix
additionally allows the ``nflx://`` title-detail form. Playback paths
(``/watch/``, episode URLs), ``airplay://``, and arbitrary schemes are rejected.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from home_media.content.direct_url import PROVIDER_HINTS
from home_media.errors import UnsupportedError

_NETFLIX_TITLE_PATH_RE = re.compile(r"^/title/(?P<id>\d+)/?$")


class ParsedContentURL(BaseModel):
    """Normalized, allowlisted content deep link."""

    scheme: str
    host: str
    path: str
    provider: str | None = None
    content_id: str | None = None
    url_form: str = Field(description="Canonical form label, e.g. https or nflx")
    may_start_playback: bool = False
    url: str


def _host_provider(host: str) -> tuple[str | None, str | None]:
    return PROVIDER_HINTS.get(host.lower(), (None, None))


def _is_playback_path(path: str, *, provider: str | None) -> bool:
    lowered = path.lower()
    if "/watch/" in lowered or lowered.rstrip("/").endswith("/watch"):
        return True
    if "/episode/" in lowered or "/episodes/" in lowered:
        return True
    if "/play/" in lowered:
        return True
    # Non-Netflix /title/ paths are treated as unknown; Netflix /title/<id> is detail-only.
    if provider == "netflix":
        return False
    return False


def _parse_netflix_title_path(path: str) -> str | None:
    match = _NETFLIX_TITLE_PATH_RE.match(path)
    if not match:
        return None
    return match.group("id")


def validate_content_url(url: str, *, provider: str | None = None) -> ParsedContentURL:
    """Validate a Companion deep-link URL against the provider allowlist.

    Always allows http/https for hosts in ``PROVIDER_HINTS``. For Netflix only,
    also allows ``nflx://www.netflix.com/title/<digits>`` (series/title detail).
    Rejects ``watch/``, episode playback paths, ``airplay://``, and arbitrary schemes.
    """
    raw = url.strip()
    if not raw:
        raise UnsupportedError("content.url", reason="empty URL")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"

    if scheme == "airplay":
        raise UnsupportedError(
            "content.url",
            reason="airplay:// streaming URLs are not accepted Companion deep links",
        )

    hint_provider, _app = _host_provider(host) if host else (None, None)
    effective_provider = provider or hint_provider

    if scheme == "nflx":
        if effective_provider and effective_provider != "netflix":
            raise UnsupportedError(
                "content.url",
                reason=f"nflx:// is only valid for netflix, not {effective_provider}",
            )
        if host not in {"www.netflix.com", "netflix.com"}:
            raise UnsupportedError(
                "content.url",
                reason="nflx:// host must be www.netflix.com",
            )
        content_id = _parse_netflix_title_path(path)
        if content_id is None:
            raise UnsupportedError(
                "content.url",
                reason="nflx:// path must be /title/<digits> (title detail only; no watch/)",
            )
        normalized_path = f"/title/{content_id}"
        normalized = f"nflx://www.netflix.com{normalized_path}"
        return ParsedContentURL(
            scheme="nflx",
            host="www.netflix.com",
            path=normalized_path,
            provider="netflix",
            content_id=content_id,
            url_form="nflx",
            may_start_playback=False,
            url=normalized,
        )

    if scheme not in {"http", "https"}:
        raise UnsupportedError(
            "content.url",
            reason=(
                f"Scheme '{scheme or '(none)'}' is not allowlisted; "
                "only http(s) for known hosts, plus Netflix nflx:// title links"
            ),
        )

    if not host or host not in PROVIDER_HINTS:
        raise UnsupportedError(
            "content.url",
            reason=f"Host '{host or '(none)'}' is not in the provider allowlist",
        )

    if provider and hint_provider and provider != hint_provider:
        raise UnsupportedError(
            "content.url",
            reason=f"URL host provider '{hint_provider}' does not match requested '{provider}'",
        )

    resolved_provider = hint_provider
    content_id = None

    if resolved_provider == "netflix":
        if "/watch/" in path.lower() or path.lower().rstrip("/").endswith("/watch"):
            raise UnsupportedError(
                "content.url",
                reason="Netflix /watch/ playback paths are rejected; use /title/<id> only",
            )
        if "/episode/" in path.lower() or "/episodes/" in path.lower():
            raise UnsupportedError(
                "content.url",
                reason="Netflix episode playback paths are rejected; use /title/<id> only",
            )
        content_id = _parse_netflix_title_path(path)
        # Allow other https paths on Netflix host (app open), but mark title detail when matched.
    elif _is_playback_path(path, provider=resolved_provider):
        raise UnsupportedError(
            "content.url",
            reason="Playback/watch paths are rejected for Companion deep links",
        )

    may_start = False
    # Title-detail Netflix links do not claim playback start.
    if resolved_provider == "netflix" and content_id is not None:
        path = f"/title/{content_id}"

    normalized = f"{scheme}://{host}{path}"
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"

    return ParsedContentURL(
        scheme=scheme,
        host=host,
        path=path,
        provider=resolved_provider,
        content_id=content_id,
        url_form=scheme,
        may_start_playback=may_start,
        url=normalized,
    )


def netflix_title_url_candidates(content_id: str) -> list[str]:
    """Return https then nflx title-detail URL forms for a Netflix content id."""
    digits = content_id.strip()
    if not digits.isdigit():
        raise UnsupportedError(
            "content.url",
            reason="Netflix content_id must be digits",
        )
    return [
        f"https://www.netflix.com/title/{digits}",
        f"nflx://www.netflix.com/title/{digits}",
    ]
