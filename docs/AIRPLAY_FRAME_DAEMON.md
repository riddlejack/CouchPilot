# Persistent Apple TV frame stream

`airplay-frame-daemon` is the low-latency visual input for Apple TV computer use.
It is a logged-in macOS LaunchAgent that opens one retained AVFoundation session
per configured Apple TV and publishes its newest frame to a private local store.
The Python runtime reads that store in milliseconds instead of opening a fresh DVT
channel for every observation.

The daemon is additive. If a stream is cold, disconnected, stale, or temporarily
unavailable, Python uses the existing warm DVT screenshot worker for the **same
confirmed UDID**. A room, stable-device, CMIO-identity, protocol, file-integrity,
or permissions mismatch stops; it never falls back to another discovered TV.

## Build and code-only gate

The package has two products:

- `AirPlayFrameCore`: strict private-config and atomic frame-store contracts;
- `airplay-frame-daemon`: CoreMediaIO discovery plus retained AVFoundation sessions.

Build without opening a capture source:

```bash
cd native/AirPlayFrameDaemon
swift build -c release
.build/release/airplay-frame-daemon \
  --config ~/.config/home-media/airplay-capture.json \
  --validate-config
```

`swift build` does not enumerate, connect to, or mutate an Apple TV.

## One-time exact binding

Discovery is read-only, but it reveals private device identities and can prompt for
macOS capture permission. Run it only on the Mac mini and do not paste its output in
an issue or commit:

```bash
.build/release/airplay-frame-daemon --discover --seconds 8
```

Copy `config/airplay-capture.example.json` to
`~/.config/home-media/airplay-capture.json`, mode `0600`. Bind each `room_key` and
existing Home Media `stable_device_id` to the exact `unique_id` discovered for that
TV. The optional localized name is a second equality check, never the lookup key.
Duplicate room, stable-device, or CMIO identities are rejected.

## Stable install and one-time UI gate

Use a stable executable path and signing identity so macOS can retain its grant:

```bash
install -d -m 700 "$HOME/Library/Application Support/Home Media/bin"
install -m 700 .build/release/airplay-frame-daemon \
  "$HOME/Library/Application Support/Home Media/bin/airplay-frame-daemon"
codesign --force --sign - --identifier com.home-media.airplay-frame-daemon \
  "$HOME/Library/Application Support/Home Media/bin/airplay-frame-daemon"
```

Before loading launchd, run the installed binary once from the logged-in Mac mini
desktop. macOS may ask for **Screen & System Audio Recording** access. The selected
Apple TV may show a one-time AirPlay code. Approve only the named room. Stop the
foreground process after a fresh `state.json` reports `status: ready`. Those are the
only expected human gates; subsequent starts should reuse the OS grants.

FairPlay-protected video may yield a black frame. Menus, search, title details, and
playback controls remain the intended observation surface; pyatv playback metadata
provides the separate resume/pause proof.

## LaunchAgent and broker integration

Copy `deploy/com.home-media.airplay-frame-daemon.plist.example` to
`~/Library/LaunchAgents/com.home-media.airplay-frame-daemon.plist`, replace `YOU`,
and validate it with `plutil -lint`. Then bootstrap it in the logged-in GUI domain.
The broker template already points Python at the same private config and state store:

```text
HOME_MEDIA_AIRPLAY_CAPTURE_CONFIG=~/.config/home-media/airplay-capture.json
HOME_MEDIA_FRAME_STREAM_DIR=~/Library/Application Support/Home Media/frames
HOME_MEDIA_FRAME_STREAM_MAX_AGE_S=3
```

launchd does not expand `~`; real plists must use absolute paths. The store and room
directories are `0700`; config, state, and PNG files are `0600`. Raw CMIO identities
appear only in the private config. Health and public screenshot metadata expose only
a short hash and backend state.

## Published protocol

Each room owns `ROOM/state.json` and one immutable `frame-SEQUENCE.png`. State is
written after its frame and contains protocol version 1, exact room/stable-device,
CMIO identity fingerprint, sequence, UTC timestamp, frame filename, SHA-256, pixel
dimensions, status, and a bounded error code. The client rereads state after the PNG;
a concurrent update, stale frame, corrupt hash, or malformed file triggers exact DVT
fallback, while identity/config mismatches are hard safety stops.
