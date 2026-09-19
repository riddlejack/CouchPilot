---
name: apple-tv-agent
description: Control and inspect configured Apple TVs through the local Apple TV Agent bridge. Use for app, profile, content, search, playback, audio, and settings goals that require current device state and verified outcomes.
---

# Apple TV Agent

Use the six MCP tools exposed by the local bridge. The tools return device state directly to the current agent; they do not call another model.

## Work from state

1. At the start of every user task, call `devices` to select the device and inspect current screen-access health. If the requested task needs visual control and access is expired or near expiration, follow [free signing maintenance](references/free-signing.md) before continuing. Healthy access needs no login or renewal.
2. Use `status` and `apps` before taking a direct action. Use `observe` when the visual route is configured and the screen matters.
3. Prefer the text observation. Set `image=true` only when labels are incomplete, the interface is unfamiliar, or visual layout resolves ambiguity.
4. For `press`, `select`, or `type`, pass the fresh `observation_id` to `act`. The server consumes that ID before dispatch. `select` activates only an exact, unique label that is already focused and sends one Select input. If the target is unfocused, move one direction with `press`, observe again, and repeat only as needed.
5. Verify the user's goal from a fresh `status` or `observe` result. A successful command response proves dispatch, not the requested outcome.

Use `control` for paired operations that do not need screen semantics: wake, sleep, transport,
open a URL, launch an app, type text, or send a remote button the user explicitly requested.
When the agent chooses navigation or Select on the user's behalf, observe first and use `act`
with its fresh receipt. Verify any content, episode, profile, or playback claim separately.

Observations are sequential samples. An image can precede its semantic tree, and transitions can briefly return an empty or previous tree. When sources conflict, inspect timestamps, preserve explicit positive focus evidence, and reobserve before taking a consequential action. Protected video pixels may be black even while metadata or playback controls remain readable.

## Resolve common goals

- **App or profile:** confirm the device, list installed apps, launch by exact bundle ID when available, then observe the expected app or visible profile. Installed apps do not prove subscriptions. Navigate one bounded direction at a time until the intended profile is confirmed focused, then select that exact label once. Never use `select` itself to search for or move focus to a profile.
- **Content:** preserve title, season, episode, region, and subscription constraints. Prefer a confirmed provider deep link when one exists. Otherwise search in an available app, select only a unique match, and verify the resulting title or episode.
- **Search:** focus the app's actual keyboard or text field before typing, then verify the field value itself. In Netflix, move focus from the Search navigation item to `Keyboard` before typing. A command acknowledgement does not prove that text was entered.
- **Playback or audio:** distinguish command acceptance from active playback and from the selected audio output. Check now-playing state and any visible route indicator that matters to the request.
- **Settings:** read the current value, change one reversible setting, and read it again. Keep enough context to restore the previous value if requested.

If semantics are empty or clearly incomplete, request an image and use only bounded directional input to recover a readable state. Reobserve after each recovery step. For an App Store goal, use a confirmed official link, then separately verify the product page, download completion, and launched app; one existing-account redownload does not establish first-purchase or password-prompt behavior.

## Maintain free screen access

If screen access fails because the signed helper expired, or the owner asks for
renewal, run `apple-tv-agent doctor` on the local Mac and follow
[free signing maintenance](references/free-signing.md). Renew the helper profile
and reinstall; merely signing into Apple again is not a repair. This is an on-demand repair: do not create a scheduler or background polling. Use the
saved Xcode account session; surface required sign-in or 2FA instead of claiming
that it can always be completed unattended.

## Boundaries

Direct control needs a stable device ID and Companion pairing. It cannot read the screen. Visual observation and UI actions require a reachable, correctly signed WDA helper whose identity is pinned by local configuration. Free Personal Team provisioning expires after seven days.

Keep WDA, RemoteXPC, pairing credentials, and raw LAN discovery on the local bridge. A remote or cloud agent should reach the bridge through an authenticated private connection; never expose WDA ports to the public internet.
