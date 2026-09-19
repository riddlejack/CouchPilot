# Siri broker runbook

## Delivered boundary

`home-media-broker` is a resident HTTP bridge between an iPhone Shortcut/App Intent and the
long-lived Mac mini service. Its public mutation surface is intentionally small:

- prepare one title in one configured room (`search_ready`, `title_open`, or `resume`);
- turn on one configured room;
- set one configured room's absolute volume.

The endpoint cannot represent shell commands, arbitrary URLs, remote buttons, button batches,
power off, relative volume, or adapter method names. Every request has a durable idempotency key.
A process crash leaves the key pending, so a retry fails closed instead of repeating a possibly
completed TV action.

Common sentences are parsed without an LLM:

```text
Turn on Living Room and pull up Breaking Bad on Netflix
Resume where I'm at in Archer on Hulu in Living Room
Search Netflix for Avatar in Living Room
Set the volume to 22 in Living Room
Wake up the Theater TV
```

If `--codex-fallback` is enabled, only sentences the deterministic parser cannot understand are
sent to an ephemeral parse-only Codex process. The invocation pins all three runtime choices:

```text
model = gpt-5.6-sol
model_reasoning_effort = low
service_tier = fast
```

`fast` is the Fast wrapper, not `priority`. The parse process has a read-only sandbox, is told not
to use tools, returns a strict JSON schema, and still cannot bypass the room/action/provider
allowlists. It never drives the TV. Novel-screen reasoning belongs behind the separate visual
controller boundary.

## Mac mini setup

Do this only after the private `homes.yaml`, Apple TV credentials, observer binding, and optional
pymobiledevice3 environment have been installed on the Mac mini.

Create a private bearer token outside the repository:

```zsh
install -d -m 700 "$HOME/.config/home-media"
umask 077
openssl rand -hex 32 > "$HOME/.config/home-media/broker-token"
chmod 600 "$HOME/.config/home-media/broker-token"
```

The daemon refuses tokens shorter than 32 bytes and rejects token files with group/world
permissions. Never put the token in Git, a plist, a command-line argument, or a screenshot.

For a foreground smoke test bound only to the Mac mini itself:

```zsh
HOME_MEDIA_PREWARM_ROOMS=living_room \
  .venv/bin/home-media-broker \
  --host 127.0.0.1 \
  --config "$HOME/.config/home-media/homes.yaml" \
  --codex-fallback
```

`GET /healthz` is non-mutating and contains no identifiers. It reports:

- `warming` while the configured observer worker starts;
- `ready` after every configured prewarm room produced a usable frame;
- `degraded` if prewarm failed or returned no frame.

The listener starts before prewarm finishes. A failed warmup never kills the daemon; the next
request can safely reconnect. The deployment template sets `HOME_MEDIA_PREWARM_ROOMS=living_room`,
which pays the slow tunnel/DVT startup at login instead of inside the first Siri command.

The template also sets `HOME_MEDIA_CODEX_BIN` to the ChatGPT app's bundled Codex executable.
Keep this as an absolute path: launchd does not inherit the interactive shell's PATH, so a
bare `codex` command can work in Terminal yet fail when Siri reaches the visual controller.

Copy `deploy/com.home-media.broker.plist.example` to a private, machine-local plist, replace every
`/Users/YOU` path, validate it with `plutil -lint`, then install it as a user LaunchAgent. The
template explicitly binds to `0.0.0.0` for same-Wi-Fi iPhone access; the program itself defaults to
`127.0.0.1` so an accidental foreground launch is not LAN-visible.

For this household prototype, plain HTTP is acceptable only on the trusted home LAN. The bearer
token prevents unauthenticated calls but does not encrypt Wi-Fi traffic. The durable version should
use a trusted HTTPS name (for example a Tailscale HTTPS endpoint) before use on shared/untrusted
networks. Do not expose port 8744 through the router.

## iPhone Shortcut: usable before an app exists

Use the generated **Home Media** Shortcut instead of assembling the action graph by hand. Its
reviewable source, pinned build script, one-prompt private setup, iCloud gate, and exact deployment
commands are in `docs/SIRI_SHORTCUT_DELIVERY.md`.

The generated workflow contains these actions:

1. **Ask for Input**: “What should I do, and in which room?” (Text).
2. **Current Date**, **Format Date**, and **Random Number** for a fresh request identifier.
3. **Dictionary** with:
   - `schema_version`: Number `1`
   - `utterance`: Provided Input
   - `idempotency_key`: `ios-` followed by the timestamp and random nonce
4. **Get Contents of URL**:
   - URL: `http://<mac-mini-local-name-or-static-IP>:8744/v1/intent`
   - Method: POST
   - Request Body: JSON using the Dictionary
   - Header `Authorization`: `Bearer <private broker token>`
   - Header `Content-Type`: `application/json`
5. **Get Dictionary Value** `spoken_response` from Contents of URL.
6. **Stop and Output** that value with the no-output behavior set to **Respond**.

Invoke it with “Siri, Home Media.” This is a two-turn prototype: Siri asks for the request and then
speaks the verified backend result. The Shortcut must not automatically repeat after a timeout;
the first request may have completed.

## Native one-sentence Siri follow-on

`ios/HomeMediaIntents/` contains the first native integration module:

- one background `ControlHomeMediaIntent` with one natural-language parameter;
- one `AppShortcutsProvider` with a parameterized one-sentence phrase and a conversational phrase;
- an ephemeral `URLSession` client that creates a unique idempotency key and never retries;
- Keychain-only bearer-token storage.

This follows Apple's small-intent guidance: the App Intent is a thin system surface and all room
resolution/business logic stays in the Mac mini service. Add the two files to a minimal iOS 27 app,
add `NSLocalNetworkUsageDescription`, implement a one-time broker URL/token setup screen, then run
the required gates:

1. build with the installed Xcode 27/iOS 27 SDK;
2. launch once and save the broker token in Keychain;
3. verify the Mac mini appears under Local Network privacy;
4. test the parameterized phrase while the phone is unlocked, then locked;
5. verify Siri says only the exact `spoken_response` returned by the hub;
6. verify an iOS timeout produces no automatic second POST.

Do not claim the Swift artifact is shipped before those gates pass. App Intents behavior is tied to
the installed beta SDK and must be compiled/on-device tested rather than inferred from source.

## Current outcome honesty

The broker and controller now implement `title_open` and Netflix `resume` as separate verified
outcomes. Resume selects only an exact visible title on a fresh frame, observes playback, sends
an explicit idempotent **Pause** command, and requires fresh stopped-state receipts before Siri says
the title is ready and paused. The Living Room broker path passed live on 2026-07-21: it returned
`playback_paused_verified` in 35.3 seconds, and an independent status read five seconds later still
reported `idle`. Search-ready continues to stop without selecting anything.

The latency budget is measured from Shortcut POST to response. Known utterances avoid a model call.
The original target of median <=7 seconds and p95 <=15 seconds has **not** been achieved or
established statistically; the verified resume run took 35.3 seconds. Record parser, prewarm state,
controller stages, and total latency for each live acceptance run without logging the bearer token
or raw household identifiers.

## Apple references

- [Making actions and content discoverable and widely available](https://developer.apple.com/documentation/appintents/making-actions-and-content-discoverable-and-widely-available)
- [Adding parameters to an app intent](https://developer.apple.com/documentation/appintents/adding-parameters-to-an-app-intent)
- [Get to know App Intents (WWDC25)](https://developer.apple.com/videos/play/wwdc2025/244/)
- [AppShortcut](https://developer.apple.com/documentation/appintents/appshortcut)
