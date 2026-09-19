# Nearby Apple TV observation spike

> Historical record: some experimental components mentioned here are now archived. See [the scope change](../docs/HISTORY.md) for the current package and preserved source.

Date: 2026-09-19. This is a live continuation of the independent reassessment.

## Confirmed target

The user calls the nearby test setup **Media Room**. Bonjour and Apple developer tools advertise the device as **Bedroom**. The device was matched to the IP address read by the user from the physical Apple TV; do not select another device by name alone. Private device identifiers remain in the local runtime cache.

- Apple TV 4K, second generation.
- tvOS 26.6.
- Companion, AirPlay, and RAOP advertised.
- Developer pairing with this Mac completed using the user's supplied verification code.
- `devicectl device info details` succeeded; Developer Mode enabled, DDI services available, native developer tunnel connected.

Another AirPlay/RAOP endpoint also advertises Bedroom. It did not identify as this Apple TV. This was a concrete reason to verify the physical device's IP before pairing.

## Screen observation

**Device Hub: blocked by an explicit OS requirement, not an unresolved discovery issue.**

After pairing and successful developer connection, Device Hub offered View Screen. Its eventual result was:

> Screen Sharing Unavailable — Bedroom must be running tvOS 27.0 or above to screen share with this Mac.

This establishes what the installed Device Hub requires for this physical target. It does not yet establish successful capture on tvOS 27. No firmware update was performed.

**QuickTime: usable wireless frames verified.**

QuickTime's New Movie Recording source menu listed Bedroom under Screen. The initial preview was black. After the user entered the on-TV AirPlay code, QuickTime showed the moving aerial screensaver. A WDA Menu input dismissed the screensaver and the QuickTime preview showed Settings. Recording was never started. QuickTime was subsequently closed to isolate WDA capture; direct WDA screenshots and streaming continued to work.

## Appium / WebDriverAgent preparation

Tools installed in an isolated user cache, not globally and not as project dependencies:

`~/Library/Caches/home-media/appium-spike/`

- Appium 3.7.0.
- XCUITest driver 12.12.6.
- appium-ios-remotexpc 5.20.3 requested from the official npm registry.
- Appium doctor: zero required fixes; optional applesimutils absent.
- One Apple development signing identity was present.

The regular Xcode installation could not build the tvOS target because its required tvOS platform component was unavailable. A per-command `DEVELOPER_DIR` pointing at the existing Xcode beta resolved that build issue; the global Xcode selection was not changed.

**Unsigned physical-device WDA build succeeded** with `WebDriverAgentRunner_tvOS`, generic tvOS destination, and signing disabled. This proves compilation only.

The signed generic-destination build failed because the development team had no registered tvOS devices suitable for a provisioning profile. Intended app identifier:

`com.example.homemedia.WebDriverAgentRunner.xctrunner`

The user explicitly authorized registration and installation, with no payment. Building for the exact physical device with automatic provisioning and device registration succeeded. Both `codesign --verify --deep --strict` and membership of the target UDID in the embedded profile passed. The generated profile expires **2026-09-26**, seven days after this test. This is a material ongoing-use/onboarding constraint of this signing setup.

The beta compiler treated `/usr/local/include` as an unsafe cross-compilation include directory. A build-only `OTHER_CFLAGS=$(inherited) -Wno-error=poison-system-directories` override allowed the signed build. This is a spike workaround to revisit when packaging; it does not establish a clean build on every Xcode installation.

`xcodebuild test-without-building` installed and started the helper through the existing native developer connection. No sudo RemoteXPC tunnel or tvOS update was needed in this configuration. Appium's HTTP server was not required: the experiment called WDA directly. WDA reported version 16.12.9, built using SDK 27.0, running on tvOS 26.6.

## Verified live capabilities

| Capability | Observed result |
| --- | --- |
| Remote input plus independent visual response | Menu dismissed the screensaver; QuickTime showed the Settings page. |
| Settings semantic observation | Read row labels and values, including current focus. |
| Selection by label | Selected the Bluetooth cell from Remotes and Devices; read connection status on the Bluetooth page. Selected Video and Audio from Settings. WDA computed the intervening focus navigation. |
| Video/audio values | Read Format = 1080p HDR, Chroma = 4:2:2, Match Content = Off, and Audio Format = Auto. These are observations, not recommended settings. |
| Direct screenshot, independent of QuickTime | Full 1920×1080 image. Five consecutive requests after closing QuickTime: 0.295, 0.294, 0.284, 0.285, 0.285 seconds. |
| MJPEG delivery on a static Settings page | 30 frames; 9.65 received frames/second; largest gap 0.111 seconds. Identical hashes on the static page are expected; this alone does not prove fresh updates. |
| MJPEG during remote navigation | Six alternating Down/Up inputs moved focus Format → Chroma → Format, verified after every action. 41 frames, 14 distinct hashes, 9.45 received frames/second, largest gap 0.193 seconds. |
| Input-to-first-different-frame interval | 0.113–0.148 seconds over those six inputs. This measures receipt of a changed image, not physical HDMI latency or completion of the animation. |
| Home screen | Read installed app icon labels and focused Settings icon. |
| Third-party app | Selected Netflix's Home icon by label; screenshots/stream showed its profile chooser. Subsequent semantic observation read profile labels and current focus. No profile was selected and no program was played. |

