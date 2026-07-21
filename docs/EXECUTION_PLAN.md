# Execution plan

This plan is designed for an agent that can execute autonomously and delegate
bounded work to sub-agents. Human input should be requested only at the live
pairing/control gates or when a decision materially changes security or scope.

## Current checkpoint (2026-07-20)

Phases 0-3 are implemented for the core and Living Room is the current reference
room. The first Phase 4 slice—Netflix `search_ready`—is live verified from Apple
TV Home through exact query readback. See `LIVING_ROOM_NETFLIX_LIVE_GATE.md`.

Next execution order:

1. add exact visible result matching and stop at title detail;
2. add a separately gated Resume action with now-playing verification;
3. pair/bind additional rooms one at a time;
4. ship the restricted SSH Siri Shortcut, then the authenticated local API/App
   Intent client;
5. continue Sonos/physical-TV adapters without coupling them to provider UI
   navigation.

## Global operating rules

1. Re-read local agent/repository instructions and inspect Git state first.
2. Independently challenge the architecture before preserving it.
3. Verify current official package/API state as of execution time.
4. Keep research and discovery read-only until a named live gate.
5. Do not confuse a passing mocked test with proof against a physical device.
6. Continue automatically after each non-human gate.
7. When user input is required, ask one precise question and preserve all state
   needed to resume immediately.
8. Do not push, publish, deploy, modify the Mac mini, or create cloud resources
   without explicit authorization.

## Recommended delegation

The lead Grok 4.5 agent should remain integration owner and use sub-agents in
parallel where useful. Good bounded lanes are:

- **Protocol/core agent:** independently review `pyatv` 0.18/current APIs,
  implement Apple TV adapter and fakes.
- **CLI/MCP agent:** scaffold interfaces over agreed application contracts;
  never implement device logic independently.
- **Content research agent:** verify current provider deep-link behavior and
  produce evidence-backed adapters/fixtures without account-cookie capture.
- **TV/Sonos agent:** independently evaluate Sonos and Sony/HA routes, beginning
  read-only.
- **Test/security agent:** adversarially review room resolution, credential
  handling, mutation gates, retries, and verification semantics.

Prevent overlapping edits by assigning modules or having research agents return
recommendations before implementation. The lead agent must review and integrate
every contribution; sub-agent completion is not acceptance evidence.

## Phase 0 — independent review and clean baseline

### Work

- Read every file listed in `README.md`.
- Inspect Git status and existing environment.
- Re-run passive `atvremote scan` and compare with the dated snapshot.
- Verify current `pyatv`, MCP SDK, Home Assistant, and Sonos tooling from primary
  sources.
- Inspect `mcp-pyatv` source/status as prior art.
- Decide whether this architecture is still the best route and document any
  changes in a decision log.

### Gate 0

- Written go/no-go recommendation.
- Explicit stale assumptions and scope corrections.
- Initial module/test plan.
- No device mutations.

If the review still supports the project, proceed automatically to Phase 1.

## Phase 1 — repository and deterministic core

### Work

- Establish `pyproject.toml`, lockfile, `src/` layout, tests, lint, and typing.
- Implement domain models, error taxonomy, structured results, room registry,
  aliases, capabilities, planner/executor contracts, redacted audit logging, and
  dry-run behavior.
- Add a sanitized example configuration and a local-private config convention.
- Implement fake adapters and contract tests before live adapters.
- Implement read-only discovery CLI with human and JSON output.

### Gate 1

- Unit/contract tests, lint, and type checks pass.
- Discovery filters Macs and Sonos from Apple TV results.
- Duplicate room-friendly names cannot cause cross-device actions.
- No secrets or private tokens tracked.

Proceed automatically to Phase 2.

## Phase 2 — Apple TV read-only adapter

### Work

- Implement current `pyatv` discovery, stable identity, endpoint refresh,
  capability reporting, credential-storage plumbing, and status connection.
- Support unpaired devices gracefully.
- Expose `discover`, `rooms`, `capabilities`, `status`, and pairing-session
  initiation without yet completing pairing.
- Add reconnection, timeouts, and classified failures.

### Gate 2

- Automated Apple TV adapter tests pass against fakes/fixtures.
- Live discovery returns the expected five current tvOS devices or documents
  exact drift.
- Unpaired status is a clear authentication result, not a crash.

Proceed to Human Gate A.

## Human Gate A — first pairing

Recommended target: Theater Apple TV. Resolve its real stable ID from the private
runtime configuration and current passive discovery; never use the sanitized
example identifier for a live pairing action.

Ask the user to be physically present at the Theater TV and confirm that it is safe
to display pairing prompts. Then:

