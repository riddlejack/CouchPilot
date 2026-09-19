# Independent review decision log

Date: 2026-07-20 (America/Chicago)
Reviewer: lead implementation agent

## Verdict: GO

The project is real. The packet architecture is the right path. No existing
project replaces a room-aware typed core with honest verification for this
household. Proceed to implementation immediately.

## Live discovery vs snapshot

Passive rediscovery confirmed the July 20 ~15:45 private snapshot with no
material drift. Real stable IDs and LAN addresses are intentionally kept in the
ignored runtime inventory rather than Git:

| Device | Status |
|---|---|
| Theater Apple TV | Unchanged, unpaired |
| Family Room ATV | Unchanged |
| Bedroom ATV | Unchanged |
| Office (2) | Unchanged |
| Living Room ATV | Unchanged at discovery; paired later in this project |

Also confirmed: Theater TV + Study Android TV Remote v2 / Cast; Sonos RINCON
ads including bonded Subs; naming collisions (`Theater` ATV vs `Theater (2)` Beam).
No Home Assistant mDNS/`8123` listener found on the bridge host or gateway.
Raw TCP probes to known ports often appear filtered; Bonjour/`atvremote scan`
remain the authoritative passive signals.

No pairing or mutation was performed.

## Current package evidence

| Package | Finding | Decision |
|---|---|---|
| `pyatv` | Latest PyPI is `0.18.0` (2026-06-19); tvOS 26 compatibility release | Pin `pyatv>=0.18,<0.19` |
| `mcp` | Stable latest `1.28.1`; `2.0.0b*` pre-release exists | Pin stable `mcp>=1.28,<2` with FastMCP |
| `mcp-pyatv` | Alpha prior art; reconnect ideas useful; screenshot/recipe path is the opposite of our product rule | Study only; do not depend |
| Sonos | SoCo is the Python-native local UPnP library; sonoscli is a solid Go CLI | Prefer **SoCo** in-process |
| Sony | `androidtvremote2` 0.3.1 implements Remote v2 (port 6466) | Prefer **direct** adapter; HA optional later |
| Content | `launch_app(url)` documented with Netflix/Disney+/Max/Apple TV+ examples | Direct URL + aliases MVP |

## Assumptions corrected / rejected

1. **No universal Apple TV resume/watch-history API.** `resume` opens a target and
   reports `verified_playback` / `opened_target` / `opened_app_only` honestly.
2. **No remote-protocol app install.** Unsupported in MVP.
3. **MRP is not advertised** on these tvOS 26.5 devices. Pair Companion + AirPlay
   (+ RAOP only if audio streaming is needed). Do not require MRP.
4. **Home Assistant is absent** on this LAN today. Do not block Theater Sony/Sonos on HA.
5. **Apple TV wake ≠ physical TV on.** Separate evidence fields are mandatory.
6. **CEC volume is relative.** Exact volume routes to Sonos for Theater.
7. **mcp v2 beta is not production.** Stay on mcp 1.x until stable v2 lands.

## Architecture retained (with tweaks)

Retain: typed application core; CLI + stdio MCP facades; room registry with
stable IDs; capability/verification semantics; Theater-first live path.

Tweaks:

- Sonos adapter = SoCo 0.31.x (not sonoscli). Bind by `vendor_stable_id` (RINCON UID).
- Physical TV: `androidtvremote2` shipped now; **next Sony upgrade = `pybravia`**
  for power + HDMI input (PSK). HA absent on LAN — optional later.
- Credentials: pyatv `FileStorage` under `~/.config/home-media/` (mode `0600`).
- Content: direct URL + aliases only. **First live deep-link target: Apple TV+**
  share links. Netflix/YouTube stay low-confidence / alias-only until proven on
  this household’s tvOS (Netflix field-broken ~Sept 2025).
- `play_url` / raw AirPlay cast: **unsupported** on tvOS 26 until pyatv #2846 lands.
- Pair Theater: Companion then AirPlay; skip standalone MRP; RAOP optional.
- Discover default omits LAN addresses; `--include-private-inventory` for local debug.
- Kill-switch + volume ceiling + per-room locks + idempotency keys as specified.

## Research agents (2026-07-20)

Findings incorporated from parallel audits: pyatv APIs, MCP SDK, Sonos/Sony,
content deep links, and hostile safety review. Full writeups live in agent
transcripts; this log is the durable decision surface.

## Ranked risks

1. **tvOS 26 pairing/protocol quirks** — mitigate with Companion-first pairing and
   clear auth errors.
2. **Wrong-room / duplicate-name actions** — mitigate with stable-ID registry and
   refusing ambiguous aliases.
3. **False verification** (CEC, deep links) — mitigate with evidence levels.
4. **Credential leakage** — external storage, redaction, gitignore.
5. **Sonos bonded component confusion** — resolve by zone name + coordinator /
   `is_coordinator` / HT primary, never Sub RINCON alone.

## First live human gate

Theater Apple TV pairing only, after user confirms presence at the Theater TV.

## Screenshot observer (2026-07-20, code-only)

**Verdict: implement optional room-bound observer; stop blind live Netflix nav.**

- Cold-launch Netflix cannot safely Select without pixels; user narration is not a
  product vision system. Live `prepare_content` now fail-closes without an observer
  binding (`screenshot_gate`).
- **License:** `pymobiledevice3` is GPL-3.0. Keep it an optional extra and call it
  only via subprocess argv (`--userspace --udid <exact>`). Never import it into
  `home-media` core; never take `devices[0]`.
- **Capture path preference:** short-lived no-root DVT screenshot with userspace
  tunnel first. No Xcode install, no sudo, no LaunchDaemon without separate approval.
- **Identity:** `room → Apple TV stable id → confirmed observer UDID` in private
  `~/.config/home-media/observer_bindings.json` (0600). Fingerprints only in logs/MCP.
- **DRM:** near-black frames → `blank_or_protected` (unobservable), not failure.
- **MCP:** `capture_room_screenshot` gated by `HOME_MEDIA_ENABLE_SCREENSHOT_DEBUG=1`,
  returns FastMCP `Image` + redacted metadata.
- Living Room first for live Gates A–D after code-only tests pass.

## Living Room Netflix search-ready (2026-07-20, live)

**Verdict: GO for the bounded `search_ready` product slice.**

- Replaced user narration with exact-room DVT screenshots and a local macOS
  Vision OCR classifier.
- The useful digital twin is the typed Netflix state graph, not a pixel-perfect
  clone or an LLM improvising remote arrows.
- Profile Select is allowed only when picker chrome, confidence, a highlighted
  profile anchor, and configured name `primary` all agree.
- Live failures exposed launch-budget and OCR-shape gaps; every failure stopped
  before result selection and became a regression test.
- Final Apple Home → Netflix → query replacement proof returned
  `query_verified`, `selected_result=false`, and `playback_started=false`.
- The next control milestone is exact result/title-detail matching; playback
  remains a separate higher-risk gate.

See `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md` for hashes and timings.
