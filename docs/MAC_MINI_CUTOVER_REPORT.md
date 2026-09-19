# Mac mini cutover report

Date: 2026-07-20
Branch: `codex/mac-mini-runtime`

> Historical checkpoint: the blockers and NO-GO below were accurate for this
> phase but were resolved for Living Room on 2026-07-21. Current live and rollout
> status lives in `CODEX_HANDOFF_CURRENT.md`, `docs/SIRI_BROKER_RUNBOOK.md`, and
> `docs/SIRI_SHORTCUT_DELIVERY.md`.

## Recommendation

**NO-GO for live cutover; GO for the single Living Room screenshot-pairing human
gate.**

The Mac mini is a recoverable, code-verified, read-only runtime with the global
mutation switch disabled. It is not yet the canonical live navigation runtime:
the host still lacks a working Living Room screenshot/developer pairing, so no
Mac-mini screenshot, provider observation, or Netflix `search_ready` outcome is
live-proven.

## Independent review findings

The initial implementation had three cutover-blocking safety defects: semantic
content and pairing bypassed the mutation kill switch, Sonos could fall back
from a configured stable UID to a friendly name, and a post-send content timeout
could redispatch under the same idempotency key. The default MCP surface also
exposed pairing, screenshot result identity was not rechecked, private-file
modes were not enforced at read time, and every screenshot paid the full DVT
startup cost.

Those defects are repaired on this branch and covered by focused tests. One
known P2 blocker remains outside the currently enabled surface: physical-TV
discovery still needs a stable identity or explicitly pairing-bound endpoint
before physical-TV mutations can be enabled. The first Shortcut transport is
strict and typed and now uses a private durable idempotency ledger across
separate SSH processes. The SSH/Siri path is still not accepted for live use
because no forced command or Shortcut has been installed or proven against the
Mac mini.

## Proof by capability

| Capability and room | Code-only proof | Live proof | Latency | Status | Remaining gate/blocker |
|---|---|---|---|---|---|
| Mac mini package/runtime | 152 tests; Ruff; mypy (46 files); source and wheel build | Real private config starts via non-network health probe | Startup health: under 1 second in this run; not a semantic command measurement | `verified` | None for code/read-only startup |
| Global mutation safety, all rooms | Kill-switch tests cover `prepare_content` and pairing; default MCP excludes pairing | Real config reports `mutations_enabled=false` | Not applicable | `verified` | Keep disabled through screenshot proof |
| Private runtime state, Mac mini | Owner/mode and Git-ignore checks; unsafe-mode tests; atomic private writes | Direct metadata-only inspection: directories 0700; runtime files 0600; current-user owned | Not applicable | `verified` | Never print or commit contents |
| Persistent screenshot worker, exact bound Apple TV | Reuse, exact identity, candidate cleanup, timeout, cancellation, shutdown, and redaction tests | None; health confirms zero workers spawned | Synthetic capture path only; household latency unmeasured | `unverified` | Fresh Living Room host pairing and one nonblank capture |
| Singleton hub, all configured rooms | Lifecycle, singleton, reload, drain, health, and shutdown tests | Real non-network health: six rooms, one observer binding, no warnings | Uptime field observed; device stages unmeasured | `verified` for process lifecycle; `unverified` for live device path | Run under supervised service only after cutover approval |
| Restricted Shortcut command, Living Room `search_ready` contract | Strict JSON, allowlist, no args/raw text/batches, durable cross-process idempotency, sanitized response, and safe-stop tests | None | Unmeasured | `verified` for boundary; `unverified` end to end | Forced-command/Shortcut installation only after cutover, then Siri proof |
| Living Room screenshot observation | Source-machine proof is preserved in the live-gate document | No Mac mini proof | Unmeasured | `unverified` on Mac mini | Physical-presence screenshot pairing |
| Living Room Netflix `search_ready` | Existing fixtures/state-machine tests pass | Source machine only; no Mac mini navigation attempted | Unmeasured on Mac mini | `unverified` on Mac mini | After capture proof, separate approval for one bounded test |

The 7-second median and 15-second p95 goals are not claimed. Stage fields and
worker reuse telemetry now exist, but trustworthy household measurements require
the repaired live screenshot channel.

## Read-only environment result

- Checkout is on the intended private `codex/mac-mini-runtime` branch and began
  from the recoverable `e406e14` baseline.
- Toolchain remains uv 0.11.30, Home Media CPython 3.12.13, screenshot CPython
  3.14.6 with pymobiledevice3 9.36.2, Swift 6.2.3, and OpenSSH 10.2.
- `~/.local/bin` is already in the execution PATH; no system setting changed.
- Private metadata has not drifted: Home Media and pymobiledevice3 directories
  are 0700, the three Home Media files and one pairing record are 0600, and all
  are owned by the current user.
- Known runtime private basenames are Git-ignored and none are tracked.
- The source machine remains intact as rollback.

## Changes and rollback

The branch changes the runtime hub/MCP lifecycle, Apple TV connection lifecycle,
content idempotency/timing, mutation gates, Sonos stable binding, screenshot
binding/capture/retention, private-file handling, CLI health/pair gating, and the
restricted Shortcut command, with unit and contract tests plus deployment docs.

Git rollback is a normal revert of the cutover commit on this branch. Runtime
rollback remains the unchanged source machine. No Mac mini pairing record was
replaced, no system service was installed, and no network/security setting was
changed, so there is no additional runtime mutation to undo from this phase.

## Next terminal gate

With the user physically present at Living Room and the television visible:

1. confirm it is safe to display the developer pairing code;
2. pair only the Mac mini screenshot/developer layer to the exact Living Room;
3. keep Home Media mutations disabled;
4. capture exactly one screenshot and require exact identity plus nonblank
   evidence;
5. stop and report. Do not navigate Netflix during this gate.

Only after that passes should the user be asked for separate approval of one
Living Room Netflix `search_ready` test. That test must stop before selecting a
result.
