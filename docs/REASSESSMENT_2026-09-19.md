# Apple TV agent control: independent reassessment

> Historical record: some experimental components mentioned here are now archived. See [the scope change](HISTORY.md) for the current package and preserved source.

Date: 2026-09-19. Original scope: first-principles design, current-source research, local code review, and offline validation. Existing working-tree changes were preserved. Subsequent authorized hardware tests on the user's nearby Media Room Apple TV are recorded in [the live spike report](../research/MEDIA_ROOM_2026-09-19.md); they supersede the original untested observation status below.

**Live update:** WDA now demonstrably controls the unmodified tvOS 26.6 device, reads Settings labels/values/focus, selects elements by label, captures full-resolution screenshots in roughly 0.3 seconds in a short sample, and delivers roughly 9–10 MJPEG frames/second with verified changes during navigation. Netflix's profile chooser is observable visually and semantically. Signing renewal and transition/idle handling remain product work; this is a physical proof, not a finished agent product.

## Decision

Preserve the working control infrastructure. Change the product's organizing principle to **general Apple TV agent control**, with computer use for discovery/recovery and verified shortcuts for familiar tasks. The strongest new implementation candidate is **pyatv plus Appium/XCUITest/WebDriverAgent**. Prove its observation and semantic UI capabilities before extending workflow automation. Do not start over by rewriting Apple's remote protocols.

The vision is a credible engineering objective: an agent sees the same interface a person sees, has a remote and direct text entry, and can verify its own work. Universal unattended operation on every tvOS version, streaming service, and home-theater configuration is not established. Compatibility should be discovered and reported, rather than hidden behind either a universal promise or an excessively narrow MVP.

The July work was partly on track and already recognized the observation bottleneck. The decisive unresolved issue was proving an end-to-end visual channel. More model intelligence alone would not repair missing frames or incorrect freshness semantics.

## The strongest version of the product

An open-source, local controller gives the user's existing agent eyes, a remote, and reliable receipts. A Codex/Claude plugin or skill teaches the agent how to use that controller. The controller does not need to contain another mandatory language model.

The user can ask for outcomes such as:

- "Get Breaking Bad season 2 episode 3 ready on a service I already pay for, with my headphones connected." Find the correct work/episode, current regional offers, user's chosen subscriptions, and actual app/account state; open the best available route; verify the episode and audio destination.
- "The projector looks washed out; investigate and fix the configuration." Inspect current tvOS settings, identify the display/receiver chain, make a reversible experiment, compare its result, and retain a way back.
- "Install this free app." Navigate the App Store and verify installation; hand over only for an actual authentication or purchase decision.
- "Put everything back the way it was." Restore recorded, readable settings where the equipment permits it; identify any values that could not be observed or restored.

The contract is outcome verification with minimal human relay. It does not require every operation to have a private API, nor every action to pass through an LLM.

## First-principles architecture

```text
Codex / Claude / another agent
          | MCP tools + short usage skill
          v
One resident local device-session service
  - exact device identity and exclusive action ownership
  - direct commands and keyboard input through pyatv
  - current frame + independently timestamped device metadata
  - action receipts and bounded recovery
          |
          +--> pluggable visual source
          +--> optional verified workflow shortcuts
          +--> content discovery / subscription preferences
          +--> projector, receiver, TV, or audio adapters
```

### 1. Keep control, observation, and reasoning separate

Use direct commands for supported power, app launch, transport, text, and audio functions. Use visible navigation for Settings, Bluetooth, App Store, provider UI, and unfamiliar dialogs. A direct command's acceptance is not evidence that the requested end state happened.

Return images to the agent that the user is already talking to. An optional standalone voice service can bring its own explicitly selected model later. This avoids mandatory secondary model calls, duplicated conversation state, and a model choice hidden in the backend.

### 2. Continuous capture does not mean continuous LLM inference

Keep the visual source connected and drain it continuously. Maintain the latest image in memory. Encode a suitably sized image when an agent requests it; preserve full resolution or a crop when small text needs inspection. Do not encode and write every 4K frame as PNG merely to read it back into Python.

Let ordinary navigation run as short, verified transitions. Invoke model reasoning when a decision or unfamiliar screen requires it. A healthy video stream prevents capture startup delays; it cannot eliminate inference latency. Measure those separately. Continuous capture is one implementation option, not a prerequisite if fresh accessibility state or sufficiently fast on-demand images provide the needed feedback.

For each observation retain device ID, stream session, source frame ID/presentation timing where available, host receive/decode time on a consistent monotonic clock, capture backend, dimensions, and frame status. Keep metadata timestamps separate from image timestamps. If source timing is unavailable, label host timing as a proxy and measure buffering rather than asserting source freshness.

