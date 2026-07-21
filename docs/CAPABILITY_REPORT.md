# Capability report (household)

Evidence labels: automated | live_verified | degraded | untested | unsupported

Generated during implementation on 2026-07-20. Update after each live gate.

## Living Room (current live room)

| Capability | Target | Status | Notes |
|---|---|---|---|
| Companion pairing | Apple TV | live_verified | Stored outside repo |
| Exact room screenshot | Apple TV | live_verified | Bound fingerprint `REDACTED_PRIVATE_BINDING_FINGERPRINT`; no `devices[0]` fallback |
| Netflix picker/Home/Search classification | Apple TV | live_verified | Vision OCR against recorded live frames; deterministic anchors |
| Netflix `search_ready` | Apple TV | **live_verified** | Apple Home → launch → replace prior query → exact readback; no result Select |
| Apps open / transport | Apple TV | live_verified / partially untested | Netflix launch proven; broader transport matrix pending |
| Result selection / title detail | Netflix | untested | Intentionally forbidden in current semantic goal |
| Exact resume progress | Netflix | unsupported as a verified universal path | Next provider milestone requires title + playback evidence |
| Sonos volume | Sonos | live_verified | Existing room binding reports volume R/W |
| Physical TV state | — | not configured | Apple TV wake does not prove display power |

Live proof: `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`.

## Theater

| Capability | Target | Status | Notes |
|---|---|---|---|
| Discover | ATV / Sony / Sonos | live_verified | Three distinct stable IDs in room registry |
| Pair Companion | Apple TV | untested | Human Gate A |
| Status / now playing | Apple TV | untested | Needs pairing |
| Power Apple TV | Apple TV | untested | Needs approval |
| Physical TV power state | Sony | untested | Direct androidtvremote2 path ready |
| Apps list/open | Apple TV | untested | |
| Deep link open | Apple TV | automated (fakes) / untested live | https + allowlisted `nflx://` title links; Gate 1 matrix pending |
| prepare_content search_ready | Netflix | automated / untested in Theater | Living Room is the verified reference room |
| Exact resume progress | Provider | unsupported as universal API | Honest outcome levels implemented |
| Raw AirPlay `play_url` | Apple TV | unsupported | tvOS 26 regression (#2821) |
| Exact volume | Sonos Beam | live_verified read / untested set | Read level=73; set awaits Human Gate C |
| TV input | Sony | degraded/untested | Key-based best effort |
| App install | — | unsupported | |

## Other rooms

| Room | Apple TV discover | Sonos | Physical TV | Control |
|---|---|---|---|---|
| Family Room | live_verified | configured | unknown brand | unpaired / untested |
| Bedroom | live_verified | configured | unknown | unpaired / untested |
| Living Room | live_verified | live_verified volume R/W | none configured | **Companion paired; Netflix search_ready live verified** |
| Office | live_verified | none configured | unknown | unpaired / untested |
| Study | n/a | n/a | discovered | unpaired / untested |

## Cross-cutting (automated)

| Item | Status |
|---|---|
| Room registry / duplicate ID rejection | automated |
| Mac/Sonos filter from Apple TV inventory | automated + live |
| Volume ceiling / power-off confirm / kill switch | automated |
| Idempotency + per-room locks | automated |
| Secret redaction | automated |
| CLI/MCP parity facade | automated |
| Dry-run planning | automated |
| Screenshot → typed provider state | automated + Living Room live_verified |
| Raw CLI/MCP remote gating | automated |
| CLI connection shutdown | automated + live_verified |
