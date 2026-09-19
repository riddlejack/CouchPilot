# Apple TV agent access and distribution

Date: 2026-09-19. Scope: current primary-source research, read-only inspection of the existing local spike, and a separately executed bounded warm-runtime proof. No hardware, account, payment, signup, or signing changes were made.

## Follow-up: owner accepts on-demand free renewal

The owner subsequently accepted automatic repair when a skill invocation finds
that visual access is expired or close to expiry. This is a practical maintenance
candidate: inspect the helper first, then reprovision, sign, reinstall, and verify
the same helper only when due. It still uses Xcode and a valid saved account
session; it does not remove provisioning expiry or guarantee unattended
authentication. The skill now
contains a [bounded renewal workflow](../plugins/apple-tv-agent/skills/apple-tv-agent/references/free-signing.md).
Actual expiry advancement and a complete renewal cycle remain unverified. The
assessment below incorporates this revised maintenance constraint.

## Decision

The practical open-source product should make **pyatv control and a LAN-local bridge** its baseline. On a Mac, add **wireless AirPlay/CoreMediaIO capture** once the existing native proof of concept reproduces the moving frames already seen in QuickTime. Let a Codex, Claude, Windows, Linux, or cloud agent call that bridge through an authenticated user-initiated connection. The agent host does not need to be the machine beside the Apple TV.

Keep WebDriverAgent (WDA) as an enhanced semantic-control tier, not a baseline dependency. Appium, pymobiledevice3, and go-ios can remove `xcodebuild` and sometimes Xcode from **runtime**, but none removes Apple's device-side signing and provisioning policy. The user now accepts a free on-demand rebuild and reinstall when the helper is due for renewal. That makes a bounded repair on the prepared Mac/Xcode account the strongest feasible route for this deployment, but renewal has not yet been demonstrated: the WDA already installed on the Media Room Apple TV is proven only through its current profile expiry on 2026-09-26.

This separates two questions that are easy to conflate:

1. **Can a small open-source host program start and talk to an already installed WDA without invoking Xcode at runtime?** Yes. The exact no-root pymobiledevice3 path now works on this already-paired Mac.
2. **Can an open-source tool install a long-lived WDA without an Apple-authorized profile?** No. Replacing Appium or Xcode does not replace Apple provisioning.

## Evidence status

| Status | Finding | Consequence |
| --- | --- | --- |
| **Confirmed live** | WDA 16.12.9 ran on the physical Apple TV 4K (2nd generation), tvOS 26.6, with semantic UI, 1920×1080 screenshots, and changing MJPEG frames. | The control/observation capability is real on this target. |
| **Confirmed live** | `pymobiledevice3` 11.15.5 launched the installed WDA through `developer dvt xcuitest --native`; WDA returned ready status and the standalone client received a real 4.3 MB screenshot. No Appium, `xcodebuild`, `sudo`, or `DEVELOPER_DIR` was involved. | The already-paired Mac has a small no-Xcode warm-runtime path. It is not proof of fresh onboarding, DDI setup, installation, signing renewal, or profile bypass. |
| **Confirmed live** | QuickTime showed wireless moving Apple TV frames after an on-TV AirPlay code. It required no Apple TV app and recording was never started. | A signing-free pixel source exists in QuickTime on this Mac; the headless native adapter is not yet verified. |
| **Confirmed live active video connection; frame delivery failed** | With QuickTime and WDA stopped, the exact target, input, running session, and one enabled video connection were present. Across 52 diagnostic samples the connection existed, was enabled and active, and had been active; the device was connected and not suspended. Screen-capture preflight and video authorization were positive. The active format reported `0×0` with subtype `0x69737220` (`isr` plus a trailing space), the output advertised BGRA, 420f, 420v, and 2vuy among its pixel types, and zero buffers arrived in 15 seconds. | This is not evidence of a TCC, disconnected-device, disabled-connection, or suspended-device failure. The retained blocker is unresolved source/format activation versus sample-buffer delegate delivery. Native pixels remain unverified. |
| **Confirmed live** | Device Hub refused this device with “must be running tvOS 27.0 or above.” | Apple's developer screen-sharing route is unavailable on the current OS, rather than merely undiscovered. |
| **Confirmed locally** | The signed WDA app is 2.5 MB; its profile expires 2026-09-26. The experimental Node `node_modules` tree is 173 MB. | Preinstalled WDA can be tiny; Appium is not the smallest runtime wrapper. |
| **Confirmed upstream** | Appium's `usePreinstalledWDA` starts an installed runner without `xcodebuild`; WDA still must be signed/trusted and needs a mounted Developer Disk Image. | Appium reduces runtime build work, not provisioning. |
| **Confirmed upstream** | pymobiledevice3 11.15.5 exposes a macOS `--native` transport through Apple's `remoted`/`remotepairingd`, documented as requiring no root, entitlement, or Xcode, and an `xcuitest` command that accepts process environment variables. | The upstream mechanism matches the successful local warm-runtime proof. |
| **Plausible, unproved here** | go-ios supplies MIT-licensed cross-platform binaries and WDA/XCTest commands. Its current repository does not identify tvOS or Apple TV support. | Do not adopt it until one exact wireless tvOS test passes. |
| **Plausible, unproved here** | pymobiledevice3 now exposes CoreDevice display video as raw RTP/HEVC. Apple exposes screenshots/video through Device Hub and `devicectl` on the tvOS 27 stack. | This is a promising no-device-app visual experiment after a user-chosen tvOS 27 update, not a current capability claim. |
| **Conditionally feasible; unverified** | Free Personal Team rebuild and reinstall through Xcode automatic signing. | It requires the prepared Mac and saved Xcode account session, and must prove a later embedded expiry plus successful reinstall. No live renewal cycle has been run. |

