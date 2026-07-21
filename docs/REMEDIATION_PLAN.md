# Home Media v0.2 remediation plan

Date: 2026-07-20

> **Scope update:** The 15 lifecycle and safety repairs in this document remain
> required. Its system-Search-only content strategy is superseded by
> `docs/PROVIDER_AUTOMATION_PLAN.md`, because Apple Search does not cover the
> user's full Netflix catalog. Native Search is now one route in a provider-aware
> ladder, not the whole product.

## Decision

Do not extend the current Netflix navigation sequence. The failure is partly an
imperfect tvOS interaction surface, but the repeated user/model correction loop
is primarily an architecture failure: a blind agent was given low-level remote
buttons and asked to infer provider UI state it could not observe.

The v0.2 target is a single semantic operation:

```text
prepare_title_search(room="living_room", title="Avatar: The Last Airbender")
```

It should wake the selected Apple TV, launch Apple's system Search app, place
the complete title in the focused search field, read the field back, and stop.
The user selects a result. The operation must not navigate Netflix, select a
result, claim an episode resumed, or improvise additional remote presses.

This is the shortest path to the user's actual outcome: one plain-language
request, one bounded device operation, and at most one human click. It also
works without building and maintaining a coordinate map for every streaming
app.

## What the first implementation proved

Keep these foundations:

- Stable room and device identity is the correct model. Five tvOS devices can
  be distinguished without relying on ephemeral IP addresses.
- Direct `pyatv` discovery, stored Companion credentials, installed-app listing,
  and app launching work on the current network.
- Living Room is already Companion-paired and the installed app inventory contains
  Apple's Search app, bundle ID `com.apple.TVSearch`.
- Exact Sonos volume works through the Sonos adapter.
- CLI and MCP are both thin facades over an application service, which is the
  right intended shape.

Do not discard the repository and start over. Repair the lifecycle and safety
seams, then add the semantic search operation.

## What failed and why

The current interaction has three structural problems:

1. `press_remote_key` and `enter_text` expose implementation primitives to a
   model that has no screen state. The model can act, but it cannot reliably
   observe focus, overlays, profile pickers, or layout changes.
2. The MCP server creates a new `ApplicationService` for every call. Connection
   caches, pairing handlers, idempotency records, locks, and discovery state
   therefore disappear between adjacent tool calls.
3. Verification is too permissive. Seeing any app or any now-playing title can
   be reported as progress even when it is not the requested title or provider.

The correct repair is semantic automation with explicit postconditions, not a
larger library of blind button recipes.

## Non-negotiable audit findings

The v0.2 implementation is not eligible for a live mutation until all of these
are fixed or deliberately removed:

### Priority 1

1. Configuration loading fails open: a missing or invalid normal config can
   silently fall back to a mutation-enabled Theater seed configuration.
2. MCP constructs a fresh service for each tool call, losing all process state.
3. CLI pairing start and finish are documented as separate processes although
   the live `pyatv` pairing handler exists only in memory.
4. Android TV endpoints discovered in one CLI process are unavailable to a
   later pairing or control process.
5. Idempotency lookup occurs outside the room lock and a key is not bound to a
   canonical request fingerprint.
6. The executor can retry a mutation after an ambiguous post-send failure,
   which can duplicate a key press or relative-volume change.
7. Relative volume changes bypass the configured volume ceiling.
8. Content verification does not match the requested title, provider, or app.
9. Physical-TV input verification trusts the requested value as the observed
   value instead of independently reading the input.

### Priority 2

10. Apple TV operations rescan, reconnect, and often perform a status read for
    each primitive action, creating avoidable multi-second latency.
11. Per-device status errors are discarded from the typed room result.
12. Configured physical TVs can be presented as discovered without live network
    evidence.
13. A pairing PIN is accepted on the command line and can leak through shell
    history or process listings.
14. Private network snapshots are not actually covered by `.gitignore`.

### Priority 3

15. README and installation documentation contradict live state and test counts.

There is also dead configuration: `provider_prefs.netflix.profile_index` is
saved but no production path reads it. Do not build v0.2 around it.

