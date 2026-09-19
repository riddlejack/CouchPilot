# Apple TV agent package delivery

Started 2026-09-19. Current scope: the Apple TV agent only. The earlier room/scene,
Sony/Sonos, and Siri stacks are archived in Git history (see [HISTORY.md](HISTORY.md)).
Do not label a mocked capability live verified.

## Product contract

An existing Codex, Claude Code, or other MCP agent controls the user's own Apple
TV using plain-English intent. The local bridge owns device connections and
returns compact fresh state; it does not require a separate LLM/API subscription.
Direct control remains useful without the optional developer screen helper.

The desired everyday experience is: install the bridge, choose and pair a device,
add the MCP integration, then ask for content, audio, or Settings changes. Setup
must identify prerequisites accurately and diagnose missing/expired access.

## Current implementation tranche

- [x] Typed persistent WDA client; bounded observations; standalone screenshot
  fallback; ambiguous mutation results never trigger an automatic repeat.
- [x] One local owner per physical device, pinned identity checks, consumed
  observation handles, freshness enforcement, and orderly cleanup.
- [x] Six-tool general MCP interface: devices, observe, act, status, apps, control.
  Text-first observations and optional images; no nested model invocation.
- [x] Setup/doctor CLI, private configuration, paired-control discovery, and
  managed lifecycle for an existing signed helper.
- [x] Reusable agent skill and Codex/Claude configuration; installation tested
  from a built wheel in a clean environment.
- [x] Live checks across Settings, Home, and Netflix; text entry/search; reconnect
  after helper restart; a concrete content preparation workflow where available.
- [x] Current README, compatibility/capability evidence, license and CI.

Live continuation: the actual stdio MCP bridge selected the preferred Netflix
profile, entered and read back Breaking Bad in search, opened its S1:E1 control,
started playback, and paused at 00:40 (Play control and elapsed time visible).
Companion and AirPlay pairing completed; installed apps were returned through
paired control. A native helper run without xcodebuild/Appium/sudo is verified;
managed cold-start, shutdown, restart, and sleep/wake have passed. Protected playback
pixels are black, so playback metadata and overlay controls remain essential.
VLC's free existing-account redownload and launch also passed. Match Dynamic
Range was read, changed, read again, and restored. See
[the agent acceptance record](../research/AGENT_ACCEPTANCE_2026-09-19.md) for
failures, timings, and remaining full-product release criteria.

## Access decisions to resolve

Appium is already open source. Apple's real-device signing policy is a separate
constraint. A preinstalled signed helper may remove Xcode from everyday runtime;
it does not establish perpetual free signing. Explore that lightweight runtime
before committing to a large installer. Keep the previously proven QuickTime
capture route as a candidate independent of installed helper expiration.

Separate the agent host from the local device bridge. A Windows/Linux/cloud agent
can potentially use a reachable authenticated bridge; a cloud VM does not share
the user's LAN or Bonjour discovery by default. Do not expose WDA publicly.

## Full-vision acceptance

1. Power/app launch/keyboard/playback through persistent paired control.
2. Settings navigation plus reading values, with verified changes and rollback
   for disruptive picture/audio changes.
3. Already-paired headphone connection verified; new pairing has an explicit
   physical accessory pairing-mode step where required.
4. Exact title/season/episode preparation and playback verification. Provider
   availability is region-dependent; subscriptions are user-provided preferences.
5. App Store discovery/install/open with account/purchase prompts represented
   honestly. This task authorizes no spending.
6. Recovery from sleep, disconnects, app transitions, unexpected screens, and
   expired access without falsely claiming success or blindly replaying inputs.
7. Fresh-user installation and sustained operation on documented hardware/OS.

The overall project remains incomplete until these are demonstrated or explicitly
scoped with their true prerequisites. Passing tests, documentation, or a successful
helper build alone does not close an end-to-end acceptance item.