The live details and latency samples are in [MEDIA_ROOM_2026-09-19.md](MEDIA_ROOM_2026-09-19.md). This report does not reinterpret that spike as completed onboarding or distribution.

## The hard boundary: Apple provisions the device-side test runner

Apple states that a Personal Team's App IDs, registered devices, and provisioning profiles expire after seven days, after which the app must be rebuilt and reinstalled. The installed WDA's embedded profile has exactly that seven-day lifetime. A tvOS development profile requires an App ID, development certificate, and registered device. A tvOS Ad Hoc profile can run an app without Xcode, but still requires an App ID, distribution certificate, and registered devices through the paid program path.

This means:

- Appium's Apache-2.0 license and WDA's BSD license make the software reusable; they do not confer permission for an Apple TV to run an unsigned XCTest bundle.
- `usePreinstalledWDA`, pymobiledevice3, or go-ios can reuse, install, or launch a **properly signed** app. They do not make a Personal Team profile last longer.
- A prebuilt WDA distributed by this project cannot be one universal signed binary for arbitrary consumer Apple TVs. Per-user or per-team provisioning remains necessary.
- A paid developer/ad-hoc workflow would reduce weekly renewal friction and can install without Xcode, but it is outside this no-payment/no-signup assessment. Provisioning profiles still expire; Apple also documents a first-launch PPQ check for newer development teams unless a short offline profile is used.

The revised acceptance of on-demand renewal changes the product decision, not Apple's policy. A Personal Team can remain free if the prepared Mac repeatedly asks Xcode to provision a new seven-day build and reinstalls it when needed. This requires full Xcode for renewal even though the verified day-to-day runtime does not.

