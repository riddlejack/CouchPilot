# Product specification

## Product outcome

Create a local-first system that lets a person tell an agent what should happen
in a room, for example:

- "Turn on the theater TV and resume Drive to Survive on Netflix."
- "Open YouTube in the Living Room."
- "Set the Family Room volume to 35 percent."
- "Pause the Bedroom Apple TV."
- "Switch the Theater TV to the Apple TV input."
- "What is playing in the Office?"

The system must translate the request into deterministic typed actions, target
the correct devices, execute through local protocols, and verify as much of the
outcome as the device exposes.

The product is not an autonomous screen-clicking robot. Visual UI navigation is
a fallback for unsupported actions.

## Primary users and operating contexts

- A household member interacting from Codex, Cursor/Grok, a terminal, or eventually text.
- Family members using simple room names without understanding device topology.
- An always-on Mac mini at the current home.
- A portable Mac on the same Wi-Fi at a future apartment, without router-admin
  access.

The core must run without Home Assistant or the Mac mini. Those are optional
deployment/integration choices, not prerequisites for Apple TV control.

## Required interfaces

### CLI

A stable CLI is the source of truth for behavior and diagnosis. It must support
human-readable output and `--json` output, return meaningful exit codes, and
offer `--dry-run` for mutating actions.

Proposed command vocabulary, subject to the implementer's independent review:

```text
home-media discover
home-media rooms list
home-media capabilities --room theater
home-media status --room theater
home-media pair start --room theater --protocol companion
home-media pair finish --session <id> --pin <pin>
home-media power on|off --room theater
home-media app list --room theater
home-media app open --room theater --app netflix
home-media content open --room theater --url <deep-link>
home-media content resume --room theater --service netflix --title <title>
home-media transport play|pause|stop|next|previous --room theater
home-media remote press --room theater --key home
home-media text enter --room theater --text <text>
home-media volume get --room theater
home-media volume set --room theater --level 35
home-media volume change --room theater --delta -5
home-media tv input --room theater --source apple-tv
home-media scene watch --room theater --service netflix --title <title>
```

### MCP

Expose a thin MCP server over the same application service used by the CLI. MCP
tool schemas must not bypass validation, authorization, dry-run logic, or
post-action verification.

Initial tools should be narrow and typed rather than a generic shell executor:

- `discover_devices`
- `list_rooms`
- `get_room_capabilities`
- `get_room_status`
- `start_pairing` and `finish_pairing`
- `set_power`
- `list_apps` and `open_app`
- `open_content`
- `get_now_playing`
- `control_playback`
- `press_remote_key`
- `enter_text`
- `get_volume`, `set_volume`, and `change_volume`
- `set_tv_input`
- `execute_watch_scene`

Use stdio for the first MCP transport. Do not expose an unauthenticated HTTP MCP
server to the LAN or internet.

## Room semantics

A room is an explicit aggregate, not a synonym for one device:

```yaml
room: theater
aliases: [theater tv, workout room]
apple_tv: <stable Apple TV id>
physical_tv: <stable Sony id>
audio: <stable Sonos id>
preferred_volume_target: sonos
preferred_power_path: apple_tv_cec
```

Rules:

1. Every mutating action requires an unambiguous room.
2. Friendly names may collide across device types; stable identifiers may not.
3. IP addresses are observed endpoints and must be rediscovered.
4. Fuzzy aliases are allowed only when they resolve uniquely.
5. A request targeting multiple rooms must be explicit and treated as higher
   risk.

## Capability truth table

