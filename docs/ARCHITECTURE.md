# Proposed architecture

The receiving agent owns the final design decision after independent review.
This proposal is optimized for reliability, testability, and agent use.

## Core principle

CLI and MCP are interfaces over one typed application core. Neither interface
contains device logic. Adapters expose honest capabilities, the planner selects
a route, and the executor verifies the result.

```text
natural language agent
        |
        v
typed MCP tools ---------- human/scripts via CLI
        |                         |
        +-------- application core --------+
                     |                      |
               room registry        planner/executor
                     |                      |
              capability graph        verification
                     |                      |
       +-------------+------------+---------+---------+
       |                          |                   |
 AppleTVAdapter              SonosAdapter       PhysicalTVAdapter
    pyatv                 sonoscli/SoCo/HA       HA/vendor/CEC/IR
       |
 ContentResolver registry (deep links and explicit fallbacks)
```

## Technology baseline

Independently verify current versions before pinning:

- Python 3.12 for compatibility with the discovered environment.
- `uv` for environment and lockfile management.
- `pyatv>=0.18,<0.19` initially, unless current compatibility evidence supports
  a newer release.
- Typer or Click for CLI; choose based on current maintenance and testability.
- Pydantic for validated configuration and public result models.
- Official Python MCP SDK/FastMCP API after checking its current 2026 docs.
- `pytest`, async test support, Ruff, and a static type checker.
- YAML for user-managed room configuration; JSON for command output.

Avoid making Home Assistant mandatory. Support it as an adapter and deployment
option because it is excellent for heterogeneous TVs and Sonos, and it has a
native MCP server, but Apple TV core control must work directly on any same-LAN
Mac.

## Suggested package boundaries

```text
pyproject.toml
src/home_media/
  cli.py
  mcp_server.py
  config.py
  models.py
  errors.py
  logging.py
  registry.py
  capabilities.py
  planner.py
  executor.py
  verification.py
  adapters/
    base.py
    apple_tv.py
    sonos.py
    home_assistant.py
    physical_tv.py
  content/
    base.py
    registry.py
    direct_url.py
    netflix.py
    disney.py
    max.py
    apple_tv_plus.py
tests/
  unit/
  contract/
  integration/
config/
  homes.example.yaml
```

Do not create every provider module before it has meaningful behavior. An
adapter registry with one or two verified providers is better than five empty
facades.

## Domain models

### Device identity

Separate stable identity from discovery state:

```text
DeviceRef
  id: stable protocol/vendor identifier
  kind: apple_tv | physical_tv | audio | receiver
  room_key
  adapter
  aliases

DiscoveredEndpoint
  device_id
  address
  protocols
  model/os/version
  observed_at
  expires_at
```

Apple TV discovery must filter by tvOS/model rather than AirPlay or Companion
presence. The live LAN includes Macs and Sonos endpoints that would otherwise
be false positives.

### Capabilities

Capabilities are runtime facts with quality metadata, not hard-coded booleans:

```text
Capability
  name
  support: supported | degraded | unavailable | unknown
  adapter
  semantics: absolute | relative | best_effort
  evidence
  observed_at
```

Examples:

- `volume.set_absolute` may be supported by Sonos but unavailable through Apple
  TV CEC.
- `power.physical_tv_state` may be supported by Sony but unknown in a room where
  only Apple TV CEC exists.
- `content.resume_exact` is normally degraded unless metadata verifies it.

### Action results

Every mutation returns a structured result:

```text
ActionResult
  action_id
  requested_intent
  room_key
  targets
  plan
  started_at / finished_at
  execution_status
  verification_status
  observed_before / observed_after
  warnings
  retryable
```

Separate accepted command, completed command, and verified outcome.

## Planning and execution

The planner should be deterministic. It receives a typed request and room
capabilities, then selects a route. It must not call an LLM.

Example for `volume set theater 35`:

1. Resolve `theater` uniquely.
2. Load volume target preference.
3. Check whether Sonos exposes absolute volume.
4. Apply configured safety ceiling.
5. Read prior level when possible.
6. Set once with idempotency key.
7. Read after and compare within tolerance.
8. Return verified/degraded/failure result.

Example for `scene watch`:

1. Resolve room and content request.
2. Power Apple TV.
3. Attempt/query physical-TV power through direct adapter; otherwise rely on
   CEC and mark physical state unverified.
