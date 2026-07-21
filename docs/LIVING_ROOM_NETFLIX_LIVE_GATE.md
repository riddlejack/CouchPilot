# Living Room Netflix search-ready live gate

Date: 2026-07-20 (America/Chicago)

## Verdict

**LIVE VERIFIED** for the bounded `search_ready` outcome on Living Room.

One semantic command can start from the Apple TV Home screen, launch Netflix,
classify the restored Netflix screen from pixels, replace a prior query, verify
the requested title through the real Apple TV keyboard channel, and stop at
results without selecting a title or claiming playback.

This is not yet title selection or resume.

## Proof

Request:

```bash
uv run home-media --json content prepare \
  --room living_room \
  --title "Avatar: The Last Airbender" \
  --provider netflix \
  --goal search_ready
```

Precondition evidence:

- exact room binding fingerprint: `REDACTED_PRIVATE_BINDING_FINGERPRINT`;
- Apple TV Home screenshot SHA-256:
  `REDACTED_PRIVATE_SCREENSHOT_SHA256`;
- screenshot was 3840x2160, nonblank, and captured from the bound room.

Observed semantic stages:

1. `launch_app`: succeeded in 13,554 ms; post-state `search_results`.
2. `select_profile_2`: skipped because no profile picker was visible.
3. `home_to_search_keyboard`: skipped because Netflix restored a search screen.
4. `keyboard_set_query`: succeeded in 3,518 ms; post-state `search_results`.
5. `existing_query_verified`: real keyboard focus plus exact readback.

Terminal result:

```text
terminal_status: query_verified
verification_status: verified
selected_result: false
playback_started: false
```

Independent final screenshot SHA-256:
`REDACTED_PRIVATE_SCREENSHOT_SHA256`.
It visibly showed the populated Avatar query and Netflix results.

## What the failed attempts taught us

- A 15-second launch deadline incorrectly included a roughly 9-10 second DVT
  screenshot. The launch had succeeded, but verification exhausted the budget.
- Netflix removes the literal `Search` heading after text entry.
- A restored query can be a valid search screen even when it differs from the
  requested title.
- macOS Vision can fold the search icon into the query as a leading `Q` and can
  merge most keyboard letters into one token.
- All failures stopped without a result Select. Each live mismatch became a
  deterministic classifier/test case before retrying.

## Automated evidence

- `108 passed`
- mypy: clean
- Ruff: clean
- source distribution and wheel: build successfully
- packaged wheel contains the Swift Vision OCR helper
- adversarial Grok 4.5 follow-up verdict: GO for the bounded live proof

## Current boundary

Live verified:

- exact-room capture and classification;
- Apple Home to Netflix launch;
- warm restored-search recognition;
- arbitrary query replacement;
- exact keyboard readback;
- stop before result selection.

Not yet live verified:

- a cold picker occurring during the integrated command (the recorded picker
  frame does classify `primary` with strong confidence and exact highlight);
- visually selecting an exact search result;
- opening title detail;
- Resume/Play and now-playing title/episode verification;
- other rooms, physical TVs, or Siri as a client.