| Capability | Initial support expectation | Required behavior |
|---|---|---|
| Discover Apple TVs | High | Filter actual tvOS devices; do not classify Macs or Sonos as Apple TVs. |
| Pair | High | Interactive PIN gate; persist credentials outside repository. |
| Read status/now playing | High | Return source, title, playback state, and confidence/availability. |
| Wake/sleep Apple TV | High | Verify Apple TV power state where protocol allows. |
| Wake physical TV via CEC | Conditional | Report Apple TV success separately from observed/confirmed physical-TV state. |
| List/open installed apps | High | Resolve app name to bundle ID and retain exact match evidence. |
| Deep-link content | Provider-dependent | Report requested URL, opened app, and observable playback state. |
| Resume content | Provider-dependent | Never claim exact episode/progress unless provider or now-playing evidence proves it. |
| Exact volume | Route-dependent | Prefer Sonos/direct-TV/HomePod absolute control; degrade honestly to relative CEC steps. |
| TV input/picture settings | Vendor/model-dependent | Expose only capabilities proven for that room. |
| Install arbitrary tvOS apps | Not an MVP capability | Return a structured unsupported/guided result; never fake success. |

## Content resolution

Use this reliability order:

1. User-supplied provider share/deep link.
2. Saved content alias mapped to a previously verified deep link.
3. Provider-specific resolver using a documented or independently reviewed
   public mechanism.
4. Launch the provider app and use typed search/text/remote commands.
5. Screenshot-assisted visual navigation, with explicit degraded confidence.

Do not capture or replay private provider APIs, cookies, or account sessions
merely because a HAR-derived client is technically possible. That path requires
a separate security/terms review and explicit user approval.

"Resume" usually means opening the provider content and relying on that
provider profile's stored progress. There is no universal Apple TV watch-history
API. The result model must distinguish:

- `verified_playback`: now-playing metadata proves expected content is playing.
- `opened_target`: provider app/deep link accepted, but playback/progress is not
  observable.
- `opened_app_only`: only the provider app was confirmed.
- `failed` or `unsupported`.

## Power behavior

Power is multi-device:

1. Wake Apple TV.
2. Allow HDMI-CEC to wake the physical TV/receiver when enabled.
3. If a physical-TV LAN adapter exists, query or command it directly.
4. Verify each layer independently.

Do not report "TV is on" merely because the Apple TV accepted `turn_on`.

## Volume routing

Select a configured route per room:

1. Sonos/direct audio endpoint with absolute volume.
2. Physical TV or receiver API with absolute volume.
3. HomePod/AirPlay output where absolute control is exposed.
4. Apple TV HDMI-CEC relative steps.
5. IR fallback, which is state-blind unless another sensor provides state.

If only relative volume is available, reject or explicitly degrade `set 35`;
do not invent a current absolute value.

## Safety requirements

- No credentials, PINs, tokens, cookies, or Apple Account data in Git or logs.
- Explicit room required for every mutation.
- Default configurable volume ceiling; require an override above it.
- No calibration, factory reset, service menu, MDM enrollment, account purchase,
  app purchase, or mass-room action without explicit confirmation.
- Never install or modify software on `home-media` until the user authorizes
  that host specifically.
- Bounded retries with timeouts; no command storms.
- Per-room action serialization to prevent conflicting agents.
- Idempotency key support for MCP mutations so agent retries do not double-run.
- Audit log records intent, target, adapter, timing, result, and verification,
  while redacting secrets.
- A kill switch/config flag must disable all mutations while preserving status
  and discovery.

## Non-goals for the first production slice

- A custom tvOS app.
- Reverse-engineering Apple protocols already covered by `pyatv`.
- A general computer-vision remote operator.
- Silent app purchasing or Apple Account automation.
- Support for every television brand before the actual household inventory is
  known.
- Internet-exposed control without authentication and network isolation.
- Claiming universal cross-provider content search or resume semantics.

## Definition of a spectacular first release

On the Theater system, a fresh agent can:

1. Discover the room and understand it contains three distinct targets.
2. Pair once with the Apple TV using a user-provided PIN.
3. Read current state, wake the Apple TV, and establish whether CEC wakes the
   Sony television.
4. List installed apps and open at least one selected streaming app.
5. Open at least one verified provider deep link and report honest evidence.
6. Read and set Sonos volume exactly, without confusing the Beam with Apple TV.
7. Execute the same operations via both CLI and MCP.
8. Pass automated tests without a live network.
9. Leave clear capability results for every other discovered room.

