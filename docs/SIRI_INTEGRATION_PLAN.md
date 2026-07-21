# Siri and iPhone integration plan

Date: 2026-07-20

## Backend milestone reached

Living Room now has a live-verified semantic command for Netflix search-ready:

```text
room + title + provider + goal
  -> exact-room screenshot observation
  -> deterministic provider state machine
  -> exact keyboard readback
  -> query_verified (no result Select)
```

That is the contract Siri should call. The next Siri implementation step is the
Stage 1 restricted SSH Shortcut; it no longer needs to send a message to a
general LLM or ask a human what is on the TV. The spoken success response for
the verified slice is: “Netflix search is ready for Avatar in Living Room. Select
a result.” Resume wording remains forbidden until playback is actually proven.

## Decision

Siri should be a typed client of Home Media Control, not the component that
drives remote buttons and not a conversational relay that must keep a particular
LLM task alive.

The durable architecture is:

```text
Siri on iPhone
      |
      v
iOS App Intent: room + title + provider + goal
      |
      v
authenticated Home Media API on the always-on Mac mini
      |
      v
ApplicationService -> route planner -> Apple TV / TV / Sonos adapters
```

There is also a no-new-app prototype:

```text
"Siri, Home Media"
      -> Shortcut asks/dictates the request
      -> Run Script over SSH or authenticated local request
      -> semantic Home Media command on Mac mini
      -> Siri speaks the structured result
```

This yields a usable Siri path before the native app exists, while the custom
iOS app provides the desired one-sentence experience later.

## What iOS 27 changes

Apple's 2026 App Intents guidance says the 27 releases let Siri access app
entities, resolve them semantically, and execute app intents using natural
language. App schemas provide system-understood action contracts, while
`EntityStringQuery` supports dynamic/server-backed entities that cannot be fully
indexed on the phone.

Relevant current Apple references:

- WWDC26, Siri and App Schemas:
  <https://developer.apple.com/videos/play/wwdc2026/240/>
- Apple Intelligence and Siri AI:
  <https://developer.apple.com/documentation/appintents/apple-intelligence-and-siri-ai>
- App Shortcuts:
  <https://developer.apple.com/documentation/appintents/app-shortcuts>
- App intent parameters:
  <https://developer.apple.com/documentation/appintents/adding-parameters-to-an-app-intent>
- System and in-app search schema:
  <https://developer.apple.com/documentation/appintents/app-schema-domain-system-and-in-app-search>
- Background and foreground intent modes:
  <https://developer.apple.com/documentation/appintents/appintent/supportedmodes>
- App Intents Testing:
  <https://developer.apple.com/documentation/AppIntentsTesting>
- Shortcuts “Use Model” and storage updates:
  <https://developer.apple.com/videos/play/wwdc2026/310/>

These are beta-era APIs and must be validated with the installed Xcode 27 SDK
and the user's exact iOS 27 beta. The current Mac has only Command Line Tools,
not full Xcode, so no iOS build or schema assumption is considered proven yet.

## Why ChatGPT/Codex Remote is not the primary bridge

The user's iPhone can remotely access Codex running on an always-on Mac mini
through the ChatGPT mobile app. OpenAI documents that the secure relay can load
host state and lets the user start/continue tasks from the Remote tab.

That is a useful manual and administrative path, but current official behavior
does not expose a Siri/App Shortcut action that sends text to a specified Codex
task on a specified host. Siri's “Ask ChatGPT” integration sends an ordinary
ChatGPT request; OpenAI documents mobile Codex as a separate Remote tab and
separate task history. Do not assume one routes into the other.

References:

- Codex mobile remote access:
  <https://openai.com/index/work-with-codex-from-anywhere/>
- ChatGPT/Codex task separation:
  <https://help.openai.com/en/articles/20001275/>
- Siri and ChatGPT setup:
  <https://help.openai.com/en/articles/10269382-setting-up-chatgpt-with-apple-intelligence>

If OpenAI later exposes a supported “send to remote Codex task” App Intent or
API, it can become another client adapter. It should still submit one typed Home
Media intent rather than ask a general coding agent to improvise device control.

## Stage 1: Shortcut prototype with no custom iOS app

### User experience

Initial two-turn form:

```text
User:  “Siri, Home Media.”
Siri:  “What should I play, and where?”
User:  “Resume Archer on Hulu in Living Room.”
Siri:  “Living Room is on. Hulu is opening Archer at your saved position.”
```

This is a real end-to-end solution while the one-sentence App Intents client is
being built.

### Preferred transport for the first proof

Use Shortcuts' Run Script over SSH action to the Mac mini. It avoids opening an
unauthenticated LAN port and reuses a mature encrypted channel.