## Phase 0: preserve and freeze

- Work in the existing repository and preserve all current user files. The
  repository currently has no commits, so treat every file as potentially
  user-owned and avoid broad rewrites.
- Do not read, print, copy, or commit stored credentials. The external `pyatv`
  credential file must remain outside the repository with mode `0600`.
- Do not run any live mutation while repairing code. Fakes, fixtures, passive
  discovery, and read-only status are allowed.
- Do not push, publish, or initialize external services.
- Protect the exact private discovery artifacts in `.gitignore`; do not delete
  them. Provide redacted examples if committed discovery documentation is
  useful.

## Phase 1: repair lifecycle and safety

### Long-lived runtime

- Build one `ApplicationService` during MCP server startup and close it during
  shutdown using the installed MCP SDK's supported lifespan mechanism.
- Give adapters explicit async shutdown hooks.
- Keep an Apple TV connection manager keyed by stable device ID. It should:
  cache discovery results for a bounded TTL, single-flight concurrent connects,
  reuse healthy connections, invalidate on disconnect/authentication/stale
  endpoint, rediscover only when necessary, and close connections on shutdown.
- Keep executor locks and idempotency state on the singleton service.
- Measure warm and cold paths. Avoid a pre-action status scan when it is not
  required for safety or verification.

### Fail-closed configuration

- Normal mode must fail with a clear typed configuration error if the config is
  missing or invalid.
- Seed configuration is permitted only behind an explicit `--demo`, `--fakes`,
  or equivalent test-only choice. It must never be an implicit fallback for a
  live adapter.

### Pairing and endpoint persistence

- Pairing is an interactive, same-process setup flow. A CLI command should
  start pairing, securely prompt for the PIN without echo, finish, and close the
  handler in that one process.
- Do not put a PIN in argv, logs, audit records, an LLM transcript, or a normal
  MCP tool argument. Remove or disable the public PIN-taking MCP flow unless a
  genuinely secret input channel is available.
- Android TV control must re-resolve a configured stable target from current
  discovery or persist a non-secret endpoint cache. It cannot depend on an
  earlier CLI process's memory.

### Mutation semantics

- Bind each idempotency key to a canonical request fingerprint including room,
  intent, targets, and normalized parameters. Reusing a key for a different
  request is an idempotency conflict.
- Make duplicate lookup/in-flight registration atomic with the per-room lock or
  an equivalent single-flight mechanism. Concurrent duplicates execute once.
- Separate safe pre-dispatch retries from ambiguous post-dispatch failures. A
  mutation may be retried only when there is evidence it was not sent. If the
  outcome is ambiguous, observe the device when possible and otherwise return
  `unknown_outcome`; do not send the mutation again automatically.
- Enforce the volume ceiling for both absolute and relative requests. Convert a
  relative Sonos request to a bounded absolute target after a read. Reject a
  relative route when the ceiling cannot be guaranteed.
- Physical-TV input is verified only by an independent read after the command.
  If the protocol cannot read it, return unverified/degraded rather than copying
  the request into observed state.
- Preserve per-device errors in `RoomStatus` using a typed errors/observations
  collection rather than dropping them.
- Distinguish configured identity from current discovery evidence.

### Default MCP surface

The default agent-facing MCP should expose semantic operations. Raw remote
buttons and raw text entry should be disabled by default or placed behind an
explicit debug opt-in such as `HOME_MEDIA_ENABLE_RAW_REMOTE=1`. They can remain
available to a human developer, but an LLM should not choose them when a
semantic operation exists.

## Phase 2: implement native title search

### Public interfaces

Add equivalent typed operations to the service, CLI, and MCP:

```text
ApplicationService.prepare_title_search(room, title, *, wake=True, dry_run=False,
                                        idempotency_key=None)

home-media search prepare --room living_room --title "Avatar: The Last Airbender"

prepare_title_search(room="living_room", title="Avatar: The Last Airbender")
```

Use a dedicated request/result model. A successful result should contain at
least:

