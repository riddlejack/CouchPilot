# Siri Shortcut delivery

## Fastest real path

The first usable Siri surface is the generated **Home Media** Shortcut:

1. “Siri, Home Media.”
2. Siri asks, “What should I do, and in which room?”
3. Say the whole request, for example, “Resume Breaking Bad on Netflix in Living Room.”
4. Siri speaks only the broker's verified result.

This is intentionally a two-turn prototype. A raw Shortcut cannot declare a
parameterized Siri phrase. The one-sentence form—“Ask Home Media to resume
Breaking Bad in Living Room”—comes from the native App Intent in
`ios/HomeMediaIntents/`, after a minimal iOS app is built and installed.

## Current household rollout status (2026-07-21)

- The Mac mini broker is reachable on the LAN and reports `ready` with a warm,
  persistent Living Room observer.
- The broker token exists outside the repository with mode `600`.
- The development Mac has Cherri `v2.3.0`, and the unsigned Shortcut has been
  compiled and validated at `dist/HomeMedia-unsigned.shortcut`.
- The unsigned artifact was copied to the Mac mini and signed there. The signed
  artifact exists at `~/Developer/home-media-control/dist/HomeMedia.shortcut` on
  the Mac mini.
- Both Macs currently have Command Line Tools rather than full Xcode. That does
  not block the Shortcut, but it blocks building and installing the native iOS
  app.
- `Home Media` is not yet present in either Mac's Shortcuts library. The Mac
  mini and development Mac show different shortcut counts, so do not assume
  they are syncing the same library; verify the Apple Account and iCloud Sync
  before choosing the import machine.
- The import question has not been answered, the broker token has not been
  stored in a Shortcut, and iCloud/iPhone synchronization has not been proven.

No Apple TV pairing is required for this step. The iPhone Shortcut talks to the
already-running Mac mini broker, and the broker owns the paired Apple TV link.

## Exact installation checklist

### 1. Compile on the development Mac

```zsh
brew install electrikmilk/cherri/cherri
scripts/build_siri_shortcut.sh --check \
  --broker-url "${HOME_MEDIA_BROKER_URL:-http://home-media.local:8744/v1/intent}" \
  --output "dist/HomeMedia-unsigned.shortcut"
```

This gate has passed once. Re-running it is safe: the build must report Cherri
`v2.3.0`, `plutil` validation must pass, and the artifact must be non-empty.
This command does not sign or import anything.

### 2. Sign as the Mac mini user

Copy the unsigned artifact to the Mac mini without putting a credential in the
command:

```zsh
export HOME_MEDIA_SSH_TARGET='your-user@home-media.local'
ssh "$HOME_MEDIA_SSH_TARGET" \
  'install -d "$HOME/Developer/home-media-control/dist"'
scp "dist/HomeMedia-unsigned.shortcut" \
  "${HOME_MEDIA_SSH_TARGET}:Developer/home-media-control/dist/HomeMedia-unsigned.shortcut"
```

Then sign it using Apple's built-in Shortcuts tool on the Mac mini. This gate
has passed once and produced the signed artifact described above; the command is
included for reproducible rebuilds:

```zsh
ssh "$HOME_MEDIA_SSH_TARGET" \
  '/usr/bin/shortcuts sign --mode people-who-know-me \
    --input "$HOME/Developer/home-media-control/dist/HomeMedia-unsigned.shortcut" \
    --output "$HOME/Developer/home-media-control/dist/HomeMedia.shortcut"'
```

Signing sends a copy to Apple for validation. If the resulting file cannot be
imported on the owner's iPhone, repeat only the signing command with
`--mode anyone`; do not rebuild the action graph.

### 3. Import privately on the Mac mini

1. In Shortcuts > Settings > General on the Mac mini, enable iCloud Sync and
   confirm it uses the same Apple Account as the iPhone.
2. Run `scripts/copy_siri_shortcut_connection.sh` on the Mac mini. It copies the
   full endpoint and bearer token to that Mac's clipboard without printing them.
3. Double-click `dist/HomeMedia.shortcut`, choose **Add Shortcut**, and paste
   the clipboard once into **Paste the Home Media connection JSON**.
4. Immediately clear the Mac mini clipboard with `pbcopy </dev/null`.
5. Confirm `/usr/bin/shortcuts list` contains exactly one `Home Media` entry.

If the Mac mini is not on the same synced Shortcuts library as the iPhone, copy
the signed artifact back before importing it on the development Mac:

```zsh
scp "${HOME_MEDIA_SSH_TARGET}:Developer/home-media-control/dist/HomeMedia.shortcut" \
  "dist/HomeMedia.shortcut"
open "dist/HomeMedia.shortcut"
```

Alternatively, sign the already-validated local unsigned artifact on the
development Mac:

```zsh
/usr/bin/shortcuts sign --mode people-who-know-me \
  --input "$PWD/dist/HomeMedia-unsigned.shortcut" \
  --output "$PWD/dist/HomeMedia.shortcut"
open "dist/HomeMedia.shortcut"
```

In either fallback, move the connection JSON directly into the import Mac's
clipboard without displaying it, paste it once, and clear the clipboard. Do not
save it in iCloud Drive, a note, a shell argument, or a screen recording.

### 4. Prove the transport before Siri

1. Confirm `http://home-media.local:8744/healthz` (or the configured bridge URL) reports `ready` from the
   import Mac.
2. Run `Home Media` once from the Shortcuts app while the Mac mini is awake.
3. Give the safe request `Turn on Living Room` and approve the local-network prompt
   if macOS presents one.
4. Confirm the Shortcut returns the broker's spoken response and that only one
   broker request was recorded.

Do not debug TV navigation from inside Shortcuts. A failed direct authenticated
broker request is a backend gate; a successful direct request plus a failed
Shortcut is a transport or permission gate.