1. Start the minimum required current-tvOS pairing flow.
2. Ask for the displayed PIN only when ready.
3. Never echo/store the PIN in source or logs.
4. Persist resulting credentials outside the repository with restrictive
   permissions.
5. Verify a read-only status call.

If multiple protocols require separate PINs, explain each one as it occurs. Do
not pair all five TVs in one burst.

## Phase 3 — Apple TV control and verification

### Work

- Implement power, app listing/open, transport, remote key, text entry, volume
  capability detection, deep-link launch, and now-playing.
- Expose identical CLI and MCP behavior.
- Add per-room locks, idempotency, bounded verification polling, and dry-run.
- Build an app alias registry from live bundle IDs rather than assumptions.

### Human Gate B

Before each potentially disruptive test category, confirm the user is watching
the correct room and playback may be interrupted. Test Theater in this order:

1. Read-only status and app list.
2. Open a harmless agreed app.
3. Transport and remote input.
4. Apple TV power/wake and physical-TV CEC observation.
5. Power off only with an explicit same-turn confirmation.

### Gate 3

- CLI and MCP results match.
- Apple TV action and physical-TV result are reported separately.
- Failures do not cause unbounded retries or wrong-room fallbacks.
- Live evidence is recorded in a local test report without credentials.

## Phase 4 — content/deep-link slice

### Work

- Ask for the user's top one or two streaming services only when the choice is
  needed; do not block earlier phases on the full provider inventory.
- Implement direct URL and saved-alias resolvers first.
- Verify one real share/deep link per selected provider.
- Add provider-specific resolution only when there is a stable, reviewed source.
- Implement honest `resume` outcome levels from the product specification.
- Avoid private account/session API capture unless separately approved.

### Gate 4

- At least one user-selected content link opens in the correct Theater app.
- Now-playing evidence is captured when exposed.
- The result does not claim exact progress when unobservable.
- Broken raw AirPlay URL streaming cannot be mistaken for failed app deep-link
  launch.

## Phase 5 — Sonos

### Work

- Independently compare current `sonoscli`, SoCo, and Home Assistant routes.
- Rediscover true Sonos household topology, zone coordinators, bonded speakers,
  and stable player IDs.
- Implement read-only zone/status/volume first.
- Bind Theater room to the correct user-facing Sonos zone, not a bonded component.
- Implement exact volume, mute, and basic transport with verification.

### Human Gate C

Ask permission immediately before the first audible change. Use a small,
reversible volume change within the configured ceiling, then restore the prior
level if requested.

### Gate 5

- Exact Theater volume read/set/read verification works.
- Duplicate names cannot select the Apple TV or a Sonos Sub accidentally.
- Current grouping/coordinator state is handled rather than assumed.

## Phase 6 — physical TV and broader home adapter

### Work

- Start with read-only inspection of Theater Sony BRAVIA at its rediscovered
  endpoint.
- Evaluate Sony's local API/Android TV Remote versus Home Assistant based on
  current support, authentication, and setting coverage.
- Implement safe power state, power control, and input selection first.
- Compare direct physical-TV state with Apple TV CEC behavior.
- Add Home Assistant adapter/config only if an instance exists or the user
  authorizes installation.
- Inventory other room TV brands/models before promising support.

### Human Gate D

Pair Sony/Android TV only while the user is present. Ask before changing input
or power. Do not touch picture calibration, service menus, factory reset, or
device-management settings.

### Gate 6

- Theater full-room status distinguishes Apple TV, BRAVIA, and Sonos.
- Watch scene wakes/opens/sets volume with per-step evidence.
- Partial failure is actionable and does not misreport overall success.

## Phase 7 — hardening and handoff-quality release

### Work

- Security review, secret scan, retry/concurrency adversarial tests.
- Installation and onboarding docs for portable Mac mode.
- Optional launchd/service instructions, not installation, for Mac mini mode.
- MCP configuration examples for Cursor/Codex without embedding credentials.
- Capability report for every discovered room.
- Document unsupported app installation and TV settings honestly.
- Run all automated checks and the authorized live smoke suite.

### Gate 7

- Acceptance checklist in `docs/ACCEPTANCE_TESTS.md` is completed with evidence.
- Fresh install works from documented commands.
- No secrets are present in Git status/history/output artifacts.
- Final report separates automated, live-verified, degraded, and untested
  capabilities.

## Phase 8 — later extensions, not allowed to distort the core

- Apple Configurator/MDM evaluation for tvOS app installation.
- Screenshot-assisted App Store/UI navigation.
- IR emitters for non-network TVs.
- Natural-language provider search beyond verified deep links.
- Remote authenticated deployment on the Mac mini.
- Additional vendor-specific picture/input controls.

These extensions may be investigated in parallel, but they must not delay or
weaken the reliable Apple TV + Sonos + room-control core.