- resolved room key and stable Apple TV ID;
- requested and normalized title;
- Search app bundle ID;
- whether a wake was requested/accepted and whether physical-TV CEC state is
  independently known;
- observed keyboard focus state;
- read-back query text or a non-sensitive normalized digest;
- stages and per-stage latency;
- execution and verification statuses;
- `selected_result: false`;
- warnings about provider coverage and physical-TV state.

Use precise terminal states such as `query_verified`, `search_open_unverified`,
`keyboard_not_focused`, `query_mismatch`, and `failed`. Never return `playing`
or `resumed` from this operation.

### Deterministic sequence

1. Resolve exactly one room and its stable Apple TV ID.
2. Reuse or establish one Companion connection.
3. If requested, wake the Apple TV. Do not claim the physical TV is on unless a
   direct TV adapter confirms it; CEC wake is evidence only.
4. Launch `com.apple.TVSearch` directly.
5. Wait, with a bounded timeout and keyboard focus listener/polling, for
   `atv.keyboard.text_focus_state == KeyboardFocusState.Focused` using the
   current `pyatv` keyboard API.
6. If the Search app needs one deterministic transition to focus its field,
   support only that documented system-Search transition. Prove it once on the
   live device. Do not create provider-specific arrow maps.
7. Clear existing text, set the complete normalized query in one operation, and
   read it back with `text_get`.
8. Compare the read-back value using a documented normalization rule.
9. Return immediately with `selected_result: false`. Do not press Select, enter
   a provider app, or choose a result.

The sequence is one application-core operation and one MCP tool call. The LLM
must not coordinate it by issuing several primitive tool calls.

### Honest provider coverage

Apple's system Search searches participating apps, not every installed app.
Current Apple documentation says US coverage includes many major services, but
Netflix coverage is limited to Netflix Originals and YouTube is not listed.
Therefore:

- Do not promise that every title or provider will appear.
- The operation succeeds when the intended query is visibly/technically ready,
  not when a particular provider result is assumed.
- If a title is absent, return a provider-coverage warning and leave the user in
  Search.
- Only after collecting real misses should a later version add a small number
  of provider-specific search adapters. Those adapters must be deterministic,
  observable, and shared across titles; never create a button map per show.

Primary references to re-check before implementation:

- Apple Search support and participating apps:
  <https://support.apple.com/en-mide/106342>
- Apple Siri search/play behavior:
  <https://support.apple.com/en-us/105019>
- pyatv app launch API: <https://pyatv.dev/development/apps/>
- pyatv keyboard API: <https://pyatv.dev/development/keyboard/>

## Phase 3: automated proof before touching a television

The full gate is:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Add tests that fail on the current defects, including:

- invalid or absent normal config fails closed;
- demo/fake seed requires explicit opt-in;
- an in-process MCP client executes multiple sequential and concurrent calls
  against the real server lifespan and proves the service factory ran once;
- pairing start/finish works in one interactive process and PINs are absent from
  argv, logs, results, and audit events;
- Android TV can resolve its endpoint in a fresh process;
- the same idempotency key plus the same fingerprint executes once, including
  concurrent calls;
- the same idempotency key plus different parameters returns a conflict;
- an ambiguous post-send timeout never repeats a mutation;
- relative volume cannot cross the ceiling;
- physical input is never verified from the requested value alone;
- room status preserves per-device failures;
- configured-but-unobserved devices are not labeled discovered;
- content verification rejects a wrong app/title/provider;
- Search handles focused, delayed-focus, never-focused, clear failure, set
  failure, read-back mismatch, and successful read-back states;
- `prepare_title_search` never selects a result or calls provider navigation;
- raw remote tools are absent from the default MCP surface;
- warm connection reuse and scan caching are asserted with fake counters;
- shutdown closes every persistent connection.

Tool-name listing is not an MCP integration test. Exercise the actual MCP
transport or supported in-process client through at least two calls so service
lifetime and state persistence are tested.

