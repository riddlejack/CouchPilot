# Mac mini deployment and cutover

Date: 2026-07-20

## Decision

The Mac mini should become the canonical Home Media runtime because it is
always on and shares the Apple TVs' LAN. The source repository should live in a
normal local folder on that Mac. Do **not** run the repository from iCloud Drive.

iCloud Drive is a poor active-development/runtime transport here:

- it can synchronize partially written files and package caches;
- virtual environments and compiled helpers are machine-specific;
- the current working directory includes hundreds of megabytes of private
  screenshots that should not become source artifacts;
- Git metadata plus file-provider synchronization can produce confusing state;
- the actual runtime credentials live outside the repository and need a
  deliberate, encrypted transfer or fresh pairing.

Use a private Git remote for source and history. If no cloud source host is
desired, use a one-time Git bundle or encrypted `scp`/`rsync` over the LAN, then
make the Mac mini checkout canonical. An iCloud archive is acceptable only as a
one-time transport for a source-only export that is opened into a non-iCloud
folder; it should not contain secrets or remain the working copy.

## Transfer boundaries

### Source: version with Git

Include:

- `src/`, `tests/`, `config/`, and public `docs/`;
- `README.md`, `pyproject.toml`, `uv.lock`, and
  `.python-version`;
- public, sanitized live-gate documentation.

Exclude and regenerate:

- `.venv/`, `.venv-pmd3/`, `.venv-discovery/`;
- `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `dist/`;
- `.private/` screenshots and evidence;
- `docs/live/` and `discovery/` private LAN snapshots.

The current source checkout began with no commits or remote. Before using a
private Git host, create and inspect a first baseline commit and confirm that
ignored private/runtime files are absent from the commit. Do not turn a 1 GB raw
folder copy into the canonical source.

### Runtime state: transfer separately and encrypted

The source machine currently uses these classes of private state:

- Home/room configuration (`homes.yaml`);
- pyatv credentials (`pyatv.conf`);
- exact room-to-observer bindings (`observer_bindings.json`);
- a pymobiledevice3 Wi-Fi remote-pair record under the user's private
  pymobiledevice3 configuration directory.

These files contain private LAN identifiers and/or credentials. Never commit
them, paste them into an agent prompt, put them in an unencrypted archive, or
copy them through a public chat. Prefer an encrypted SSH transfer directly
between the Macs, with destination directories mode `0700` and files mode
`0600`.

Screenshots are not needed for cutover. Keep the source-machine screenshots as
rollback evidence and let the Mac mini generate fresh private evidence.

Transferred pyatv credentials may work on the same LAN. A transferred
pymobiledevice3 remote-pair record may be host- or environment-sensitive; its
presence is not proof. Test both read-only. If either fails, pair that layer
fresh on the Mac mini while the user is present at the exact TV.

## Phase A — source baseline on the current Mac

Before transfer:

1. Run `git status --short --branch` and confirm the repository boundary.
2. Run the full code-only gate:

   ```bash
   uv run pytest -q
   uv run ruff check .
   uv run mypy src
   uv build
   ```

3. Inspect `.gitignore` and use `git check-ignore -v` on representative private
   files, discovery files, virtual environments, and build artifacts.
4. Run a secret scan against the exact files proposed for the first commit.
5. Review the staged file list and diff before committing.
6. Create a private remote only after the user approves the host/account/name.

The baseline recorded while preparing this document was 108 passing tests,
Ruff clean, mypy clean, and a successful source/wheel build. Re-run rather than
trusting that snapshot during actual transfer.

## Phase B — bootstrap on the Mac mini

The receiving agent should first discover the actual host state rather than
assuming the source Mac's environment exists. In particular, verify Command
Line Tools/Swift, `uv`, Python, SSH, and local network reachability.

In a normal local checkout:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy src
uv build
```

Create the private runtime directory with mode `0700`. Place transferred runtime
files there with mode `0600`. Recreate the separate pymobiledevice3 environment
from its documented dependencies; do not copy a virtual environment between
Macs.

