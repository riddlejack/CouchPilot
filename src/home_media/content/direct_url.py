"""Direct URL and saved-alias content resolution."""

from __future__ import annotations

from home_media.errors import UnsupportedError
from home_media.models import ContentTarget

PROVIDER_HINTS = {
    "netflix.com": ("netflix", "com.netflix.Netflix"),
    "www.netflix.com": ("netflix", "com.netflix.Netflix"),
    "tv.apple.com": ("apple_tv_plus", "com.apple.TVWatchList"),
    "www.disneyplus.com": ("disney_plus", "com.disney.disneyplus"),
    "disneyplus.com": ("disney_plus", "com.disney.disneyplus"),
    "play.hbomax.com": ("max", "com.hbo.hbonow"),
    "www.max.com": ("max", "com.hbo.hbonow"),
    "max.com": ("max", "com.hbo.hbonow"),
    "www.youtube.com": ("youtube", "com.google.ios.youtube"),
    "youtube.com": ("youtube", "com.google.ios.youtube"),
    "youtu.be": ("youtube", "com.google.ios.youtube"),
}

# Field evidence (July 2026): Netflix HTTPS deep links often only open the app on
# Apple TV since ~Sept 2025 despite AASA/pyatv docs. Apple TV+ share links are the
# strongest public evidence. Confidence is for planners/agents — not verification.
PROVIDER_CONFIDENCE = {
    "apple_tv_plus": 0.95,
    "disney_plus": 0.8,
    "max": 0.65,
    "netflix": 0.35,
    "youtube": 0.4,
}

PROVIDER_WARNINGS = {
    "netflix": (
        "Netflix deep links are frequently app-only on recent tvOS; "
        "expect opened_app_only unless now-playing proves content"
    ),
    "youtube": (
        "YouTube tvOS deep-link behavior is community-reported only; verify on device"
    ),
}


class DirectURLResolver:
    name = "direct_url"

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = {k.lower(): v for k, v in (aliases or {}).items()}

    def resolve(
        self,
        *,
        url: str | None = None,
        alias: str | None = None,
        title: str | None = None,
    ) -> ContentTarget:
        source = "direct_url"
        if alias and not url:
            url = self.aliases.get(alias.lower())
            source = "alias"
            if not url:
                raise UnsupportedError("content.alias", reason=f"Unknown alias '{alias}'")
        if not url:
            raise UnsupportedError("content.open", reason="url or alias required")

        from home_media.content.urls import validate_content_url

        # Allowlisted http(s) hosts + Netflix-only nflx:// title links.
        validated = validate_content_url(url)
        provider = validated.provider
        app = None
        if provider:
            # Map provider → expected app via host hints when possible.
            for _host, (hint_provider, hint_app) in PROVIDER_HINTS.items():
                if hint_provider == provider:
                    app = hint_app
                    break
        if app is None and validated.host:
            _provider, app = PROVIDER_HINTS.get(validated.host, (None, None))
        confidence = PROVIDER_CONFIDENCE.get(provider or "", 0.6 if provider else 0.5)
        if validated.url_form == "nflx":
            confidence = min(confidence, 0.45)  # experimental until live matrix
        target = ContentTarget(
            provider=provider,
            url=validated.url,
            app_bundle_id=app,
            title=title,
            resolution_source=source,  # type: ignore[arg-type]
            confidence=confidence,
            expected_app=app,
        )
        return target

    def warning_for(self, target: ContentTarget) -> str | None:
        if not target.provider:
            return None
        return PROVIDER_WARNINGS.get(target.provider)
