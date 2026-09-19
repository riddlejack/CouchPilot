# Apple TV Computer Use

The control surface is an observe → act → observe loop for focus-based television
interfaces. It is intentionally provider-neutral: the same controller can operate
Netflix, Hulu, Apple TV settings, or an unfamiliar modal without teaching the core a
new coordinate system.

## State contract

`ApplicationService.observe_apple_tv(room)` returns one exact-room generation:

- the PNG frame for a multimodal MCP response;
- a monotonically increasing sequence number;
- frame hash, dimensions, blank/DRM status, and capture timing;
- provider state, confidence, stable visual anchors, and indexed OCR/AX-like elements;
- current app, power, keyboard focus/readback, now-playing title, and playback state;
- the recent bounded action history.

PNG bytes are deliberately excluded from `model_dump()` and normal JSON logs. An MCP
facade should return them as an image content block alongside `agent_metadata()`.

Every mutation includes the sequence it was planned against. The controller rejects a
stale generation instead of sending input to a screen that may have changed.

## Two execution speeds

### One-action loop

`ApplicationService.act_apple_tv(room, action)` dispatches one typed action and returns
the resulting frame. This is the primitive for model-driven exploration:

```text
observe generation 41
press Left against generation 41
observe generation 42
```

Supported primitives are wake, launch app, directional/select keys, text replacement,
and transport. A deterministic Select needs an explicit recognized source state and
confidence threshold. On an unfamiliar screen, an image-capable agent can instead name
the visible target it inspected on that exact generation; Select is still rejected when
there is no frame, the frame is blank/DRM-protected, or its generation is stale.
Provider workflows impose stronger title/profile gates.

### Verified macro

A macro is one short known state-to-state transition, not a blind end-to-end script.
It checks a fresh pre-frame, sends at most four actions, captures one post-frame, and
stops with `vision_required` if any condition drifts.

The first Netflix skills are:

| Skill | Verified precondition | Burst | Verified postcondition |
| --- | --- | --- | --- |
| `select_highlighted_profile` | Profile picker; configured name is visibly highlighted | Select | Netflix Home/Search |
| `home_to_search` | Netflix Home with top navigation | Up, Left, Down | Search keyboard visible |
| `set_query` | Search keyboard plus real tvOS keyboard focus | Replace text | Focus retained and exact readback |

This removes the screenshot/model latency between the three directional actions while
remaining self-healing. If Netflix changes its navigation, only the burst is sent; the
unexpected post-frame is returned to model vision and no later Select or text action is
attempted.

## Cold Netflix path

The application service now runs the following bounded workflow:

```text
Apple Home / unknown
  → wake + launch Netflix → observe
  → if profile picker: verified profile skill → observe
  → if Netflix Home: verified Home-to-Search skill → observe
  → verified text replacement/readback → query_verified
```

`apple_home` is distinct from Netflix `home`; provider state alone is never allowed to
turn an Apple app grid into Netflix navigation. Blank/DRM frames, errors, unexpected
screens, low-confidence profile focus, and query mismatches stop with the latest pixels
ready for model vision.

## Runtime requirement

The controller, pyatv connection manager, and DVT screenshot worker only meet the fast
path when owned by a long-lived process. A command that constructs and closes an
`ApplicationService` for every Siri request throws the warmed tunnel away.

Production Siri/MCP traffic should therefore target the resident Home Media hub. The
hub should prewarm each paired room with a read-only screenshot at startup and keep the
observer healthy. A one-shot CLI remains useful for setup and diagnosis, not the
15-second voice path.

The production visual source is the retained AVFoundation/CoreMediaIO service in
`native/AirPlayFrameDaemon`. It publishes fresh exact-room frames into a private
atomic store; `HOME_MEDIA_AIRPLAY_CAPTURE_CONFIG` and
`HOME_MEDIA_FRAME_STREAM_DIR` enable it. The existing persistent DVT worker remains
the exact-UDID fallback when the stream has no fresh frame. See
`docs/AIRPLAY_FRAME_DAEMON.md` for the one-time macOS capture/AirPlay gate and
LaunchAgent install.

## Latency model

For a warm Netflix Home request, the deterministic path needs three frames:

1. recognize Home;
2. verify the Up/Left/Down burst reached Search;
3. verify exact keyboard readback.

The same flow previously risked a full observation between every arrow. Cold launch
adds one frame for launch and, when shown, one for profile selection. The model is only
invoked when a recognized fast path cannot prove its postcondition.

## Test boundary

`tests/unit/test_computer_use.py` covers pixel exclusion, generation staleness,
one-action verification, Select guards, bounded Home-to-Search capture count, macro
drift fallback, named-profile gating, exact text readback, and the complete fake Apple
Home → Netflix Search path. These tests never connect to or mutate a physical TV.

Live acceptance remains separate: verify the warm worker, Living Room frame identity,
Apple Home classification, profile focus, exact query readback, and measured wall time
on the Mac mini before calling the 15-second target achieved.