### 5. Sync and prove the iPhone

1. On the iPhone, open Settings > Shortcuts and enable iCloud Sync. Also
   confirm iCloud Drive is enabled for Shortcuts.
2. Wait for `Home Media` to appear, then open it once while unlocked.
3. Allow Local Network access when asked. In Settings > Privacy & Security >
   Local Network, confirm Shortcuts remains enabled.
4. Run the tile once with `Turn on Living Room`.
5. Say `Siri, Home Media`, answer the follow-up with `Search Netflix for Avatar
   in Living Room`, and verify Siri speaks the broker's exact result.
6. Repeat while the phone is locked. Do not immediately repeat a timeout: the
   mutation may still have completed.

Passing every step above completes the usable two-turn Siri delivery. At the
current checkpoint, the signed artifact exists but import, token configuration,
iCloud synchronization, and unlocked/locked iPhone Siri proof remain pending.
The one-sentence App Intent delivery is also pending.

### 6. One-sentence App Intent follow-on

The current Swift sources type-check with the installed Swift 6.3.2 macOS SDK,
but they are not an app. Before “Ask Home Media to resume Archer in Living Room”
can be installed, create and prove all of the following:

1. Full Xcode 27 with the iOS 27 SDK on a development Mac.
2. A signed iOS app target with a unique bundle identifier and the two files in
   `ios/HomeMediaIntents/`.
3. A minimal SwiftUI app entry point and one-time setup screen that stores the
   broker origin in `homeMediaBrokerURL` and the bearer token in Keychain.
4. `NSLocalNetworkUsageDescription` and `NSAllowsLocalNetworking` as documented
   in `ios/HomeMediaIntents/README.md`.
5. On-device installation, first-unlock Keychain setup, and Local Network
   permission.
6. Siri tests for the parameterized phrase while unlocked and locked, including
   proof that a timeout creates no second POST.

The guaranteed first native phrase includes the app name: `Ask Home Media to
<request>`. An app-name-free sentence such as `Resume Archer in Living Room` is an
iOS 27 Siri semantic-resolution gate, not something the current Shortcut can
promise. Test it on the user's exact beta before advertising it; if resolution
is inconsistent, add room-specific App Shortcuts so only the title remains
dynamic.

## Build without secrets

The source is `shortcuts/Home Media.cherri`. It contains one import question,
not a broker credential. The answer is a compact JSON object containing:

- the full endpoint, normally
  `http://home-media.local:8744/v1/intent` or the value passed through
  `--broker-url`/`HOME_MEDIA_BROKER_URL`;
- the private bearer token from the Mac mini.

Install the pinned compiler if needed and run a local, unsigned validation build:

```zsh
brew install electrikmilk/cherri/cherri
scripts/build_siri_shortcut.sh --check
```

The build requires Cherri v2.3.0 and uses deterministic action UUIDs. The
generated file lands at `dist/Home Media.shortcut`. Keep generated artifacts
out of commits.

If signing and importing on the same Mac, create the importable artifact locally:

```zsh
scripts/build_siri_shortcut.sh --sign
open "dist/Home Media.shortcut"
```

The household rollout instead signed the validated artifact on the Mac mini as
shown in the exact checklist above. Either route uses Apple's Shortcuts signing
service in “people who know me” mode; as with exporting from the Shortcuts app,
Apple receives a copy for validation. During import, paste the one-line
connection object when prompted:

```json
{"url":"http://home-media.local:8744/v1/intent","token":"PRIVATE_TOKEN_FROM_MAC_MINI"}
```

On the Mac mini, copy that object without printing the token or placing it in
shell history:

```zsh
scripts/copy_siri_shortcut_connection.sh
```

Paste once into the import prompt, then clear the clipboard with
`pbcopy </dev/null` after import.

Do not add the real object, endpoint, or token to the Cherri source or Git.

This prototype stores the random broker token inside the imported, iCloud-synced
Shortcut. Someone who can unlock the phone and edit the Shortcut can read it.
Treat it as a revocable household API credential—never reuse the Mac login
password or any personal credential. The native iOS client moves this token to
Keychain.

## iCloud and iPhone gate

On the Mac that owns the same Apple Account as the iPhone:

1. In Shortcuts > Settings > General, confirm **iCloud Sync** is enabled.
2. Import the signed file and answer its connection-JSON setup prompt.
3. Run **Home Media** once on the Mac. Approve the first network/privacy prompt
   and confirm the broker returns a spoken response.
4. Wait until **Home Media** appears in Shortcuts on the iPhone.
5. Run it once while the iPhone is unlocked and allow local-network access if
   asked.
6. Test “Siri, Home Media” unlocked, then locked.

The Shortcut generates a fresh timestamp-plus-random idempotency key for every
invocation and never retries. If Siri reports a timeout, do not immediately
repeat the request: the TV mutation may have completed and the broker will fail
closed for the original key.

## Supported request examples

```text
Turn on Living Room
Resume Breaking Bad on Netflix in Living Room
Pull up Avatar on Netflix in Living Room
Search Netflix for Avatar in Living Room
Set the volume to 22 in Living Room
```

Do not test the Siri path until `GET /healthz` on the Mac mini says `ready` and
the same machine can complete a direct authenticated `POST /v1/intent` smoke
test. The Shortcut is only a transport; it cannot repair an unhealthy observer,
unpaired Apple TV, or failed live content gate.

## Why this is generated instead of scripted through the UI

On macOS 26.5, Apple's `shortcuts` command supports run, list, view, and sign,
but not create or import. Apple officially supports importing a signed
`.shortcut` file and syncing it through iCloud. Keeping the workflow in Cherri
makes the action graph reproducible and leaves only the final Apple import
confirmation to the UI.