Create a dedicated, narrowly authorized Mac account or forced-command SSH key.
The key may invoke only a wrapper such as `home-media-shortcut`; it must not
provide an unrestricted interactive shell.

The wrapper reads structured JSON or raw dictated text from standard input. It
never interpolates dictated text into a shell command. It calls the same
`ApplicationService` as CLI/MCP and prints one versioned JSON result.

Possible Shortcut steps:

1. Dictate Text or Ask for Input.
2. Optionally use iOS 27's Use Model action to extract a constrained dictionary:
   `room`, `title`, `provider`, `goal`, and optional `volume`.
3. Validate `room`, `goal`, and provider against fixed lists in the Shortcut.
4. Send the dictionary as standard input to the restricted SSH command.
5. Parse the returned JSON.
6. Speak the human-readable result or failure.

The model is a convenience parser only. The Mac mini must revalidate every
field, resolve exactly one room, enforce safety limits, and refuse unsupported
actions.

### Alternative local API prototype

Once the HTTP service described below exists, replace SSH with Shortcuts' Get
Contents of URL action. Do not expose the server publicly merely to simplify a
Shortcut.

## Stage 2: native iOS 27 App Intents client

Build a small SwiftUI app, provisionally named Home Media. Its visible UI is
mostly setup, room synchronization, connectivity, and recent action status. Its
main product surface is App Intents.

### Small entity surface

#### `RoomEntity`

- stable room key, such as `living_room`;
- display name;
- spoken synonyms/aliases;
- supported high-level capabilities;
- last synchronization time.

Rooms are fetched from the hub and cached locally so Siri can resolve “Living
Den,” “the den,” or “theater” even before a network round trip. Use stable IDs and
refresh App Shortcut parameters when aliases change.

#### `MediaTitleEntity`

- normalized title/request text;
- optional provider and provider content ID;
- display title;
- resolution confidence/source.

Use `EntityStringQuery` for arbitrary or server-backed titles. Frequently used
and verified titles may also be indexed/donated so Siri resolves “Archer” or
“pick up Avatar” with fewer follow-up questions. Do not mirror a streaming
provider's entire catalog into Spotlight.

#### `ProviderValue`

An `AppEnum` or small entity set: Auto, Netflix, Hulu, Disney+, Max, and only the
providers actually configured. Auto remains the default.

#### `PlaybackGoal`

An `AppEnum`: Search Ready, Open Title, Resume.

### First App Intents

#### `PrepareContentIntent`

Parameters:

- required `room: RoomEntity`;
- required `title: MediaTitleEntity` or schema-compatible search criteria;
- optional `provider: ProviderValue`;
- `goal: PlaybackGoal`, defaulting according to user preference;
- optional safe volume.

It runs without opening the app when possible, calls one semantic hub endpoint,
and returns spoken dialog derived from structured action status.

Examples to prove on the iOS 27 beta:

- “Use Home Media to resume Archer on Hulu in Living Room.”
- “Search Netflix for Avatar in Living Room with Home Media.”
- “Turn on the Theater TV and pull up Severance with Home Media.”

Evaluate the current `.system.searchInApp` schema for the Search Ready variant
because it gives Siri a system-understood free-form search contract. Keep the
room-aware prepare/resume operation as a custom intent if no honest video or
remote-control schema matches. Do not misuse the audio playback schema for TV
video merely to gain phrases.

#### `SetRoomVolumeIntent`

Parameters: room and absolute level. It calls the exact-volume route and returns
the observed read-after-write level. Siri must reject levels above the room
ceiling unless the existing explicit override policy is satisfied.

#### `SetRoomPowerIntent`

Parameters: room and On/Off. On may run inline. Off requires explicit
confirmation and remains unavailable in the first Siri release if confirmation
behavior is not reliable while locked.

### App Shortcut discoverability

Expose two to five useful shortcuts, not remote buttons:

- Prepare content;
- Resume content;
- Set room volume;
- Turn on room.

App Shortcut trigger phrases historically constrain how many open-ended dynamic
parameters can appear directly in one phrase. Test the iOS 27 Siri/schema path
with both room and title. If the generic one-sentence intent does not resolve
reliably, create room-specific App Shortcuts for the five rooms so the spoken
room is fixed and the one dynamic entity is the media title. Five room shortcuts
remain within Apple's shortcut-count guidance and preserve the desired utterance.

Fallback behavior is conversational, not failure: if Siri cannot resolve the
title or room in one sentence, it asks for only the missing value, then performs
the same typed intent.

### Background behavior