A post-action observation must be from the same device, have an appropriate action barrier, and satisfy the expected visible or semantic postcondition. A frame decoded after dispatch can still contain buffered pre-action content. An unchanged image can also be a valid fresh frame from a static screen. Neither pixel difference nor an incremented counter proves causality.

Revalidate the relevant focus/context before Select if the model spent time reasoning or another person might have used the remote. Session restart after a possibly delivered Select is ambiguous: observe and reconcile instead of blindly repeating it.

### 3. Make the generic tool surface genuinely sufficient

The conceptual interface is small: list/connect devices, observe, act, read status, and disconnect. `act` should support directional/Back/Home input, appropriate press types, direct text replacement/readback, explicit Play/Pause, app/deep-link launch, and bounded transitions. Expose capability differences instead of silently downgrading.

Keep exactly one action owner per physical device across CLI, MCP, and optional voice clients. In-process locks alone cannot coordinate independently launched servers. Route those clients through one resident service or use an explicit cross-process device lease. A local socket is sufficient for the initial Mac-only design; a public listener is unnecessary.

### 4. Treat content discovery as a different problem from remote control

Resolve title identity, season/episode, country, current offers, offer type, and preferred subscriptions before deciding which app to launch. Installed app does not mean subscribed; subscribed does not prove that this season is included. A URL accepted by tvOS does not prove that the correct episode opened.

Start with user-declared subscriptions and region. Use a replaceable availability adapter and verify through the provider UI when needed. Prefer tested deep links, fall back to direct text search, and inspect the result. Do not encode eternal assertions about which services participate in Apple's search.

### 5. Troubleshoot the whole signal path

Apple TV output format, projector picture mode, receiver input, and headphone pairing are different controls on different devices. A screenshot of Apple's internal framebuffer can look correct while the HDMI chain or projector is wrong.

Use projector/receiver telemetry where available. For physical output, a temporary camera view of the monitor/projector is a useful observation adapter: it sees blackouts, input overlays, and whether a picture is actually present. It is not a calibrated colorimeter. HDMI capture is an alternative for compatible unprotected output, but capture hardware can alter EDID/HDR negotiation and HDCP can block it; do not assume it transparently reproduces the original theater setup.

