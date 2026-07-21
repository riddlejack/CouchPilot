# Provider-aware Apple TV automation plan

Date: 2026-07-20

> **Implemented milestone:** Living Room Netflix `search_ready` is live verified.
> The current command uses exact-room DVT screenshots, local Vision OCR, typed
> state anchors, real keyboard focus/readback, and stops before result Select.
> See `LIVING_ROOM_NETFLIX_LIVE_GATE.md`.

> **Future client:** `docs/SIRI_INTEGRATION_PLAN.md` defines the Shortcut and
> native iOS 27 App Intents layers. This controller must keep semantic actions
> independent of CLI/MCP so Siri can call the same core later.

## Decision

Keep the 15 foundational repairs in `REMEDIATION_PLAN.md`, but do not implement
Apple system Search as the only content route. It cannot search the user's full
Netflix catalog and will never be universal because participation is controlled
by each streaming provider.

The replacement architecture is a route ladder:

```text
plain-language content intent
          |
          v
provider/content resolver
          |
          +-- 1. verified direct deep link
          +-- 2. Apple system Search / TV integration
          +-- 3. provider-specific search state machine
          +-- 4. screenshot-guided recovery or selection
          +-- 5. one bounded human handoff
```

This is not a pixel-perfect clone of Netflix. The useful “digital twin” is a
small, versioned state graph for each provider with observable preconditions and
postconditions. Routine actions run as deterministic recipes; an LLM or vision
model is used to classify an exceptional screen, not to invent every arrow in
real time.

## Current evidence that changes the prior plan

- Apple Search covers participating providers only. Current Apple documentation
  lists Netflix Originals rather than the complete Netflix catalog.
- `pyatv`'s current app documentation explicitly lists
  `https://www.netflix.com/title/80234304` as a known working tvOS deep link.
- The local project already maps Avatar to
  `https://www.netflix.com/title/70142405`, but the one live attempt encountered
  the profile picker and a later replay did not open the title.
- The current adapter accepts only `http` and `https`, so it has not tested the
  long-standing Netflix tvOS custom-scheme form
  `nflx://www.netflix.com/title/<id>`.
- Netflix officially does not support AirPlay from iPhone or iPad, so “browse on
  the phone and AirPlay it” is not a viable Netflix architecture.
- The user has now observed a candidate cold-launch Netflix path: select the
  currently highlighted profile, then Up once, Left once, then enter the query.
  Treat that as a hypothesis to calibrate, not a universal invariant.
- `crlian/mcp-pyatv` now demonstrates DVT screenshots, segmented navigation
  recipes, state checks, and confidence decay. Its April 2026 status reports
  screenshots at roughly five seconds and a broken tvOS 26 accessibility tree.
  Its screenshot implementation currently picks `devices[0]` and ignores its
  `device` argument, which is unsafe in this five-Apple-TV home. It is an MIT
  design/code reference, not a drop-in dependency.

Current references:

- pyatv app/deep-link API: <https://pyatv.dev/development/apps/>
- pyatv keyboard API: <https://pyatv.dev/development/keyboard/>
- Netflix casting limitations: <https://help.netflix.com/en/node/49>
- Apple Search coverage: <https://support.apple.com/en-mide/106342>
- Existing screenshot/recipe MCP status:
  <https://github.com/crlian/mcp-pyatv/blob/main/STATUS.md>
- Existing screenshot implementation:
  <https://github.com/crlian/mcp-pyatv/blob/main/src/mcp_pyatv/tools/developer.py>
- pymobiledevice3: <https://github.com/doronz88/pymobiledevice3>

## Product intents must remain distinct

Do not collapse all content requests into a misleading `resume` command.

### `prepare_search`

Get the user to a provider or Apple search-results screen with the exact query
entered. Do not select a title. This is the safe baseline and the user has said
one final click is acceptable.

### `open_title`

Open the exact title's detail page. This requires a verified provider content
ID/deep link or a visually confirmed exact result. It must not start playback.

