"""Content route planner: deep link vs Apple search vs provider state machine.

Apple system search participates for some providers, but for Netflix it lists
**Netflix Originals only**. Non-original / ``provider=netflix`` titles must not
use ``apple_system_search`` as the primary route for ``search_ready``; it may
appear only as a last-resort fallback with an explicit warning.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ContentGoal(StrEnum):
    SEARCH_READY = "search_ready"
    TITLE_OPEN = "title_open"
    RESUME = "resume"


class ContentRoute(StrEnum):
    DIRECT_DEEP_LINK = "direct_deep_link"
    APPLE_SYSTEM_SEARCH = "apple_system_search"
    PROVIDER_STATE_MACHINE = "provider_state_machine"
    SCREENSHOT_RECOVERY = "screenshot_recovery"
    HUMAN_HANDOFF = "human_handoff"


class RoutePlan(BaseModel):
    routes: list[ContentRoute] = Field(default_factory=list)
    reason: str
    provider: str | None = None
    goal: ContentGoal
    warnings: list[str] = Field(default_factory=list)


_MAX_ROUTES = 3

_APPLE_SEARCH_NETFLIX_ORIGINALS_ONLY = (
    "Apple system search lists Netflix Originals only; "
    "non-original Netflix titles must not rely on it as primary"
)


def _dedupe_bound(
    routes: list[ContentRoute],
    *,
    max_routes: int = _MAX_ROUTES,
) -> list[ContentRoute]:
    seen: set[ContentRoute] = set()
    out: list[ContentRoute] = []
    for route in routes:
        if route in seen:
            continue
        seen.add(route)
        out.append(route)
        if len(out) >= max_routes:
            break
    return out


def plan_content_routes(
    title: str,
    provider: str | None,
    goal: ContentGoal,
    *,
    apple_search_participates: bool,
    has_verified_deep_link: bool,
) -> RoutePlan:
    """Build an ordered, cycle-free route plan (max 3 routes).

    Rules:
    - Netflix (``provider=netflix``) must not use ``apple_system_search`` as
      primary for ``search_ready``; last-resort only, with warning.
    - If ``has_verified_deep_link`` and goal is ``title_open``/``resume``,
      prefer ``direct_deep_link`` first.
    - Apple search only when ``apple_search_participates`` or ``provider`` is None
      (except Netflix last-resort case above).
    """
    _ = title  # reserved for future title-class heuristics (originals vs catalog)
    warnings: list[str] = []
    provider_norm = provider.lower() if provider else None
    candidates: list[ContentRoute] = []
    reason_parts: list[str] = []

    apple_allowed = apple_search_participates or provider_norm is None
    is_netflix = provider_norm == "netflix"

    if goal in {ContentGoal.TITLE_OPEN, ContentGoal.RESUME} and has_verified_deep_link:
        candidates.append(ContentRoute.DIRECT_DEEP_LINK)
        reason_parts.append("verified deep link preferred for title_open/resume")

    if goal == ContentGoal.SEARCH_READY:
        if is_netflix:
            candidates.extend(
                [
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.SCREENSHOT_RECOVERY,
                    ContentRoute.APPLE_SYSTEM_SEARCH,
                ]
            )
            warnings.append(_APPLE_SEARCH_NETFLIX_ORIGINALS_ONLY)
            warnings.append(
                "apple_system_search is last resort only for Netflix search_ready"
            )
            reason_parts.append(
                "Netflix search_ready uses provider state machine; Apple search last resort"
            )
        elif apple_allowed:
            candidates.extend(
                [
                    ContentRoute.APPLE_SYSTEM_SEARCH,
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.HUMAN_HANDOFF,
                ]
            )
            reason_parts.append("Apple search participates (or provider unset)")
        else:
            candidates.extend(
                [
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.SCREENSHOT_RECOVERY,
                    ContentRoute.HUMAN_HANDOFF,
                ]
            )
            reason_parts.append("provider does not participate in Apple system search")
    else:
        # title_open / resume fallbacks after optional direct_deep_link
        if is_netflix:
            candidates.extend(
                [
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.SCREENSHOT_RECOVERY,
                    ContentRoute.HUMAN_HANDOFF,
                ]
            )
            warnings.append(_APPLE_SEARCH_NETFLIX_ORIGINALS_ONLY)
            reason_parts.append("Netflix title_open/resume avoids Apple search primary")
        elif apple_allowed:
            candidates.extend(
                [
                    ContentRoute.APPLE_SYSTEM_SEARCH,
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.HUMAN_HANDOFF,
                ]
            )
            reason_parts.append("Apple search available for title_open/resume fallbacks")
        else:
            candidates.extend(
                [
                    ContentRoute.PROVIDER_STATE_MACHINE,
                    ContentRoute.SCREENSHOT_RECOVERY,
                    ContentRoute.HUMAN_HANDOFF,
                ]
            )
            reason_parts.append("provider state machine for title_open/resume")

    routes = _dedupe_bound(candidates)
    if not routes:
        routes = [ContentRoute.HUMAN_HANDOFF]
        reason_parts.append("no eligible routes; human handoff")

    return RoutePlan(
        routes=routes,
        reason="; ".join(reason_parts) if reason_parts else "default route plan",
        provider=provider_norm,
        goal=goal,
        warnings=warnings,
    )
