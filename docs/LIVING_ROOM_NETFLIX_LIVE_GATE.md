# Living Room Netflix live gates

Updated: 2026-07-21 (America/Chicago)

## Verdict

**LIVE VERIFIED** for bounded `search_ready`, exact-title selection, and
resume-then-pause outcomes on Living Room.

One semantic command can start from the Apple TV Home screen, launch Netflix,
classify the restored Netflix screen from pixels, replace a prior query, verify
the requested title through the real Apple TV keyboard channel, and stop at
results without selecting a title or claiming playback.

The original proof below covers search-ready. A later broker acceptance run also
proved exact-title selection and resume-then-pause.

### Resume-then-pause proof (2026-07-21)

- Request: resume `Avatar: The Last Airbender` on Netflix in Living Room.
- Terminal result: `playback_paused_verified`.
- Broker latency: 35.3 seconds.
- The controller selected the exact visible title and playback CTA, observed the
  causal playback transition, sent explicit idempotent **Pause**, and verified
  fresh stopped-state receipts.
- An independent status read five seconds after the broker response still
  reported `idle`, confirming that playback had not resumed.

This proves the Mac mini broker/controller path. It does **not** prove Shortcut
import, token configuration, iCloud synchronization, or Siri on the iPhone.

## Original search-ready proof (2026-07-20)

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

## Original search-ready automated evidence (2026-07-20)

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
- cold Netflix profile-picker classification and configured `primary` profile
  selection;
- stop before result selection for `search_ready`;
- exact visible result selection and title detail;
- resume playback followed by explicit Pause and independent stopped-state
  verification.

Not yet live verified: other rooms, physical TVs, or Siri as a client.