### `resume_title`

Start the exact requested title at the provider-managed resume point. Success
requires post-playback evidence matching the requested title and, when exposed,
season/episode. Opening an app, showing a title page, or seeing unrelated
now-playing metadata is not resume success.

Suggested public operation:

```text
prepare_content(
    room="living_room",
    title="Avatar: The Last Airbender",
    provider="netflix",       # optional when resolvable
    goal="search_ready",      # search_ready | title_open | resume
    wake=True,
)
```

The result should report the chosen route, provider, resolved content ID/URL
source, confidence, each observed state, actions performed, selected/playing
flags, verification evidence, latency, and the precise handoff needed.

## Route 1: direct provider deep links

This is the fastest and most likely path to genuinely hands-off behavior. A
provider's own profile and playback state can preserve the resume position once
the correct title or episode is opened.

### Resolver model

Add a local content-target cache with at least:

```text
normalized title
provider
provider content ID
canonical title URL
optional episode URL/ID
resolution source
last verified time
successful URL form
confidence
```

Resolution order:

1. exact local verified cache/alias;
2. a direct share URL supplied by the user or agent;
3. a supported metadata/deep-link provider after independent API review;
4. provider in-app search fallback.

Do not scrape authenticated provider sessions or depend on undocumented private
account APIs. If an LLM resolves a URL through web search, mark that source and
verify it on the device before promoting it to the local cache.

### Netflix experiment matrix

Before declaring Netflix deep links broken, test a bounded matrix using one
user-approved title URL and no automatic playback:

- `https://www.netflix.com/title/<series_id>`;
- allowlisted `nflx://www.netflix.com/title/<series_id>`;
- cold app vs already-running app;
- correct profile already active vs profile picker shown;
- initial link, profile selection, then replay of the same link;
- replay after temporarily returning to tvOS Home or launching a neutral app,
  if that is necessary for tvOS to deliver the URL again.

Each cell must record current app, whether a profile picker appeared, whether a
title detail page appeared, latency, and user/screenshot confirmation. Never
generalize from one failed cell.

Custom schemes must be allowlisted per provider; do not weaken URL validation
to accept arbitrary schemes. A `watch/<episode_id>` or custom-scheme playback
URL is a later, explicit playback test. Do not substitute a series ID where an
episode/video ID is required, and do not test it without a separate same-turn
approval because it can start playback.

## Route 2: Apple Search and TV integration

Keep `prepare_title_search` for providers/titles Apple actually indexes. It is
still the most stable route when applicable and is valuable for Hulu/Disney/Max
and Apple TV “Up Next” behavior.

The route planner must know that provider coverage is partial. It may select
Apple Search when:

- the provider is listed as participating for the region;
- a prior exact title/provider result was verified; or
- the user did not constrain the provider and accepts a cross-provider result.

It must not route an ordinary non-original Netflix title to Apple Search and
pretend the absence is a query failure.

## Route 3: provider-specific state machines

### State model

Start with Netflix only. Define a generic provider interface, but do not build
empty adapters for every streaming service.

```text
UNKNOWN
PROFILE_PICKER
HOME
SEARCH_NAV
SEARCH_KEYBOARD
SEARCH_RESULTS
TITLE_DETAIL
PLAYING
ERROR_OR_MODAL
```

An observation may combine:

- current app bundle ID;
- Apple TV attention/power state;
- keyboard focus state;
- keyboard read-back text;
- now-playing title/episode/device state;
- an optional screenshot classification with detected text and confidence;
- a one-time user confirmation during calibration.

A transition must declare its allowed starting states, action batch, expected
ending state, timeout, verification rule, recovery rule, and whether it may
start playback. If the starting state is unknown, do not run a Select action.

### Netflix search-ready recipe

Candidate happy path, subject to live calibration:

1. Resolve room and connect once.
2. Wake and launch `com.netflix.Netflix`.
3. Observe the initial state.
4. If and only if `PROFILE_PICKER` is observed, choose the locally configured
   profile (preferred name or one-based index 2) and verify `HOME`.