Primary Apple sources: [developer account and Personal Team limits](https://developer.apple.com/help/account/basics/about-your-developer-account), [tvOS development profiles](https://developer.apple.com/help/account/provisioning-profiles/create-a-development-provisioning-profile), [tvOS Ad Hoc profiles](https://developer.apple.com/help/account/provisioning-profiles/create-an-ad-hoc-provisioning-profile), [provisioning profile internals](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles), and [PPQ/offline profile behavior](https://developer.apple.com/help/account/provisioning-profiles/provisioning-profile-updates).

## Free Personal Team on-demand renewal

### Recommendation

On each request that needs WDA, inspect the cached helper profile before launch. If it is still healthy, start it through the existing pymobiledevice3 native route. If it is expired or inside the accepted repair window, run one bounded rebuild/reinstall attempt in the signed-in user's session so Xcode can use the prepared account and login keychain. Report a required sign-in or two-factor step to the user instead of retrying indefinitely. No background scheduler is needed.

This is **feasible but not yet end-to-end verified**. Apple explicitly says Personal Team App IDs and registered devices expire after seven days and that the app must be rebuilt and reinstalled after its provisioning profile expires. The installed `xcodebuild` help says `-allowProvisioningUpdates` lets an automatically signed target create or update profiles, App IDs, and certificates; `-allowProvisioningDeviceRegistration` lets it register the exact destination when necessary. The command authenticates through an account already added in Xcode or an App Store Connect API key. A free Personal Team does not include App Store Connect, so this deployment must rely on the Xcode account session and signing identity in the local user's keychain rather than an API key.

The repair has four separate gates:

1. **Authenticated signing session.** Xcode must remain signed into the Personal Team, the signing private key must remain usable in the login keychain, and Apple developer services must be reachable. Do not place an Apple Account password or two-factor code in the script. An expired account session is an interactive maintenance event, not something the job can safely bypass.
2. **Actually newer profile.** A successful rebuild does not prove renewal. Apple says automatic signing normally requests a profile only when it cannot find a locally cached profile that satisfies the build. A still-valid seven-day profile may therefore be reused. When renewal is due, identify only the cached profile matching the team, runner bundle ID, tvOS platform, and exact device; move it to a mode-0700 backup directory, then build with provisioning updates. Apple documents backing up and removing unwanted cached profiles to force a new automatic-signing request. Restore the backup if the build fails.
3. **Expiry verification before replacement.** Decode only the new runner's embedded `embedded.mobileprovision` into a private temporary plist. Require the expected team, exact runner application identifier, exact device membership, tvOS platform, and an `ExpirationDate` later than the currently installed build. Prefer a near-seven-day remaining lifetime. If expiry did not move forward, do not replace the working installation and do not report renewal.
4. **Install and runtime verification.** Run the newly generated `.xctestrun` against the exact Apple TV with `test-without-building`, wait for WDA readiness, and verify the helper-reported device identity. Then stop the test process and prove that the normal native launcher can start the newly installed bundle. Build success without this install/readiness chain is incomplete.

The local cache already contains the official WDA Xcode project, the shared `WebDriverAgentRunner_tvOS` scheme, a signed tvOS build-products directory, and its `.xctestrun`. Xcode 27.0 beta supplied the tvOS 27.0 SDK used by that build; Xcode 26.6 is also installed. Keep the WDA source version and hash pinned in a private durable directory rather than resolving a new package on each renewal.

The minimal build basis for this prepared cache is:

```sh
export DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
export WDA_PROJECT="$HOME/Library/Caches/home-media/appium-spike/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj"

"$DEVELOPER_DIR/usr/bin/xcodebuild" \
  -project "$WDA_PROJECT" \
  -scheme WebDriverAgentRunner_tvOS \
  -destination "id=$APPLE_TV_DEVELOPER_UDID" \
  -derivedDataPath "$PRIVATE_NEW_DERIVED_DATA" \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  DEVELOPMENT_TEAM="$APPLE_PERSONAL_TEAM_ID" \
  PRODUCT_BUNDLE_IDENTIFIER="$WDA_BASE_BUNDLE_ID" \
  CODE_SIGN_STYLE=Automatic \
  COMPILER_INDEX_STORE_ENABLE=NO \
  build-for-testing
```

`WDA_BASE_BUNDLE_ID` is the base identifier passed to the WDA project; Xcode's XCTest runner adds `.xctrunner`. Preserve the developer identifier's exact case as reported by Xcode. Use a new private derived-data directory for each attempt so the old accepted build remains available until the new profile passes inspection.

After profile inspection succeeds, install and start the exact new products with:

```sh
"$DEVELOPER_DIR/usr/bin/xcodebuild" \
  -xctestrun "$PRIVATE_NEW_XCTESTRUN" \
  -destination "id=$APPLE_TV_DEVELOPER_UDID" \
  test-without-building
```

These commands are a source-backed implementation basis, not a tested renewal claim. They require the Mac to be awake, logged into the prepared user account, online, able to access the login keychain, and able to reach an awake developer-paired Apple TV. Apple Account reauthentication or two-factor authentication, a locked signing key, a changed Xcode/WDA toolchain, expired device pairing, missing developer support, or Apple service failures can still require human intervention. Store logs and decoded profile data only in private mode-0600 files.

Sources: [Apple's Personal Team limits](https://developer.apple.com/help/account/basics/about-your-developer-account), [Apple's automatic-signing cache behavior and forced profile request](https://developer.apple.com/help/account/provisioning-profiles/edit-download-or-delete-profiles), [Apple's physical-device signing flow](https://developer.apple.com/documentation/xcode/running-your-app-on-simulated-or-physical-devices), [Apple's provisioning-profile structure](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles), [Appium's tvOS WDA build command](https://github.com/appium/appium-xcuitest-driver/blob/master/docs/getting-started/provisioning-profile/generic-device-config.md), and [Appium's prebuilt WDA behavior](https://github.com/appium/appium-xcuitest-driver/blob/master/docs/reference/capabilities.md).

## Appium versus smaller WDA launchers

### Appium/XCUITest driver

Appium remains the best documented general automation stack. Its current documentation explicitly covers physical wireless tvOS, focused elements, remote inputs, Windows/Linux hosts, RemoteXPC, and an already installed WDA. `appium:usePreinstalledWDA=true` avoids `xcodebuild`; `appium:webDriverAgentUrl` attaches to a separately managed WDA. Appium also documents stripping embedded XCTest frameworks so a runner can be 3 MB or less.

The tradeoff is packaging and privileges. This spike's complete Node dependency tree is 173 MB. Appium's own `appium-ios-remotexpc` tunnel creates TUN/TAP interfaces and its documented tunnel command requires root/sudo. On Linux it also requires kernel TUN setup; Windows requires an elevated shell and WinTun. Its current docs support Windows/Linux only for physical iOS/tvOS 18+ and require either preinstalled or externally managed WDA because `xcodebuild` is unavailable.

Appium is therefore valuable if consumers need the WebDriver protocol, client ecosystem, or its session management. This repository already talks directly to WDA, so carrying Appium solely to proxy the same HTTP API is unnecessary.

Sources: [preinstalled WDA](https://github.com/appium/appium-xcuitest-driver/blob/master/docs/guides/run-preinstalled-wda.md), [non-macOS hosts](https://appium.github.io/appium-xcuitest-driver/latest/guides/non-macos-hosts/), [RemoteXPC tunnels](https://appium.github.io/appium-xcuitest-driver/latest/guides/remotexpc-tunnels-real-devices/), [tvOS automation](https://appium.github.io/appium-xcuitest-driver/latest/guides/tvos/), and [appium-ios-remotexpc](https://github.com/appium/appium-ios-remotexpc).

### pymobiledevice3: verified warm-runtime launcher on this Mac

pymobiledevice3 is GPL-3.0 and runs on macOS, Linux, and Windows. Its current 11.15.5 documentation says:

- `developer dvt xcuitest` starts an XCUITest runner and accepts repeated `--env key=value` values.
- On macOS, `--native` uses Apple's existing `remoted` tunnel through `remotepairingd` with no root, entitlement, or Xcode.
- On current Linux/Windows systems, an in-process userspace tunnel is available without root/admin; Apple TV-like remote-pairing devices must first be paired.
- Developer commands still require Developer Mode and a mounted Developer Disk Image. `mounter auto-mount` exists, but clean tvOS onboarding without Xcode has not been proved here.

The successful test used an isolated, pinned pymobiledevice3 11.15.5 invocation. Keeping it as an optional subprocess preserves a clear GPL boundary and avoids silently changing the core environment.

The successful bounded invocation for the currently installed helper was:

```sh
# 1. List the Mac's already paired RemoteXPC devices without sudo.
# Select the physical target using the private identifier already held locally.
uvx --from pymobiledevice3==11.15.5 \
  pymobiledevice3 remote browse --native

# 2. Keep this foreground process running. No Appium or xcodebuild is involved.
uvx --from pymobiledevice3==11.15.5 \
  pymobiledevice3 developer dvt xcuitest \
  --native \
  --udid "$APPLE_TV_DEVELOPER_UDID" \
  --env USE_PORT=8100 \
  --env MJPEG_SERVER_PORT=9100 \
  --env WDA_PRODUCT_BUNDLE_IDENTIFIER=com.example.homemedia.WebDriverAgentRunner.xctrunner \
  com.example.homemedia.WebDriverAgentRunner.xctrunner

# 3. From a second shell, prove readiness through the same no-root transport.
uvx --from pymobiledevice3==11.15.5 \
  pymobiledevice3 developer wda status \
  --native \
  --udid "$APPLE_TV_DEVELOPER_UDID" \
  --port 8100

# 4. If the previously verified direct URL is reachable, exercise this repo's probe too.
uv run --frozen python scripts/wda_probe.py \
  --url "$APPLE_TV_WDA_URL" status
```

The environment names and values above match the current Appium WDA launch strategy and the local signed bundle. The test launched WDA, returned ready status, and produced a real 4.3 MB screenshot through the standalone client. The Apple TV was showing its PineBoard screensaver, whose semantic tree was empty as expected. No Appium, `xcodebuild`, `sudo`, or `DEVELOPER_DIR` was invoked.

This proves only warm runtime on this already-paired Mac with existing developer support mounted. It does **not** prove fresh onboarding, helper installation, profile renewal, Linux/Windows tvOS operation, or operation after 2026-09-26. The isolated Python runtime's installed size also remains to be measured.

A resident bridge can wrap the foreground command as a child process: start it on demand, wait for `GET /status` or `developer wda status`, retain one owner per Apple TV, and terminate the child on idle timeout or broker shutdown. WDA's port is unauthenticated, so the broker should be the only remote-facing service and the helper should not be left running indefinitely.

Sources: [pymobiledevice3 repository and GPL license](https://github.com/doronz88/pymobiledevice3), [current XCUITest CLI](https://doronz88.github.io/pymobiledevice3/cli/developer/#dvt-xcuitest), and [current no-root tunnel behavior](https://doronz88.github.io/pymobiledevice3/guides/ios17-tunnels/).

### go-ios: attractive license and binary shape, tvOS unconfirmed

go-ios is MIT-licensed and designed as a static cross-platform CLI. Its current source documents app installation, developer-image mounting, `runtest`, `runwda`, `runxctest`, screenshot/MJPEG, and signing with a P12 plus provisioning profile. The current v1.3.2 release provides official compressed binaries of about 16.6 MB for Linux, 16.8 MB for macOS, and 8.8 MB for Windows.

This is the strongest permissively licensed small-binary candidate. The limiting evidence is material: neither the current README nor a source-tree search identifies tvOS or Apple TV support. Its README's iOS 17+ tunnel command also requires sudo, and signing still requires Apple material. Treat go-ios as a candidate for a bounded compatibility trial, not a distribution answer.

Sources: [go-ios repository, commands, and MIT license](https://github.com/danielpaulus/go-ios) and [v1.3.2 release](https://github.com/danielpaulus/go-ios/releases/tag/v1.3.2).

## Signing-free observation

### Current default: Mac AirPlay/CoreMediaIO

QuickTime's live success is the strongest evidence because it used the exact physical target. It received moving wireless screen frames after a one-time on-TV AirPlay code, with no app installed on the Apple TV. The user did not need to begin a recording.

A headless native adapter is plausible using public macOS CoreMediaIO and AVFoundation APIs. The installed Command Line Tools SDK, separate from full Xcode, contains `kCMIOHardwarePropertyAllowScreenCaptureDevices` and `kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices`. The new `scripts/airplay_capture_probe.swift` initializes and retains an AVFoundation discovery session before toggling those process-local properties. Its enumeration-only run found nine sources with distinct private IDs, including exactly one Bedroom source, while QuickTime was closed.

The exact-UID capture tests advanced further but failed their acceptance condition. In the final bounded run, the session had one output and the video connection existed, was enabled, and remained active across 52 diagnostic samples. The source device remained connected and not suspended; `CGPreflightScreenCaptureAccess` was true and video authorization was authorized. The output advertised common BGRA and YUV pixel types. Even so, the active format reported zero width and height with media subtype `0x69737220` (`isr` plus a trailing space), and no sample-buffer delegate callback arrived in fifteen seconds.

This rules out a blank permission explanation and an absent, disabled, or inactive `AVCaptureConnection` in that sample. It does not establish whether `0×0` is a wireless-source placeholder awaiting another activation/format negotiation step or whether an active connection is failing to deliver through `AVCaptureVideoDataOutput`. No further hardware experiment was run, so that boundary remains open.

Constraints are straightforward: Mac-only, user-visible AirPlay pairing/approval when required, macOS Screen & System Audio Recording permission, and possible black/protected content. Permission was granted in the zero-buffer run, so it is not the evidenced blocker in that sample. The route provides pixels, not accessibility semantics. It does avoid an Apple TV app, developer signing, Appium, and full Xcode.

Sources: [QuickTime movie source selection](https://support.apple.com/guide/quicktime-player/record-a-movie-qtp356b55534/mac), [macOS screen-recording permission](https://support.apple.com/guide/mac-help/control-access-to-screen-and-system-audio-recording-mchld6aa7d23/mac), and [Apple's protected-video limitation](https://support.apple.com/102618). The CoreMediaIO constants were verified in the installed public Command Line Tools SDK header `CoreMediaIO.framework/Headers/CMIOHardwareSystem.h`; the connection-state definition is in `AVFoundation.framework/Headers/AVCaptureSession.h`.

### tvOS 27 developer capture

Apple documents Device Hub device viewing plus screenshot/video capture, and the installed Xcode 27 beta's `devicectl` exposes:

```text
devicectl device capture screenshot --device … --destination …png
devicectl device capture screen-record --device … --destination …mp4
```

The current tvOS 26.6 target explicitly failed Device Hub's OS gate. After a user-chosen tvOS 27 update, this route is likely to become available, but it has not been tried. Local inspection finds `devicectl` in the Xcode application and not in Command Line Tools. It is not an open-source redistributable minimal package and therefore does not meet the desired distribution shape by itself.

pymobiledevice3 11.15.5 exposes the underlying-looking alternative `developer core-device display start-video-stream`, which records length-prefixed RTP/HEVC packets over its no-root native/userspace transports. It may provide a small no-Xcode receiver for the tvOS 27 developer stream. Its documentation does not claim this exact command works on Apple TV, so the first tvOS 27 experiment should call `get-media-support-info` and capture five seconds before any product integration.

ScreenCaptureKit in a tvOS 27 companion app is not a full-system substitute. The installed tvOS 27 SDK marks display-style sharing unavailable on tvOS and exposes a picker for the current application. It does not establish supported capture of Settings, Home, or other apps.

Sources: [Device Hub](https://developer.apple.com/documentation/xcode/device-hub), [Apple device screenshot/video capture](https://developer.apple.com/documentation/xcode/capturing-screenshots-and-videos-from-devices), and [pymobiledevice3's current developer CLI](https://doronz88.github.io/pymobiledevice3/cli/developer/).

## Agent host versus local bridge

The Apple TV protocols are local-network and stateful. That constrains the **bridge**, not the agent:

```text
Codex / Claude / browser / Windows / Linux / cloud agent
                  |
       authenticated, user-initiated connection
                  |
        LAN-local bridge beside Apple TV
        - pairing secrets stay local
        - pyatv control and metadata
        - Mac AirPlay/CoreMediaIO frames
        - optional signed WDA lifecycle
                  |
              Apple TV
```

The bridge should initiate or accept only an authenticated private connection, authorize tools narrowly, serialize actions per device, and return bounded observations. Do not publish WDA port 8100, MJPEG port 9100, Appium, or a raw RemoteXPC registry to the public internet. A cloud agent needs the broker API and image/result payloads; it does not need Apple pairing credentials or direct LAN discovery.

Windows/Linux can already be the agent host. They can also be the local bridge for pyatv. Full visual/semantic parity on a Windows/Linux bridge is conditional: Appium documents tvOS 18+ plus preinstalled WDA and RemoteXPC, and pymobiledevice3 documents Apple TV pairing plus userspace tunnels, but neither changes signing. go-ios's exact tvOS behavior is still open. A nearby Mac bridge is the lowest-friction verified visual configuration.

## License and distribution fit

| Component | License/source status | Distribution implication |
| --- | --- | --- |
| pyatv | MIT | Good baseline dependency. |
| Appium server / XCUITest driver / appium-ios-remotexpc | Apache-2.0 | Permissive, but sizeable Node runtime and documented tunnel privileges. |
| WebDriverAgent | BSD | Redistributable source/helper subject to license; device execution still requires Apple signing. |
| pymobiledevice3 | GPL-3.0-or-later | Excellent proof tool. Keep as a separately invoked optional executable unless the project's license/distribution policy deliberately accepts GPL obligations; obtain legal review before redistribution. |
| go-ios | MIT | Attractive standalone helper if a tvOS trial passes. |
| CoreMediaIO / AVFoundation / `devicectl` / Device Hub | Apple platform software | Usable as host capabilities under Apple's terms; not open-source components to rebundle casually. |

Invoking a GPL CLI as a separate process is technically separable from importing its Python APIs, but that does not remove obligations when distributing the CLI or a combined package. The current project already labels pymobiledevice3 as an optional GPL tool and shells out; preserve that boundary until licensing is intentionally decided.

## Small discriminating experiments

Run these in order; each resolves a specific decision without changing hardware or paying/signing up.

1. **Completed: managed no-Xcode warm runtime of the existing signed helper.** The bridge launched WDA through pinned pymobiledevice3 11.15.5, returned real semantic/image observations, stopped its owned runner and endpoint on shutdown, and started it again in a fresh MCP session without Appium, `xcodebuild`, `sudo`, or `DEVELOPER_DIR`. The 173 MB Appium runtime and sudo tunnel are unnecessary for this prepared-Mac runtime path.
2. **Completed with a sharp negative: native AirPlay connection-state diagnostic.** The connection existed and stayed enabled and active while the device remained connected and unsuspended, but its format was `0×0` and no buffers arrived. This leaves wireless-source/format activation versus `AVCaptureVideoDataOutput` delivery unresolved. Stop this line until a new source-backed discriminator justifies another hardware run.
3. **Next durability experiment: one controlled free renewal.** Within the final 48 hours of the current profile and only after explicit authorization to touch the account and device, request a new automatic-signing profile, require a later embedded expiry with the same team, bundle ID, platform, and device, run the new `.xctestrun` on that Apple TV, verify WDA readiness and identity, then prove the normal native launcher can restart it. An unchanged expiry, build-only success, install failure, or interactive sign-in requirement is not a completed renewal.
4. **go-ios discovery-only tvOS trial.** Use the official v1.3.2 binary and existing pairing material; first test list/info/tunnel discovery without installing or signing anything. Stop if it cannot identify the Apple TV through a documented path. Only then try launching the still-valid preinstalled runner. This decides whether the MIT small binary deserves integration work.
5. **Fresh-machine/DDI audit.** In an isolated user environment, determine whether pymobiledevice3 `mounter auto-mount` can prepare this tvOS version without Xcode content. Record downloads, Apple-hosted artifacts, license, size, and persistence. Do not describe the existing mounted DDI as clean onboarding.
6. **tvOS 27 capture only after the user independently chooses to upgrade.** First try Device Hub/`devicectl` screenshot for the primary source baseline. Then try pymobiledevice3 `get-media-support-info` and a five-second HEVC packet capture. Pass: decoded changing frames from Home and Settings with no device-side app. This determines whether tvOS 27 can remove both AirPlay UI and WDA from the visual tier.

## Practical default

Ship a permissively licensed local controller built around pyatv and keep the bridge API agent-neutral so Codex, Claude, Windows/Linux clients, and cloud agents can all use the same nearby bridge. Keep native CoreMediaIO/AirPlay capture as paused Mac-only research until it delivers frames; the active `0×0` result is not an implementation-ready visual route.

Offer WDA as an explicitly enhanced tier for users who already have valid Apple provisioning. On an already-paired Mac with developer support mounted, use the verified pymobiledevice3 11.15.5 native warm-runtime path around the 2.5 MB preinstalled helper; Appium remains an optional compatibility layer. For this user, an on-demand expiry check plus free Personal Team rebuild/reinstall is now the preferred durability experiment. Promote it only after one run proves a later embedded expiry, installation, WDA readiness, and subsequent native launch; do not describe command construction or build success as verified renewal.

If tvOS 27 later proves the CoreDevice display stream through a small open-source transport, promote it after measuring onboarding and protected-content behavior. Today, pyatv is the verified low-friction direct-control baseline, and the verified programmatic visual route is the already installed signed WDA launched through the native pymobiledevice3 warm-runtime path. QuickTime proves that this Mac can receive Apple TV pixels over AirPlay, but the headless CoreMediaIO adapter remains an open implementation gate.