Settings tree requests in the initial samples took approximately 0.7–1.4 seconds; one Video and Audio tree took 3.16 seconds. Home tree observation took 3.83 seconds. Netflix's successful tree read took 0.86 seconds. These are small exploratory samples, not a latency guarantee or reliability benchmark.

## Failure found and recovery

Immediate tree reads after Home/app transitions timed out at 20 seconds. A later active-app read also timed out. During the Netflix failure, MJPEG still returned a usable profile-chooser image in 0.13 seconds, and a separate screenshot succeeded in 0.32 seconds. The screenshot/stream route must remain independent of the semantic route.

WDA defaults to a 10-second application-idle wait and a two-second animation cool-off. Setting its **helper session** `waitForIdleTimeout=0` and `animationCoolOffTimeout=0` was followed by successful Netflix observation, **but another transition still timed out**. Explicitly choosing the active application for tree reads alone also did not eliminate the failure. These change automation behavior, not the Apple TV's accessibility or picture settings.

The probe was then changed to wait until the requested bundle ID is actually active **before** requesting its tree. This passed the next Settings → Netflix transition, including the expected profile-chooser label, in 1.43 seconds total request time. Returning to Video and Audio with a screenshot passed in 4.54 seconds. This is a promising fix candidate, not sufficient repetition to claim the transition bug solved. One recovery command also waited 14.12 seconds on a helper settings request, consistent with outstanding work continuing after a client timeout; client timeout is not server-side cancellation.

One immediate post-input tree also described the transition rather than the final destination. Therefore, a successful HTTP response and a newly received tree are insufficient. Wait for an expected visible label/app/focus, with a bounded deadline. Never automatically repeat a potentially successful action merely because its observation timed out.

## Repeatable probe

[`../scripts/wda_probe.py`](../scripts/wda_probe.py) is a standard-library Python 3.12+ experimental CLI for the running helper. It supports status, structured observations, standalone screenshots, remote buttons, unique visible cell/button/icon selection, and activation of a verified bundle ID. It records request timings and explicit image request/receipt bounds. `--expect` checks a visible destination label; an acknowledged command without that check is not reported as a verified goal.

Examples, with the exact device endpoint supplied locally:

```sh
uv run --frozen python scripts/wda_probe.py --url "$APPLE_TV_WDA_URL" status
uv run --frozen python scripts/wda_probe.py --url "$APPLE_TV_WDA_URL" --expect 'Video and Audio' activate com.apple.TVSettings
uv run --frozen python scripts/wda_probe.py --url "$APPLE_TV_WDA_URL" --expect Bluetooth select Bluetooth
uv run --frozen python scripts/wda_probe.py --url "$APPLE_TV_WDA_URL" --screenshot /tmp/apple-tv.png screenshot
```

The Bluetooth example requires a current page containing that unique visible cell. Selection deliberately fails on zero or multiple matches. Observations are sequential samples, not atomic screen snapshots. Screenshot fallback can be invoked even when a semantic read fails. This CLI is not yet connected to the production MCP controller.

The local cache contains the npm tools, signed build, private signing configuration, target identifiers, `*.xctestrun`, measurements, and captured UI evidence. Do not commit the cache. To restart this specific installed build, use Xcode beta's `xcodebuild -xctestrun <cached xctestrun> -destination id=<confirmed UDID> test-without-building`. Rebuilding after profile expiration requires automatic provisioning again. General installation/onboarding still needs implementation and testing.

## Architectural consequence and remaining proof

The central observation bottleneck is **demonstrably solvable on this unmodified physical Apple TV**. The leading implementation is now pyatv for ordinary direct commands plus an on-demand WDA session for semantic UI, screenshots, and MJPEG. The existing native AirPlay work is a fallback/packaging opportunity rather than the sole critical path. Device Hub's tvOS 27 requirement does not block this route.

Keep LLM interpretation on demand; 10 frames/second delivered locally does not require sending 10 frames/second to a model. Preserve producer-frame timing and action/destination checks when integrating this with the existing controller.

Still unproved: overnight stability, reconnection, reboot/sleep recovery, protected playback visibility, exact-episode resolution/playback, app installation flows, actual headphone connection, settings-change rollback, other device/tvOS versions, and frictionless signing renewal. No Apple TV picture/audio setting was changed in this spike. WDA exposes a powerful unauthenticated LAN control endpoint during a test; production should use an authenticated local broker, restricted transport, and an on-demand lifecycle rather than leave that endpoint running indefinitely.

## Closeout

The final screenshot and semantic read both showed **Video and Audio**, with **Format** focused. The helper test process was deliberately stopped after the proof; its signed app remains installed for reuse. The original working-tree changes were preserved. New work is limited to this report, the reassessment report, and the experimental probe script.

Validation: live status, observations, app activation, visible icon selection, screenshot/stream capture, and the navigation measurements above; the probe rejected a deliberately nonexistent label without sending a selection. Ruff and Python compilation passed for the new script. Earlier baseline validation passed 275 repository tests, Ruff, mypy, and package build; production package code was not changed in this spike.