5. If `HOME` is observed, batch Up, Left, then Down using the live-calibrated
   Netflix layout.
6. Wait for `atv.keyboard.text_focus_state == KeyboardFocusState.Focused`.
7. Replace and read back the full title through the keyboard channel. A restored
   different query is reusable search state, not a failure.
8. Stop at `SEARCH_RESULTS`; do not press Select for the v0 baseline.

Blind Select is not accepted. If Netflix resumes directly to Home or Search, a
Select intended for the profile picker could open or play highlighted content.
Profile selection therefore requires a real screenshot classification, strong
picker chrome, and exact highlighted-profile name equality.

### Known-state reset

Investigate whether tvOS exposes a safe, verifiable way to cold-launch or
terminate only the current Netflix app. A possible app-switcher sequence is not
accepted merely because it works once; it must verify that Netflix is the
focused/current app before closing anything and prove it cannot terminate an
unrelated app. There is no public `pyatv` force-quit API, so fail closed if a
safe reset cannot be established.

## Route 4: screenshot-guided state and recovery

Full hands-off provider navigation requires an observer because third-party app
layouts are dynamic and tvOS does not expose their general accessibility tree
through `pyatv`.

The practical observer is an on-demand DVT screenshot using developer pairing
and `pymobiledevice3`. Use it at state boundaries, not after every arrow:

```text
screenshot/classify entry state
        -> run a short verified recipe
        -> keyboard/current-app checkpoint
        -> screenshot only if needed for result/detail selection
```

Living Room now has the authorized `pymobiledevice3` developer pairing and exact
observer binding. Capture runs through a separate Python 3.14 subprocess
boundary; core `home-media` does not import the GPL package. Other rooms still
require their own explicit developer/Companion pairing and binding proof.

### Multi-device safety requirement

Every captured image must be cryptographically or structurally bound to the
requested room's stable Apple TV identity and the exact tunnel service. Fail on
zero matches, multiple matches, or identifier disagreement. Never use
`get_tunneld_devices()[0]`, never ignore a `device` argument, and never ask the
LLM to guess which room an image came from.

Protected video may be blacked out by DRM; menu chrome and pre-playback screens
are the target. A screenshot provider must label protected/blank captures as
unobservable rather than letting vision hallucinate.

### Screen classifier

For routine operation, prefer a small local classifier/OCR/template layer that
returns structured state and confidence. An LLM vision call is appropriate for
calibration, unfamiliar screens, and recovery. It should return a proposed
state and visible anchors, not raw button commands.

Do not select a title result unless the requested title is visibly matched at a
high confidence threshold. Do not click Resume/Play unless the title detail
state and action label are both observed. After playback starts, require
`now_playing` evidence matching the title; use the screen only as secondary
evidence.

## Calibration: how the app “learns” Netflix

Provide an explicit, user-present calibration mode rather than letting an LLM
explore unpredictably during normal use.

1. Capture a screenshot and machine observations.
2. Classify/label the current state.
3. Execute one bounded action or short batch.
4. Capture the postcondition.
5. Save the transition as a private local fixture/recipe.
6. Replay it against recorded fixtures before trying it live again.

Each recipe stores provider, tvOS version, optional provider app version,
starting/ending state, steps, evidence anchors, success/failure counts,
last-verified time, and confidence. Confidence drops after a failed transition,
an app/tvOS upgrade, or time without verification. Low-confidence recipes stop
for observation instead of guessing.

Screenshots may contain profile names and viewing information. Keep them in an
ignored private directory, redact logs, and do not send them to a remote vision
service without explicit user consent.

## Fully hands-off resume

Build this only after search-ready is reliable.

Preferred order:

1. verified direct episode/watch deep link;
2. verified direct title link followed by an observed Resume/Play action;
3. exact search result visually matched, title detail observed, Resume selected;
4. stop for a user click.

