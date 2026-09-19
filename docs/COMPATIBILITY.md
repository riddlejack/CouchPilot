# Apple TV Agent compatibility

Status date: 2026-09-19. This matrix separates observed hardware behavior from implemented code and plausible portability.

## Host roles

| Role or route | Status | Boundary |
| --- | --- | --- |
| Agent on macOS, Windows, Linux, or cloud | **Architecture supported; remote transport not bundled** | The agent calls an authenticated local stdio bridge through a separately supplied private connection. Cloud placement does not give it direct LAN or WDA access. |
| Local bridge on macOS with direct pyatv control | **Verified live on the tested target** | Needs the Apple TV on the local network, a stable discovery ID, and Companion pairing. |
| Local bridge on Linux | **CI configured; not executed here** | Python/pyatv and the MCP server are portable, but this exact package and Apple TV onboarding flow have not been verified on Linux. |
| Local bridge on Windows | **Plausible; not in current CI** | Python/pyatv is portable, but this exact package and Apple TV pairing flow have not been tested here. |
| Visual bridge with external WDA endpoint | **Verified live on the tested target** | The helper must already be correctly signed, running, private, and identity-pinned. |
| Managed WDA startup with cached `.xctestrun` | **Implemented; Mac only** | Current manager invokes `xcodebuild test-without-building` and needs a full Xcode developer directory. |
| Managed WDA startup with `pymobiledevice3` 11.15.5 `--native` | **Verified live on the prepared Mac** | The bridge launched the installed helper on demand, restarted it in a fresh MCP session, and stopped its owned process on shutdown without Appium, `xcodebuild`, `sudo`, or `DEVELOPER_DIR`. It reused existing pairing and mounted developer support. |
| QuickTime wireless Apple TV frames | **Verified live on the Mac** | Requires user-visible AirPlay approval. The headless CoreMediaIO adapter has not yet reproduced QuickTime's frames. |
| Native CoreMediaIO/AVFoundation capture | **Active connection verified; frame delivery failed** | With QuickTime and WDA stopped, the exact target, input, running session, and one enabled video connection were present. Across 52 samples the connection was active, the device was connected and not suspended, and permissions were positive, but the active format stayed `0×0` with subtype `0x69737220` (`isr` plus a trailing space), and zero buffers arrived in 15 seconds. The remaining blocker is unresolved wireless-source/format activation versus output-delegate delivery; native pixels remain unverified. |
| tvOS 27 Device Hub or CoreDevice capture | **Plausible; not tested** | The physical target remains on tvOS 26.6; Device Hub rejected it because tvOS 27 is required. |

## Device and operation matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Apple TV 4K (2nd generation), tvOS 26.6 | **Verified live target** | WDA 16.12.9 returned semantic UI, 1920×1080 screenshots, and changing MJPEG frames. |
| Other Apple TV models and tvOS versions | **Open** | No blanket compatibility claim is made. |
| Direct `status`, `apps`, `wake`, `sleep`, `transport`, `open_url`, `launch`, `type`, `press` | **Implemented; bounded live acceptance** | Sleep/wake, app inventory, and App Store detail links were verified. Immediate command responses can still contain prior state, so completion needs a later observation. Raw buttons use the existing paired-control allowlist. |
| Visual `observe` | **Verified live on the tested target** | Text is the default and images are optional. Empty or previous trees occurred during transitions; a fresh observation resolved tested cases. Image and tree samples can represent slightly different moments, so compare their timestamps before acting on a contradiction. |
| Visual `press`, `select`, and `type` | **Verified live with receipts; bounded select hardened afterward** | Requires a fresh `observation_id`, consumed before dispatch. Current `select` requires one exact, unique, already-focused label and sends one remote Select input. An unfocused match is rejected before mutation; focus navigation uses bounded directions plus a fresh observation. |
| Visual focus handling | **Bounded live acceptance** | Positive focused-element evidence is retained when WDA's visibility flag conflicts with the screen. When Arcade semantics failed, an image-guided Right input recovered navigation; the next state was observed before continuing. |
| Visual app launch | **Verified for tested apps** | Bundle ID and post-launch state still need verification for each request. |
| Content and episode resolution | **Verified bounded workflow; not a universal resolver** | Netflix search and one requested episode were verified. Provider catalog, region, subscription, profile, and current UI affect other requests. Installed apps do not establish subscriptions. |
| App Store redownload and launch | **Verified for one free existing-account redownload** | [Apple's official VLC tvOS URL](https://apps.apple.com/us/app/vlc-media-player/id650377962?platform=tv) reached the correct page; Redownload progressed through Preparing to Open, and Open launched `org.videolan.vlc-ios`. This does not cover a new Apple ID, password prompt, first purchase, or paid app. |
| Settings read/change/restore | **Verified bounded workflow** | Match Dynamic Range was read Off, changed On, verified, restored Off, and verified. Format and Chroma were read without modification. |
| DRM-protected video capture | **Limited by platform/content** | Captures may be black or unavailable. Do not infer playback failure from protected pixels alone. |

## Provisioning and distribution

- WDA is optional. Direct control does not require an Apple developer account or device-side app.
- Alternate runners do not remove Apple signing. A Personal Team helper profile expires after seven days and must be renewed and reinstalled.
- Best-effort on-demand free renewal is feasible only on a prepared Mac with full Xcode, an active Xcode account session, an accessible signing key, network access, and the exact developer-paired Apple TV. The skill checks this when WDA is needed; no scheduler is required. It has not been live-verified. A build must contain a later embedded expiry and be reinstalled successfully before renewal is claimed.
- In the accepted build, the Python 3.12 wheel was 210,923 bytes, its full dependency environment was about 97 MiB, the optional native runner environment was about 152 MB, and the signed helper was about 2.5 MB. These figures exclude Python, uv, and existing Apple developer support.
- `pymobiledevice3` is GPL-3.0-or-later. The native backend invokes pinned version 11.15.5 through `uvx` as a separate optional process; do not redistribute it with the MIT core unless distribution obligations are deliberately accepted.
- The package is not on PyPI. Install from the experimental `codex/apple-tv-agent-preview` branch or a local wheel containing the changes.
- The plugin's `.mcp.json` invokes the installed `apple-tv-agent-mcp` command and needs no model API key.

The verified managed-runtime result does not establish first-time visual pairing, fresh Developer Disk Image setup, helper signing or installation, seven-day renewal avoidance, Linux/Windows WDA operation, or a durable consumer distribution route.

See [AGENT_QUICKSTART.md](AGENT_QUICKSTART.md), [the live acceptance record](../research/AGENT_ACCEPTANCE_2026-09-19.md), [the physical-device record](../research/MEDIA_ROOM_2026-09-19.md), and [the access assessment](../research/ACCESS_AND_DISTRIBUTION_2026-09-19.md).