4. Select input only if a safe direct path exists.
5. Resolve app/deep link.
6. Open content.
7. Verify current app/now-playing with bounded polling.
8. Set requested volume through the preferred audio adapter.
9. Return per-step evidence; do not collapse partial success into success.

## Adapter contracts

Adapters must be narrow and mockable. They should expose capabilities before
actions. Suggested contracts:

- `discover()`
- `get_status()`
- `get_capabilities()`
- `pair_start()` / `pair_finish()` where applicable
- `set_power()`
- `list_apps()` / `open_app()`
- `open_url()`
- `get_now_playing()`
- `control_transport()`
- `press_key()` / `enter_text()`
- `get_volume()` / `set_volume()` / `change_volume()`
- `set_input()`

An adapter must return `unsupported`, not `False`, when the feature does not
exist. Transport/network failure, authentication failure, device rejection, and
unsupported capability are different error classes.

## Apple TV adapter

- Build on `pyatv`; do not duplicate protocol implementation.
- Load `pyatv` credentials from an external storage location with mode `0600` or
  a secure OS-backed store if practical.
- Pair only the protocols required by current tvOS. The live devices advertise
  Companion, AirPlay, and RAOP, not MRP.
- Cache connections but reconnect with bounded exponential backoff.
- Resolve by stable identifier; rediscover IP before connection.
- List app bundle IDs and persist normalized app aliases separately.
- Treat `play_url` raw AirPlay streaming separately from Companion app/deep-link
  launch because their compatibility and failure modes differ.

## Content resolvers

Resolvers produce an explicit `ContentTarget`; they do not control devices.

```text
ContentTarget
  provider
  canonical_url or app_bundle_id
  title/series/season/episode when known
  resolution_source
  confidence
  expected_app
```

Start with direct URLs and saved aliases. Add provider-specific resolution only
after a live verified example. Never hide provider uncertainty behind a generic
`resume` success.

## Sonos adapter

Independently compare current `sonoscli`, SoCo, and Home Assistant behavior.
Prefer a local supported interface with exact volume, discovery, topology, and
JSON output. The adapter should support:

- Stable player/zone resolution.
- Current group topology and coordinator awareness.
- Exact volume read/set and mute.
- Playback state and basic transport.
- Home-theater settings only after explicit capability discovery.

Do not assume each Sonos advertisement is a separate user-facing room; bonded
Subs and surrounds appear independently.

## Physical TV and Home Assistant adapters

The first useful direct target is Sony BRAVIA because Theater and Study were
confirmed. Evaluate the official/local Sony path, Home Assistant's Bravia/Android
TV integrations, and pairing requirements before choosing.

Home Assistant should remain the preferred broad integration plane for mixed TV
vendors, receivers, CEC bridges, and IR emitters. Our application may call Home
Assistant REST/WebSocket services while Home Assistant can separately expose
safe entities through its native MCP server.

Do not install Home Assistant on the Mac mini without explicit permission. If a
Home Assistant instance already exists, discover and inspect it read-only before
proposing changes.

## State, concurrency, and resilience

- Serialize mutating operations per room; allow parallel reads.
- Use short-lived discovery caches and current endpoint resolution.
- Bounded timeouts at every protocol call.
- Retry only classified transient failures.
- Reconnect once on stale sessions; do not loop indefinitely.
- Make scene steps resumable and report partial completion.
- Support `--dry-run` all the way through planning.
- Keep live integration tests opt-in with explicit environment markers.

## Security model

- Local stdio MCP first.
- If remote access is later added, prefer VPN/Tailscale plus authenticated
  transport; never bind a raw unauthenticated server to all interfaces.
- Store HA tokens and provider material outside config files.
- Redact URLs if they contain tokens/query credentials.
- Treat remote key presses, power, volume, app opens, and text entry as
  mutations in audit records.
- Never offer a generic arbitrary command/shell MCP tool.

## Deployment modes

1. **Portable local:** CLI/MCP runs on a Mac currently on the same LAN.
2. **Always-on home:** same package runs as a user service on the Mac mini after
   explicit approval, with credentials local to that host.
3. **Home Assistant augmented:** physical-TV/Sonos routes flow through an
   authenticated HA instance.
4. **Remote agent:** agent reaches the always-on host through an authenticated
   tunnel/VPN; local device protocols remain on the home LAN.

The code and configuration schema should be identical across modes.