For Apple-integrated providers such as Hulu, test whether a canonical share URL
or Apple TV/Up Next deep link lands on the provider-managed resume state before
building a UI recipe. Provider-managed playback progress is more reliable than
our code trying to reconstruct an episode number.

No route may report `resumed` unless:

- the correct provider/current app is observed;
- now-playing title matches the normalized request;
- playback state is playing; and
- season/episode either matches when available or is explicitly reported as
  unverified.

## MCP/API surface

Default agent tools should be semantic:

- `prepare_content`
- `prepare_provider_search`
- `open_verified_title`
- `resume_verified_title`
- `get_content_action_status`

The route planner and provider state machine own primitive actions. Raw remote,
arbitrary URL, recipe editing, screenshots, and calibration are debug/admin
surfaces gated separately. An LLM should request an outcome, not orchestrate
Up/Left/Select itself.

## Implementation and proof order

### Phase A: foundation, code only

- Fix all 15 findings from `REMEDIATION_PLAN.md`.
- Add persistent MCP/service/connection lifecycle.
- Add typed content intents, route planner, provider adapter contract, local
  verified-target cache, and fake observations.
- Keep existing direct URL and Apple Search as explicit routes.
- Add a Netflix adapter with deep-link and search-ready state-machine contracts;
  do not claim live correctness yet.

### Phase B: automated provider proof

- Exhaustive transition tests for every allowed/forbidden start state.
- Assert Select never executes from `UNKNOWN`.
- Assert profile index 2 is used only after `PROFILE_PICKER` observation.
- Assert wrong keyboard text, wrong app, or wrong title cannot verify success.
- Assert direct-link/custom schemes are provider-allowlisted.
- Assert route fallback is bounded and cannot oscillate.
- Execute composite tools through the real MCP lifecycle in fakes.
- Run Ruff, mypy, pytest, and an adversarial review.

### Gate 1: approved Netflix deep-link matrix

With the user in front of Living Room, test one title-detail URL matrix. No playback
URL and no blind Select. The user should answer only whether the correct title
detail page, profile picker, Netflix Home, or other screen appeared.

### Gate 2: approved Netflix search-ready calibration

Test the candidate profile -> Home -> Up -> Left -> keyboard path. Record exact
state transitions. Stop with the query entered and let the user click the
result. Do not improvise additional arrows on failure.

### Gate 3: separately approved screenshot setup

Only if Gates 1/2 leave an observation gap, ask to install/configure the minimum
developer screenshot stack for one Apple TV. Prove exact room/tunnel binding and
one menu screenshot before building vision logic.

### Gate 4: hands-off title/resume

After screenshots/state classification and search-ready are reliable, test one
exact title selection, then one explicit resume action. Require matching
now-playing evidence and zero wrong-title starts.

## Success targets

- Search-ready: at least 19/20 successful trials per calibrated provider/state
  set, with zero wrong selections or playback starts.
- Direct-title route: report its observed success rate separately by warm/cold
  and profile state; do not combine failures into a vague “mostly works.”
- Happy-path search-ready latency: target <=5 seconds without a screenshot.
- Screenshot-recovery latency: target <=15 seconds with no more than two
  screenshots.
- Full resume: zero false-positive success reports and zero wrong-title starts.
- UI changes: one failed transition lowers confidence and stops; it does not
  trigger an LLM button-press spiral.

## Honest ceiling

High confidence: reliable Netflix search-ready automation is achievable with a
provider state machine, keyboard read-back, and one calibrated entry-state
observer.

Medium confidence: Netflix title-detail and resume can be hands-off for verified
deep links and calibrated screens. It will need live testing and maintenance
when Netflix changes its tvOS UI.

Low confidence: a maintenance-free universal controller that fully navigates
every third-party app. tvOS exposes control primitives but not a supported
cross-app semantic UI/accessibility API. The sustainable product is a routing
engine plus a few high-value provider adapters, not a universal blind agent.
