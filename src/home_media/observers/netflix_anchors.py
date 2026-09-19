"""Deterministic Netflix chrome/text-geometry anchors from OCR tokens.

Artwork / poster pixels are never required. Classification uses chrome and
query text geometry only so unit tests can inject synthetic token fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

from home_media.content.prepare import normalize_query, queries_match
from home_media.observers.ocr_types import OcrDocument, OcrToken
from home_media.providers.base import ProviderState

# Anchor codes are safe for ordinary action logs (no free OCR dumps).
ANCHOR_PROFILE_CHOOSE = "netflix.chrome.choose_profile"
ANCHOR_PROFILE_WHOS = "netflix.chrome.whos_watching"
ANCHOR_HOME_NAV = "netflix.chrome.top_nav"
ANCHOR_SEARCH_CHROME = "netflix.chrome.search"
ANCHOR_KEYBOARD = "netflix.chrome.keyboard"
ANCHOR_CATEGORY = "netflix.chrome.category_rail"
ANCHOR_RESULTS = "netflix.chrome.results"
ANCHOR_QUERY_VISIBLE = "netflix.chrome.query_visible"
ANCHOR_QUERY_POPULATED = "netflix.chrome.query_populated"
ANCHOR_TITLE_DETAIL = "netflix.chrome.title_detail"
ANCHOR_ERROR_MODAL = "netflix.chrome.error_or_modal"
ANCHOR_HIGHLIGHTED_PROFILE = "netflix.chrome.highlighted_profile"
ANCHOR_APPLE_HOME_GRID = "apple.chrome.app_grid"

# Deliberately omit ambiguous navigation words such as Home, Shows, and Movies.
# Three distinct branded/system app labels on one frame are strong evidence for
# the tvOS app grid and keep Apple Home separate from Netflix Home.
_APPLE_HOME_APP_LABELS = {
    "app store",
    "arcade",
    "computers",
    "disney+",
    "espn",
    "f1 tv",
    "facetime",
    "fitness",
    "hulu",
    "max",
    "music",
    "netflix",
    "paramount+",
    "peacock",
    "photos",
    "plex",
    "podcasts",
    "prime video",
    "settings",
    "tubi",
    "twitch",
    "youtube",
}


@dataclass(frozen=True)
class NetflixAnchorClassification:
    state: ProviderState
    confidence: float
    anchors: list[str]
    highlighted_profile_name: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _has_phrase(tokens: list[OcrToken], phrase: str) -> bool:
    needle = _norm(phrase)
    if not needle:
        return False
    joined = " ".join(t.normalized_text for t in tokens)
    if needle in joined:
        return True
    return any(needle == t.normalized_text or needle in t.normalized_text for t in tokens)


def _top_band(tokens: list[OcrToken], *, y_max: float = 0.28) -> list[OcrToken]:
    return [t for t in tokens if t.cy <= y_max]


def _mid_band(tokens: list[OcrToken]) -> list[OcrToken]:
    return [t for t in tokens if 0.25 <= t.cy <= 0.75]


def _has_home_nav(tokens: list[OcrToken]) -> bool:
    top = _top_band(tokens)
    labels = {"home", "shows", "movies", "my netflix"}
    found = {t.normalized_text for t in top} & labels
    # Require at least 3 of the 4 top-nav labels in the top band.
    return len(found) >= 3


def _has_search_chrome(tokens: list[OcrToken]) -> bool:
    top = _top_band(tokens, y_max=0.35)
    return any(t.normalized_text == "search" for t in top) or _has_phrase(tokens, "search")


def _has_keyboard_anchors(tokens: list[OcrToken]) -> bool:
    # Letter-row / category evidence without requiring artwork.
    letters = {t.normalized_text for t in tokens if len(t.normalized_text) == 1}
    alpha = set("abcdefghijklmnopqrstuvwxyz")
    letter_hits = len(letters & alpha)
    # Vision often merges the horizontal tvOS alphabet into one imperfect but
    # mostly alphabetic token (for example ``bodefgh...xyz``).  Geometry and
    # length make that a stable keyboard anchor without depending on exact OCR.
    grouped_alpha_row = any(
        12 <= len(t.normalized_text.replace(" ", "")) <= 35
        and t.normalized_text.replace(" ", "").isalpha()
        and 0.15 <= t.cy <= 0.38
        and t.w >= 0.25
        for t in tokens
    )
    categories = {"tv shows", "movies", "my list", "documentaries", "action", "comedy"}
    cat_hits = sum(1 for t in tokens if t.normalized_text in categories)
    return (
        grouped_alpha_row
        or letter_hits >= 8
        or (letter_hits >= 4 and cat_hits >= 1)
        or cat_hits >= 2
    )


def _has_results_chrome(tokens: list[OcrToken]) -> bool:
    phrases = ("titles", "top results", "explore titles", "movies & tv", "results")
    return any(_has_phrase(tokens, p) for p in phrases)


def _apple_home_app_label_count(tokens: list[OcrToken]) -> int:
    labels = {
        token.normalized_text
        for token in tokens
        if 0.08 <= token.cy <= 0.95
        and token.normalized_text in _APPLE_HOME_APP_LABELS
    }
    return len(labels)


def _query_visible(tokens: list[OcrToken], requested_query: str | None) -> bool:
    if not requested_query:
        return False
    target = normalize_query(requested_query)
    if not target:
        return False
    for token in tokens:
        if queries_match(requested_query, token.text):
            return True
    joined = " ".join(t.text for t in tokens)
    return queries_match(requested_query, joined)


def _query_field_populated(tokens: list[OcrToken]) -> bool:
    """Detect Netflix's populated top-left query field independent of its value.

    Vision commonly folds the magnifying-glass glyph into the text token as a
    leading ``Q``.  Geometry is therefore more reliable than comparing the
    token verbatim to the requested query when deciding whether this is a
    reusable search-results screen.
    """
    excluded = {
        "home",
        "shows",
        "movies",
        "my netflix",
        "hold to dictate in english",
    }
    for token in tokens:
        norm = token.normalized_text
        if norm in excluded or len(norm) < 4:
            continue
        if token.x <= 0.16 and 0.075 <= token.cy <= 0.22 and token.w >= 0.12:
            return True
    return False


def _infer_highlighted_profile(tokens: list[OcrToken]) -> str | None:
    """Best-effort highlighted profile from mid-band names near picker chrome.

    Netflix's tvOS picker renders the focused row expanded and shows only that
    row's profile label.  Only return a name when picker chrome is already
    present and exactly one short label appears in the left-side profile lane.
    Fixture markers remain supported, but are never required live.
    """
    mid = _mid_band(tokens)
    marked: list[str] = []
    names: list[str] = []
    skip = {
        "choose a profile",
        "who's watching",
        "whos watching",
        "who’s watching",
        "add profile",
        "manage profiles",
        "profile",
        "netflix",
    }
    for token in mid:
        raw = token.text.strip()
        norm = token.normalized_text
        if not raw or norm in skip or len(norm) < 2:
            continue
        # Profile labels live beside the avatar stack in the left 40% of the
        # frame.  Ignore artwork/title text on the right side.
        if token.cx > 0.40:
            continue
        if norm.endswith("*") or "[sel]" in norm or norm.startswith(">"):
            cleaned = raw.lstrip(">").replace("[sel]", "").rstrip("*").strip()
            if cleaned:
                marked.append(cleaned)
            continue
        # Single-word / short display names only — avoid chrome sentences.
        if " " in norm and norm not in {"my netflix"} and len(norm.split()) > 3:
            continue
        names.append(raw)
    if len(marked) == 1:
        return marked[0]
    if len(marked) > 1:
        return None
    if len(names) == 1:
        return names[0]
    return None


def _looks_title_detail(tokens: list[OcrToken]) -> bool:
    detail = {"play", "resume", "episodes", "more info", "trailers & more", "my list"}
    hits = sum(1 for t in tokens if t.normalized_text in detail)
    return hits >= 2 and not _has_home_nav(tokens)


def _looks_error_or_modal(tokens: list[OcrToken]) -> bool:
    phrases = (
        "something went wrong",
        "try again",
        "unable to connect",
        "network error",
        "offline",
        "error",
    )
    return any(_has_phrase(tokens, p) for p in phrases)


def classify_netflix_anchors(
    document: OcrDocument,
    *,
    requested_query: str | None = None,
) -> NetflixAnchorClassification:
    """Map OCR chrome tokens → ProviderState. Conservative on ambiguity."""
    tokens = [t for t in document.tokens if t.normalized_text]
    if not tokens:
        return NetflixAnchorClassification(
            state=ProviderState.UNKNOWN,
            confidence=0.15,
            anchors=["netflix.ocr.empty"],
        )

    anchors: list[str] = []

    if _looks_error_or_modal(tokens):
        anchors.append(ANCHOR_ERROR_MODAL)
        return NetflixAnchorClassification(
            state=ProviderState.ERROR_OR_MODAL,
            confidence=0.8,
            anchors=anchors,
        )

    profile_chrome = False
    if _has_phrase(tokens, "choose a profile"):
        anchors.append(ANCHOR_PROFILE_CHOOSE)
        profile_chrome = True
    if _has_phrase(tokens, "who's watching") or _has_phrase(tokens, "whos watching"):
        anchors.append(ANCHOR_PROFILE_WHOS)
        profile_chrome = True
    if profile_chrome:
        highlighted = _infer_highlighted_profile(tokens)
        if highlighted:
            anchors.append(ANCHOR_HIGHLIGHTED_PROFILE)
        return NetflixAnchorClassification(
            state=ProviderState.PROFILE_PICKER,
            confidence=0.92 if highlighted else 0.88,
            anchors=anchors,
            highlighted_profile_name=highlighted,
        )

    if _looks_title_detail(tokens):
        anchors.append(ANCHOR_TITLE_DETAIL)
        return NetflixAnchorClassification(
            state=ProviderState.TITLE_DETAIL,
            confidence=0.75,
            anchors=anchors,
        )

    search_chrome = _has_search_chrome(tokens)
    keyboard = _has_keyboard_anchors(tokens)
    results = _has_results_chrome(tokens)
    query_ok = _query_visible(tokens, requested_query)
    query_populated = _query_field_populated(tokens)
    home_nav = _has_home_nav(tokens)
    apple_home_app_count = _apple_home_app_label_count(tokens)

    if search_chrome:
        anchors.append(ANCHOR_SEARCH_CHROME)
    if keyboard:
        anchors.append(ANCHOR_KEYBOARD)
    if results:
        anchors.append(ANCHOR_RESULTS)
    if query_ok:
        anchors.append(ANCHOR_QUERY_VISIBLE)
    if query_populated:
        anchors.append(ANCHOR_QUERY_POPULATED)
    if home_nav:
        anchors.append(ANCHOR_HOME_NAV)

    # The Apple app grid can include a Search app label, so search_chrome alone
    # must not suppress this rule. Netflix profile/detail/error and its strong
    # Home/keyboard shapes have already taken precedence above.
    if apple_home_app_count >= 3 and not home_nav and not keyboard:
        return NetflixAnchorClassification(
            state=ProviderState.APPLE_HOME,
            confidence=0.91,
            anchors=[ANCHOR_APPLE_HOME_GRID, "apple.chrome.app_labels_3plus"],
        )

    # Netflix replaces the literal "Search" heading with the query after text
    # entry.  The live results screen therefore often has keyboard chrome plus
    # an exact visible query, but no OCR token containing "Search" or
    # "Results".  Treat that combination as the populated results/search
    # screen; requiring the vanished heading would turn a successfully typed
    # query into UNKNOWN and abort the verified workflow.
    if query_ok and keyboard:
        return NetflixAnchorClassification(
            state=ProviderState.SEARCH_RESULTS,
            confidence=0.9,
            anchors=anchors,
        )

    # A warm launch can restore results for a different prior query.  It is
    # safe to classify this as SEARCH_RESULTS so the semantic action can
    # replace the text; exact-query verification remains false until keyboard
    # readback matches the new request.
    if query_populated and keyboard:
        return NetflixAnchorClassification(
            state=ProviderState.SEARCH_RESULTS,
            confidence=0.82,
            anchors=anchors,
        )

    # Exclude keyboard/search screens from Home even if some nav labels appear.
    if home_nav and not search_chrome and not keyboard:
        return NetflixAnchorClassification(
            state=ProviderState.HOME,
            confidence=0.9,
            anchors=anchors,
        )

    if search_chrome and (keyboard or results or query_ok):
        empty_query = not requested_query or not query_ok
        if query_ok and (results or not keyboard):
            return NetflixAnchorClassification(
                state=ProviderState.SEARCH_RESULTS,
                confidence=0.9 if results else 0.82,
                anchors=anchors,
            )
        if keyboard and empty_query:
            return NetflixAnchorClassification(
                state=ProviderState.SEARCH_KEYBOARD,
                confidence=0.88,
                anchors=anchors,
            )
        if keyboard:
            return NetflixAnchorClassification(
                state=ProviderState.SEARCH_KEYBOARD,
                confidence=0.8,
                anchors=anchors,
            )
        if results:
            return NetflixAnchorClassification(
                state=ProviderState.SEARCH_RESULTS,
                confidence=0.78,
                anchors=anchors,
            )

    if search_chrome and not keyboard:
        return NetflixAnchorClassification(
            state=ProviderState.SEARCH_NAV,
            confidence=0.7,
            anchors=anchors,
        )

    return NetflixAnchorClassification(
        state=ProviderState.UNKNOWN,
        confidence=0.25,
        anchors=anchors or ["netflix.ocr.unclassified"],
    )