Keep live mutations disabled during bootstrap. Do not install a LaunchAgent,
change firewall settings, or expose a listener yet.

## Phase C — read-only Mac mini proof

Run in this order and preserve sanitized JSON results:

1. package/CLI version;
2. room list and exact stable-room resolution;
3. passive Apple TV discovery and drift comparison;
4. Living Room capability/status call;
5. screenshot observer binding lookup without printing the raw observer ID;
6. one Living Room screenshot capture and classifier result;
7. MCP startup and tool-list contract.

Expected outcomes:

- exactly one room resolves for `living_room`;
- no device mutation is sent;
- screenshot pixels remain private on the Mac mini;
- failures identify authentication, observer pairing, capture tunnel, OCR, or
  classification as distinct layers.

If authentication or capture fails, repair only that layer. Do not compensate
with blind button navigation.

## Phase D — live cutover gate

With the user watching Living Room and approving one navigation test:

1. start from Apple TV Home;
2. call Netflix `search_ready` for a harmless agreed title;
3. observe and classify each state transition;
4. verify exact keyboard query readback;
5. stop before result selection;
6. capture sanitized stage timings and the final result contract.

The Mac mini becomes the active runtime only after this passes. Retain the
source Mac's private state and checkout until the Mac mini has also survived a
restart and repeated the read-only health gate.

## Phase E — supervised service and Siri

After cutover, the next implementation order is:

1. persistent screenshot/tunnel worker to remove per-command startup latency;
2. singleton Home Media hub with graceful shutdown and health reporting;
3. restricted `home-media-shortcut` SSH command that accepts typed JSON only;
4. two-turn Siri Shortcut live proof;
5. authenticated local HTTP API and native iOS App Intents client;
6. remaining rooms, provider state machines, physical-TV adapters, and Sonos
   verification.

The first Shortcut must use a dedicated restricted command or account. It must
not give dictated text to a shell, expose the general Codex process, or allow
raw remote batches. The LLM/Siri layer parses intent; the deterministic service
validates and executes it.

### Implemented pre-gate runtime surfaces

The `codex/mac-mini-runtime` branch provides these building blocks without
installing a service or opening a listener:

- `home-media --json health` starts the configuration locally, reports redacted
  process health, and closes without probing a device;
- MCP owns one `HomeMediaHub` per stdio server process and drains active requests
  during deliberate reload or shutdown;
- screenshot capture defaults to a persistent exact-UDID subprocess worker;
  set `HOME_MEDIA_CAPTURE_MODE=oneshot` only as an explicit compatibility
  fallback;
- `home-media-shortcut` accepts one versioned JSON object on stdin and permits
  only semantic `prepare_content` with goal `search_ready`; it records request
  fingerprints and terminal/ambiguous outcomes in a private mode-0600 ledger so
  one-process-per-SSH retries cannot repeat a mutation.

Do not install `home-media-shortcut` as an SSH forced command before live
cutover. Its boundary and cross-process idempotency are code-verified, but the
SSH account/forced-command and iPhone Shortcut remain later explicit setup
work. A future supervised local transport may replace the ledger without
expanding the typed operation surface.

## Rollback

- Do not delete or overwrite the source-machine checkout or private state during
  cutover.
- If the Mac mini live gate fails, disable mutations there and continue from the
  last verified source-machine runtime.
- If transferred credentials behave ambiguously, remove only the affected Mac
  mini copy and pair fresh; do not modify the source copy.
- Keep Git source rollback separate from credential/runtime rollback.

## Definition of deployed

Deployment is complete only when all are true:

- the Mac mini has a clean, recoverable Git checkout;
- code-only gates pass locally;
- private files have correct permissions and are absent from Git;
- Living Room read-only status and screenshot observation work locally;
- one approved Netflix `search_ready` gate passes with exact query verification;
- the service survives a Mac mini restart and reports healthy;
- rollback to the source machine remains possible until the restart proof.