The first intents should support background execution and not open the app for
ordinary commands. The app must be opened once for pairing, Local Network
permission, and room synchronization. If iOS requires foreground transition for
a permission or ambiguous action, use the current `supportedModes`/system
context APIs and explain the handoff rather than silently failing.

Siri responses should be exact:

- “Netflix search is ready for Avatar in Living Room. Select a result.”
- “Archer is playing in Living Room.”
- “Living Room Apple TV woke, but the physical TV power state is unverified.”
- “The Home Media hub is offline.”

Never say “playing” when the backend returned only search-ready or title-open.

## Stage 3: authenticated Mac mini control endpoint

MCP stdio is an agent interface, not an iPhone transport. Add a narrow,
versioned HTTP/JSON service around the same `ApplicationService` when the core
is stable.

Suggested endpoints:

```text
GET  /v1/rooms
GET  /v1/capabilities/{room_id}
POST /v1/actions/prepare-content
POST /v1/actions/volume
POST /v1/actions/power
GET  /v1/actions/{action_id}
```

No endpoint may accept shell commands, raw Python, arbitrary adapter methods,
or unrestricted remote-key batches.

Every mutation includes:

- stable client/device identity;
- idempotency key;
- resolved stable room ID;
- typed action and parameters;
- request timestamp/nonce;
- API schema version.

### Discovery and authentication

- Advertise the hub over Bonjour on the local network.
- First launch pairs the app to the hub using a short-lived code or QR payload.
- Store the resulting client credential in iOS Keychain.
- Store only a verifier/public key or hashed token on the hub.
- Use TLS with certificate/public-key pinning or a comparably authenticated
  local channel; do not trust “same Wi-Fi” as authentication.
- Rate-limit mutations and maintain a redacted audit trail.
- Never expose the service directly to the public internet.

For away-from-home use, use a private overlay such as Tailscale or another
explicitly reviewed secure tunnel. Codex Remote remains a separate manual path.

### Runtime

Run the hub as a supervised user LaunchAgent on the always-on Mac mini after an
explicit deployment gate. It reuses the singleton service and Apple TV
connections, reloads safe config deliberately, and reports health. Do not create
a competing poller or a second device-control core.

For operations longer than Siri's execution budget, return an action ID and
short initial dialog, then complete asynchronously. The iOS app can show a Live
Activity or notification later, but the primary search-ready path should target
completion within the current intent budget.

## Security and safety boundaries

- Siri/App Intents may access semantic operations only.
- Room resolution must be exact; aliases are synchronized and collisions fail.
- Search Ready may never select or start playback.
- Resume may start playback but must verify the requested title.
- Power Off and high-volume overrides require confirmation and may be excluded
  from Siri v1.
- No App Intent exposes app installation, purchases, accounts, service menus,
  resets, picture calibration, or arbitrary navigation.
- Phone retries reuse the same idempotency key.
- A timeout after sending a mutation returns unknown/degraded; it does not
  blindly repeat the action.
- Spoken dialogs never disclose private addresses, tokens, or provider history.

## Validation ladder

Apple recommends validating App Intents progressively. Follow this order:

1. Backend unit/contract tests and HTTP authentication/idempotency tests.
2. Shortcut-over-SSH execution using fakes.
3. One approved real Shortcut command to Search Ready on Living Room.
4. Xcode AppIntentsTesting for intents, entities, and queries out of process.
5. Shortcuts app inspection for parameter shape and dialogs.
6. Spotlight/entity resolution and synonyms.
7. Siri end-to-end on the user's iOS 27 beta while locked and unlocked.
8. Twenty non-destructive trials across room/title phrasing variants.

Acceptance targets:

- 20/20 commands resolve the intended room or ask for disambiguation; zero wrong
  rooms.
- Search Ready never selects or plays.
- Resume never reports success for the wrong title.
- Repeated Siri delivery with one idempotency key executes once.
- Median same-LAN Siri-to-query-ready latency <=7 seconds; p95 <=15 seconds.
- Offline hub and timeout errors produce a clear spoken result within the Siri
  interaction rather than hanging.

## Delivery order

1. Finish the provider-aware Apple TV core and its safety/lifecycle repair.
2. Preserve the Siri-compatible service/result contract during that work.
3. Build the restricted SSH wrapper and install the two-turn Shortcut.
4. Prove one live Siri -> Mac mini -> Living Room Search Ready command.
5. With explicit approval, install Xcode 27 and create the signed iOS app.
6. Implement App Entities, App Intents, and App Shortcuts.
7. Prove one-sentence Siri commands, then add volume and power.
8. Add secure away-from-home connectivity only if the user wants it.

This plan produces a working Siri route before requiring an App Store-quality
app, while keeping the final native experience independent of ChatGPT, Codex,
or any one model vendor.