New Bluetooth pairing can require physically putting the accessory in pairing mode. Reconnecting an already-paired device should be a separate, lower-friction workflow. Apple documents that physical pairing step in its [Bluetooth instructions](https://support.apple.com/en-us/102227).

## Observation routes and experiment order

First do the inexpensive Device Hub/QuickTime visibility check because the software is already present. The main new technical spike should be Appium/WDA, especially semantic observation. These are experiments with stopping conditions, not a commitment to implement five competing backends.

| Route | Why test it | What remains unproved |
|---|---|---|
| Appium XCUITest + WebDriverAgent | Documented physical tvOS support, focused elements, and semantic element selection; established UI-testing infrastructure may avoid our private accessibility reverse engineering. | Signing/provisioning friction, exact target/OS support, Settings and third-party element coverage, stream speed, and persistent-session reliability. |
| Device Hub on this Mac | Already installed; Codex Computer Use can inspect its desktop interface. Potentially avoids building a viewer. | Physical Apple TV compatibility, Settings and third-party UI visibility, actual frame age, and sleep/reconnect behavior. |
| QuickTime wireless Apple TV capture, then a native AVFoundation/CoreMediaIO adapter | Establish Apple's working viewer first, then reproduce its capture path. Reuse existing native work if the source works. | Current target/OS discovery, pairing UX, session start, and sustained frames. Earlier enumeration was not a successful stream. |
| Newer CoreDevice/idevice/pymobiledevice3 display support | Could provide a cleaner continuous stream without a visible Mac window. | Exact tvOS service support. iPhone success does not establish Apple TV success. |
| Camera observing the nearby screen | Lets development continue even if Apple's digital capture path fails; observes real output. | Camera position, legibility, and practical latency. Less convenient as a default installation. |
| Existing DVT screenshot worker | Existing integration and historical capture proof; useful diagnostic compatibility path. | It cannot be assumed fast enough for an interactive primary loop. |

Do not spend another large implementation cycle on a daemon before obtaining and measuring real frames through its underlying route.

[Appium's tvOS guide](https://appium.github.io/appium-xcuitest-driver/latest/guides/tvos/) explicitly covers real wireless devices, remote buttons, focused elements, and element selection that computes remote navigation. Modern wireless setup requires provisioning, pairing, and a RemoteXPC tunnel. This is a no-jailbreak development-tool route, with more setup friction than an ordinary consumer app. No Appium/WebDriverAgent/XCUITest references were found in the inspected repository source, docs, or research.

The driver also offers an [MJPEG screenshot stream](https://appium.github.io/appium-xcuitest-driver/latest/guides/mjpeg/); achievable rate and tvOS behavior must be measured. Its [capability documentation](https://appium.github.io/appium-xcuitest-driver/latest/reference/capabilities/) distinguishes newer RemoteXPC requirements, including disabled devicectl launch fallback on OS 27+. A persistent test session and narrow focused-element queries are worth testing before repeatedly requesting a full UI tree.

[TV Labs](https://docs.tvlabs.ai/platform/platforms/apple) documents XCUITest automation against `com.apple.TVSettings`, which strengthens the case that this can reach relevant system UI. That does not establish access to every third-party app element or prove our home device works. Developer signing lifetime, restart behavior, and distribution/onboarding remain product acceptance criteria.

Apple's [Device Hub overview](https://developer.apple.com/documentation/xcode/device-hub) describes physical and simulated device interaction. Its [detailed interaction page](https://developer.apple.com/documentation/xcode/interacting-with-your-app-in-device-hub) specifically describes the tvOS controls in simulator terms. Therefore those documents support an experiment, not a claim of proven physical tvOS coverage. The July handoff had already proposed Device Hub; this is not a newly discovered solution.

A tempting new route was checked and narrowed: [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) is now present in tvOS 27. However, the installed tvOS SDK explicitly marks `SCShareableContentStyleDisplay` unavailable on tvOS, and the [display-style documentation](https://developer.apple.com/documentation/screencapturekit/scshareablecontentstyle/display) omits tvOS. The generic `present()` picker is also unavailable there, while current-application capture is available. A companion app cannot be assumed to gain supported full-system capture just because the framework exists. Do not make this the main implementation path without new evidence.

## What the repository actually contains

Current branch: `codex/apple-tv-computer-use`. Five visible commits dated July 20, 2026; substantial pre-existing changes in 35 tracked files. This review did not overwrite or commit those changes.

### Preserve

- `adapters/apple_tv.py` and `connection.py`: pyatv integration, discovery, pairing, persistent connections, keyboard input/readback, app launch, and status.
- Registry/configuration, stable device bindings, explicit capability reporting, and secret-storage boundaries.
- Shared service, typed models, CLI, MCP image responses, audit infrastructure, and partial-result semantics.
- Exact-room screenshot binding and the proven DVT fallback, as compatibility components.
- The useful Netflix transition knowledge and tests, as optional accelerators rather than the system's definition of intelligence.

### Replace or simplify

- Observation freshness and post-action receipts: correct producer timing must survive every layer.
- Provider-centric policy in the generic observation path. `observers/service.py:94` defaults to `VisionNetflixClassifier`, even for general observation.
- The agent interaction surface. `mcp_server.py:565` exposes a key-only computer-use action, although the controller supports text, app launch, wake, and transport. Generic text is currently behind the raw-remote debug flag.
- Mandatory nested model policy for unfamiliar screens. `vision_policy.py:204` starts a separate ephemeral Codex process, selects `gpt-5.6-sol`, and forces low reasoning and the fast tier. That is a poor default for an agent-neutral interactive skill.
- Full-size PNG encoding and filesystem handoff as the continuous-stream hot path. The native daemon currently performs that work for each published frame.
- Multiple potential runtime owners. MCP constructs its own hub/service, while the broker constructs another service. Consolidate physical device ownership before allowing concurrent clients.

### Missing for this vision

There are no dedicated implemented HDR/SDR settings, Bluetooth/headphone, App Store installation, regional catalog, or subscription-entitlement workflows in the inspected source. General remote primitives could support some of them after reliable observation; they are not delivered features now.

`RecipeStore` exists but has no service/executor integration. It is scaffolding, not demonstrated learning. Postpone an elaborate learned navigation graph until a general visual loop works reliably.

The original `PRODUCT_SPEC.md:19` explicitly excludes an autonomous visual operator, and its first-slice non-goals repeat that exclusion. The initial design centered room-aware media commands and later accumulated visual Netflix control. This explains the mismatch with today's primary need: an agent that can handle Settings and unfamiliar screens.

## Evidence and validation

**Verified in this review:**

- `uv run --frozen pytest -q`: **275 passed in 44.91 seconds**.
- `uv run --frozen ruff check .`: passed.
- `uv run --frozen mypy src`: passed, 50 source files.
- `uv build --no-sources`: source distribution and wheel built.
- Device Hub exists at `/Applications/Xcode-beta.app/Contents/Applications/DeviceHub.app`; its interface was accessible through Codex Computer Use. Its available-device list did not show an Apple TV. No device screen was opened.
- The local tvOS 27 SDK was inspected to check ScreenCaptureKit availability, rather than relying on its framework-level description.

**Offline defect reproduction:** An injected `ScreenshotResult` with a capture timestamp one hour in the past was returned before and after an action. The controller dispatched a fake Right action, advanced sequence 1 to 2, and assigned a new `observed_at` timestamp to unchanged pixels. The capture timestamp did not survive into `ComputerUseState`.

This independently demonstrates a controller-contract gap at `computer_use.py:435` and `:482`. It does not prove that every production backend accepts hour-old images: the stream backend separately checks an age limit. Nevertheless, that backend returns the latest acceptable frame without a post-action barrier, and the controller discards producer timing. A recently captured pre-action frame can still be mislabeled as a new post-action observation. Passing tests did not catch this distinction.

**Reported by the July records; not reverified on hardware:** Living Room Netflix search, exact-title selection, and resume-then-pause succeeded in bounded trials. One documented broker run took 35.3 seconds. Later notes report full-frame capture at approximately 9–16 seconds and a native streaming daemon that never produced a frame. The newer handoff supersedes earlier optimistic observation notes. See `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md:22`; internal handoff notes remain in the private archive.

## First physical proof after the nearby Apple TV is ready

1. Identify the chosen device and record its tvOS version. Pair it deliberately; do not reuse Living Room's identity or silently update firmware.
2. Establish the simplest visible source in Device Hub or QuickTime, then test the new Appium/WDA route for focused labels, Settings values, and images. Check Home, Settings, and a third-party app. Record capture age, gaps, and reconnect behavior. Use the monitor/camera as an independent reference where useful.
3. Prove a short general loop: observe focus, move once, receive a causally appropriate post-action frame, enter text directly, and read it back. Inject stale frames/disconnects in offline tests and show they cannot masquerade as successful navigation.
4. Deliver the motivating settings workflow before adding another Netflix feature: inspect Video and Audio, inspect Bluetooth, and then make one explicitly requested reversible setting change with before/after evidence. Format changes need recovery through a temporary signal loss.
5. Prove title and episode identity, provider choice, and already-paired headphone connection. Distinguish prepared/paused content from playback; do not start playback merely to prove that title selection worked unless that is the requested test.
6. Repeat from warm, cold, modal, interrupted, and manually changed UI states. A five-minute healthy stream is a useful first gate, not enough to claim appliance reliability; later test longer sessions and sleep/wake cycles.

Engineering targets, not achieved results: aim for subsecond observation overhead on a warm stream and report p50/p95 source age, action-to-visible-result, inference time, and total task time separately. A fixed seven-second target for arbitrary multi-screen reasoning is premature.

## Product choices that improve the vision

- A saved "headphones tonight" or "projector movie" setup can become a verified shortcut after general navigation works.
- A settings change log with readable before/after values makes experiments reversible and helps explain a later regression.
- A short, opt-in diagnostic frame buffer can provide the useful part of computer history without recording an entire evening by default.
- Setup should expose capability tiers: direct control, visual control, and whole-room diagnostics. Mac-first is a reasonable initial visual implementation; do not claim identical support on Windows/Linux.
- Local control does not imply local inference. Images returned to a cloud agent are processed by that agent's provider; retain minimal screenshots and avoid sending credentials in diagnostic traces.
- Publish a compatibility matrix and a tested onboarding flow. No project-level LICENSE was found in the current tree; choose one and audit redistributed dependencies before an open-source release. Nothing was published during this review.

## Can learned timings remove the need for screen sharing?

The user's follow-up is sound: vision can be a development instrument, while ordinary production operations use learned transitions. The important distinction is **no continuous video** versus **no feedback**.

| Operation | Plausible production feedback |
|---|---|
| Launch an installed app | Direct launch plus usable app/UI state |
| Fill search | Keyboard focus plus exact text readback |
| Pause | Explicit Pause plus a fresh playback-state receipt |
| Reach a Settings item | Focused-element label or accessibility value; occasional image if semantics are absent |
| Run a known navigation shortcut | Confirmed start state, bounded actions, terminal-state check |
| Diagnose actual projector output | Device telemetry or physical-screen observation |

Calibrate input pacing and collect latency distributions, but use event/state waits with deadlines where possible. Network delivery timing does not determine whether an app is ready or which screen it restored. Profile prompts, dialogs, account state, manual remote input, and app updates break an otherwise perfectly timed sequence.

Learn small transitions such as known Home state to Search, not one giant sequence from an arbitrary state to playback. Store preconditions, observed focus, action semantics, versions/configuration, measured duration, success/failure evidence, and a recovery path. A repeated screen can have different focus. Directional steps that seem harmless may wrap, and Back is not a universal reset. Prefer direct app launch or a semantically identified anchor when establishing the start state.

After repeated success, reduce visual checks to boundaries and use cheaper semantic signals. Retain screenshots as a diagnostic/recovery channel. If no result signal exists, a best-effort macro can still be useful for low-impact operations, but must report that its result was not observed. It should not be the default for purchases, deleting data, or changing a video mode that can lose the screen.

This means slow or unavailable continuous screen sharing need not block the entire project. Accessibility feedback could be the more elegant primary interface. Camera-assisted development can also collect recipes while a better digital observer is being investigated.

## Existing projects and buy-versus-build findings

The requested independent GPT-5.6 SOL research found useful components, but no demonstrated turnkey product covering home-device control, settings, content availability, headphones, and reliable observation together. This is a search finding, not proof that no such product exists.

| Project or service | Reuse assessment |
|---|---|
| [pyatv](https://github.com/postlund/pyatv) | MIT; established control library already used here. Retain it. Its protocols are reverse engineered, so keep compatibility tests. |
| [Trevor Nichols' appletv-mcp](https://github.com/trevor-nichols/appletv-mcp) | MIT control server, recent v0.2.0 release in September 2026. Useful small tool surface and setup ideas; does not establish that the missing observation problem is solved. |
| Its [screenshot sidecar](https://github.com/trevor-nichols/appletv-mcp/blob/main/sidecars/appletv-screenshot/README.md) | GPL-3.0-or-later. The author explicitly reports fake tests and no Apple TV hardware capture yet. It is a one-shot DVT route, not evidence of a working continuous stream. |
| [slandau3/appletv-mcp](https://github.com/slandau3/appletv-mcp) | MIT; useful content-resolution experiments and author-reported physical-tvOS testing. Depends on a pyatv fork and an unofficial JustWatch endpoint; inspect ideas rather than adopting its resolver unchanged. |
| [crlian/mcp-pyatv](https://github.com/crlian/mcp-pyatv) | MIT; another thin control wrapper, not a complete visual/settings solution. |
| [Home Assistant Apple TV integration](https://www.home-assistant.io/integrations/apple_tv/) | Useful established home-automation integration for direct control. It does not supply the general screen/Settings operator. Keep Home Assistant optional. |
| [Appium XCUITest](https://github.com/appium/appium-xcuitest-driver), [WebDriverAgent](https://github.com/appium/WebDriverAgent), [appium-ios-remotexpc](https://github.com/appium/appium-ios-remotexpc) | Apache-2.0 driver/tunnel components and BSD-licensed WDA. Strongest new building blocks to evaluate for semantic UI access. |
| [device-hub-ios](https://github.com/JaviSoto/device-hub-ios) | MIT; HEVC/control implementation with iOS/iPadOS 27 beta verification. Interesting protocol reference, not proof of tvOS support. |
| [alokdhir/appletv-remote](https://github.com/alokdhir/appletv-remote) | Active-looking native Swift control implementation; no license was found in the research snapshot. Do not copy it on the assumption that public source implies reuse permission. |
| [BrowserStack](https://www.browserstack.com/docs/app-automate/appium/advanced-features/automate-tvos-apps), [TestingBot](https://testingbot.com/support/other/apple-tv), [TV Labs](https://docs.tvlabs.ai/platform/platforms/apple) | Commercial testing infrastructure, useful evidence for device automation. Their documented lab workflows are not a turnkey controller for the user's signed-in Apple TV at home. |

For availability data, the [official JustWatch API](https://apis.justwatch.com/docs/api/) requires a partner contract/token and attribution. Open-source client code does not itself grant access to a catalog service. [Watchmode](https://api.watchmode.com/) offers availability and paid link features; keep it an optional adapter, not a mandatory paid dependency for basic control. [TMDB watch-provider data](https://developer.themoviedb.org/reference/movie-watch-providers) is another discovery input to evaluate with its attribution requirements; it does not itself establish exact episode launch. A user's existing agent can also research availability before invoking the local device tools.

Recommendation: keep this repository's demonstrated foundations, reuse Appium/WDA rather than building an accessibility protocol stack from scratch, and borrow small MCP/onboarding ideas from the MIT projects where useful. Replacing the entire repository with another MCP wrapper would not resolve its hardest problem.

The next decision is which observation source—semantic, visual, or both—actually works on the user's chosen physical Apple TV. Subsequent implementation should follow that measured answer.