After tests pass, conduct an adversarial code review focused on safety,
concurrency, retry boundaries, lifecycle cleanup, secret exposure, result
truthfulness, and accidental live side effects. Fix every accepted priority 1
or priority 2 finding and rerun all gates. A reviewer report is evidence only;
the lead agent must inspect and resolve each finding.

## Phase 4: one bounded Living Room proof

This phase requires the user to be present. Do not begin it merely because the
automated suite passes.

Ask exactly for approval to open Search and type an agreed title on Living Room.
State that this may wake the Apple TV/physical TV through CEC, will not select a
result, and will not start playback.

Then:

1. Run one `prepare_title_search` call.
2. Record the target stable ID and stage latencies without exposing credentials
   or private network data.
3. Ask the user one question: whether the correct title is visible in Apple TV
   system Search results.
4. Stop. Do not compensate with raw arrows if the proof fails.

Pass criteria:

- correct room and stable Apple TV resolved;
- Search app opened;
- full query appeared and read back correctly;
- no result was selected;
- no provider UI was navigated;
- no more than one human observation was required;
- warm query-ready latency target is median <=3 seconds and p95 <=6 seconds
  over five non-disruptive trials; report actual numbers even if the target is
  missed;
- CEC/physical-TV state is described honestly.

On failure, capture the structured stage, focus state, timing, current app when
available, and error class, then stop for diagnosis. Do not build a Netflix map
as a fallback.

## Phase 5: optional vision spike, separately authorized

Vision is useful for exceptional recovery and verification, but it is not the
primary solution. Retail tvOS does not expose a supported general-purpose
network framebuffer or remote-desktop API to third-party controllers.

The most plausible software path is an on-demand developer screenshot through
Xcode device pairing and `pymobiledevice3` DVT services. It has material costs:

- the current Mac has Command Line Tools but not full Xcode or
  `pymobiledevice3`;
- Apple TV developer pairing/Developer Settings require user interaction;
- tvOS tunnel discovery and screenshot capture are slower and more fragile than
  Companion control;
- third-party accessibility-tree support is currently not a dependable basis
  for autonomous navigation;
- a multi-Apple-TV house requires exact mapping from the requested room's stable
  Apple TV ID to the matching developer tunnel. Selecting the first tunneled
  device is unsafe.

Do not install Xcode, install `pymobiledevice3`, enable developer settings, pair
a developer device, or change the Apple TVs without a separate explicit user
approval. If approved, the spike passes only when one named room produces an
on-demand screenshot and the code proves it cannot silently capture another
room. Screenshots should be checkpoints, not a 5-second feedback loop for every
button press.

References:

- Apple device pairing:
  <https://developer.apple.com/documentation/xcode/pairing-your-devices-with-your-mac>
- Apple Developer Mode:
  <https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device>
- pymobiledevice3: <https://github.com/doronz88/pymobiledevice3>
- Existing experimental tvOS MCP status:
  <https://github.com/crlian/mcp-pyatv/blob/main/STATUS.md>

An HDMI capture device is the more literal visual feed, but it adds hardware
per active route and HDCP can black out protected video. It is not justified for
the native-search workflow.

## Deferred capabilities

These are outside v0.2 and must not delay the native-search proof:

- exact episode/progress resume across arbitrary providers;
- installing App Store apps (no supported general remote API);
- autonomous navigation of provider-specific menus;
- picture calibration, service menus, purchases, account changes, or resets;
- physical-TV settings and input automation beyond independently observable,
  explicitly paired adapters;
- multi-room scenes and power-off;
- Sonos topology/group changes beyond already proven exact single-room volume.

## Definition of done

v0.2 is done only when:

1. all 15 audit findings are fixed, removed, or explicitly documented as an
   unsupported path that is no longer exposed;
2. lint, types, unit, contract, and real MCP lifecycle tests pass;
3. the default MCP surface favors semantic operations and guards raw remote
   primitives;
4. one approved Living Room call opens system Search and visibly types the full
   title without selecting anything;
5. the user confirms the expected search results in one observation;
6. actual latency is reported;
7. documentation matches reality and names unsupported/deferred behavior
   without claiming resume or physical-TV verification.
