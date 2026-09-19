"""Narrow URL validation used only by the compact agent bridge."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from home_media.errors import UnsupportedError

_APP_STORE_DETAIL_PATH = re.compile(
    r"^/(?:(?P<country>[A-Za-z]{2})/)?app/"
    r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*)/id(?P<app_id>[0-9]+)/?$"
)


def validate_app_store_detail_url(url: str) -> str:
    """Return one canonical Apple App Store detail URL or reject it."""

    raw = url.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsupportedError("content.url", reason="invalid App Store URL") from exc

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "apps.apple.com"
        or parsed.netloc.lower() != "apps.apple.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or "#" in raw
    ):
        raise UnsupportedError(
            "content.url",
            reason="App Store links require the exact secure apps.apple.com origin",
        )
    if parsed.query not in {"", "platform=tv"} or ("?" in raw and not parsed.query):
        raise UnsupportedError(
            "content.url",
            reason="App Store links allow only the platform=tv query",
        )

    match = _APP_STORE_DETAIL_PATH.fullmatch(parsed.path)
    if match is None:
        raise UnsupportedError(
            "content.url",
            reason="App Store links must be app detail paths ending in id<digits>",
        )

    country = match.group("country")
    country_path = f"/{country.lower()}" if country else ""
    canonical = (
        f"https://apps.apple.com{country_path}/app/"
        f"{match.group('slug')}/id{match.group('app_id')}"
    )
    if parsed.query:
        canonical = f"{canonical}?platform=tv"
    return canonical
