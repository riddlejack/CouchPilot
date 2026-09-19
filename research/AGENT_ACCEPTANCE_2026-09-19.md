# General agent bridge: live acceptance

Target: the user's nearby Apple TV 4K (2nd generation), tvOS 26.6. Exact device
binding was confirmed against the user's physical-device IP, its developer UDID,
WDA device identity, and Companion/AirPlay advertisements. Private identifiers,
credentials, screenshots, and raw transcripts remain outside the repository.

These checks used the real `apple-tv-agent-mcp` stdio server with an MCP client.
No nested LLM or separate model API key was used by the bridge. This is a limited
single-device acceptance run, not an overnight reliability benchmark.

## Demonstrated

| Workflow | Evidence |
| --- | --- |
| MCP protocol | Initialized and exposed exactly devices, observe, act, status, apps, control. Screenshots arrived as native image content blocks. |
| Native helper lifecycle | With the exploratory runner stopped, an MCP observation launched the installed WDA through pinned pymobiledevice3 11.15.5. Semantic state returned with a new session. No Appium, xcodebuild, sudo, or DEVELOPER_DIR needed for this runtime. |
| Shutdown | Closing the MCP session left zero native-runner processes; the WDA HTTP endpoint was unreachable afterward. |
| Restart | A subsequent fresh MCP session launched the helper and opened the App Store. The combined cold-start/launch/first observation took 4.071 seconds in one sample; the transitional observation was correctly unverified. |
| Direct pairing | Companion and AirPlay pairing completed. Pairing codes were read from the target's WDA observation; credentials stayed in the existing private pyatv store. |
| Installed apps | Paired control returned the installed bundle IDs, including Netflix, Settings, and App Store. |
| Sleep/wake | Sleep was followed by power=off; wake was followed by power=on. Screen observation worked afterward in the same bridge session. Immediate command responses still contained the previous power state, so they were not treated as completion. |
| Netflix profile/search | Entered the requested profile, focused the keyboard, typed Breaking Bad, and read back both the search-field value and exact result label. |
| Episode preparation | Opened the result and observed Pilot plus Play Season 1: Episode 1. |
| Playback/pause | Pressed the observed episode control; subsequently paused at 00:40. The overlay exposed Play, elapsed 00:40, remaining 57:39. After sleep/wake, Netflix exposed Resume S1: Ep. 1, 1 percent complete. |
| Settings edit and restoration | Read Match Dynamic Range=Off; changed it to On; read On; restored Off and read Off. Match Frame Rate remained Off. |
| Baseline picture settings | Read Format=1080p HDR and Chroma=4:2:2. This run did not change the output format or HDMI mode. |
| App installation and launch | Opened [VLC's official App Store page](https://apps.apple.com/us/app/vlc-media-player/id650377962?platform=tv) by direct Companion URL. Observed Redownload, then Preparing to download, then Open; opened it and observed active bundle org.videolan.vlc-ios and its Local Network screen. No purchase or account prompt was involved. This tests an existing-account free redownload, not first-purchase authentication. |

## Failures retained

- Selecting Netflix's profile cell by label moved focus but timed out. A later
  observation showed the intended profile focused; an explicit Select input
  then entered it. WDA's native cell click can navigate internally for many
  iterations. A client timeout does not cancel that server-side work. The final
  bridge removes element-click navigation: an exact unique target must already
  be focused before one remote Select is sent. This replacement was tested live:
  Video and Audio opened and Match Content was read; unfocused Chroma was rejected,
  and a subsequent observation confirmed focus remained on Format.
- Typing while the Search navigation tab was focused returned an acknowledgement
  without changing the field. After moving focus into Keyboard, typing worked.
  The workflow verified readback rather than equating acknowledgement with text.
- Some transitions returned an empty tree or the previous screen. A subsequent
  observation resolved them. Failed postconditions stayed unverified. One final
  Home transition needed a later observation taking 20.588 seconds; this is not
  a uniform low-latency guarantee. The final screen was verified as Home before
  the action-test bridge was closed. The device later entered its screensaver
  during idle time. A final cold check requiring Home correctly remained
  unverified; a fresh cold check without that assumption returned PineBoard and
  a real screensaver image in 6.776 seconds. Its empty semantic tree was
  consistent with the image, and the owned helper stopped on exit.
- Protected Netflix video pixels were black. Playback controls remained readable.
  pyatv reported the app but did not supply episode/title/time metadata in these
  samples. Its idle state alone was not accepted as proof of paused playback.
- App Store tab buttons were visibly on screen but WDA marked them invisible;
  the focused tab still had positive focus evidence. This motivated retaining
  conflicting focus evidence explicitly while leaving other hidden nodes hidden.
- Native AirPlay capture initially enumerated no devices because AVFoundation's
  device graph was not initialized. Seeding a retained DiscoverySession found
  the target. Its CMIO ID matched the target's Companion/AirPlay advertisements.
  The first exact-device capture attempt with QuickTime and WDA stopped timed out
  after 15 seconds. Native frame delivery remains unverified.
- A second native capture diagnostic reached `session_running` with camera
  authorization and screen-capture preflight both positive, but delivered zero
  buffers within 15 seconds. This narrows the failure to frame delivery after
  session startup; it does not establish that granting another permission would
  fix it. A standalone paired screensaver command also timed out; it was not
  replayed or claimed successful.
- A final native capture diagnostic also confirmed an enabled, active video
  connection across 52 samples, while the source format remained 0×0 (`isr `).
  It delivered zero frames or distinct hashes. This leaves source activation or
  stream negotiation open; session readiness alone is not evidence of pixels.

## Distribution and acceptance boundaries

A wheel installed into a new environment outside the checkout imported from
site-packages, ran CLI help/doctor, and completed actual stdio MCP initialization,
tool listing, and empty-config devices output without stderr. The wheel was
209,853 bytes in the initial Python 3.12 acceptance build; the full dependency environment occupied about
97 MiB. The optional native runner's isolated environment was about 152 MB.
These measurements exclude Python/uv and existing Apple developer support.

The Codex plugin is a separate repo-contained artifact, not included in the
Python wheel. Nothing has been published to PyPI or GitHub by this acceptance
run. Signing, installation, and DDI setup reused the already prepared device.
The cached free signing profile expires on 2026-09-26 at 17:38:35 UTC; doctor
reports that expiry. Native runtime does not remove the signing requirement.

For this user, the checkout was installed as an editable isolated uv tool,
registered as `apple-tv-agent` in Codex MCP configuration, and its skill copied
to the user's agent skills directory. The installed executable passed an actual
stdio MCP initialization/tool-list/devices smoke check from outside the repo.
This registration applies to future sessions, not the current preloaded tools.

A repeat installed guided setup selected the exact target and matched its prior
stable ID through discovery aliases. It preserved the existing device key, native
helper binding, pairing store, and preferred profile (`primary`). Setup itself labels
reused pairing unverified; the independent live paired-control checks above supply
that evidence. Endpoint refresh additionally has offline tests for changed private
LAN addresses, exact stable-ID aliases, wrong device identity, and local tunnels;
no DHCP change was forced on the household network.

Still open: first-user visual onboarding, renewal without full Xcode, sustained
reliability, other hardware/OS versions, arbitrary exact-episode workflows,
cross-provider availability resolution, first-purchase App Store prompts, and actual
headphone connection (deferred at the user's request). Projector/receiver picture
controls beyond Apple TV output settings require their own device integration.

## Final software checks

- 416 non-live automated tests passed; Ruff, strict mypy (56 source files), and
  Git whitespace checks passed. macOS and Linux CI are configured but have not run
  remotely for this unpushed working tree.
- Final wheel built successfully, installed into an isolated Python 3.12
  environment outside the checkout, imported from that environment's
  site-packages, and completed real stdio MCP initialization, all six tool
  declarations, and empty-config discovery with no stderr. The first verifier
  assertion needed canonical path resolution for macOS `/var` versus
  `/private/var`; the package import itself was correct.
- Process cleanup tests include a real subprocess whose child ignores TERM and
  outlives its launcher. Group ownership survives a dead launcher, and shutdown
  escalates before releasing the lock. The final installed bridge also left
  zero native-runner processes and an unreachable helper endpoint on exit.
- Plugin and skill validation passed; the updated skill was synced locally.

## On-demand access follow-up

The owner rejected scheduled maintenance. The temporary Codex automation was
deleted; no background renewal was left running. Each `devices` invocation now
reads local cached profile evidence afresh and distinguishes healthy, due within
48 hours, expired, and unknown access. It explicitly does not claim to check an
Apple account login. The installed stdio MCP returned `valid` for the current
profile in 0.052 seconds without device inputs. The skill uses this at the start
of each requested task and supplies bounded reprovision/sign/install/verify
recovery when visual access needs it. An actual expiry-advancing renewal remains
unverified; Apple sign-in or 2FA can require a user step.

After these changes and privacy cleanup, 419 tests, Ruff, strict mypy, plugin/skill
validation, and whitespace checks passed. The 210,923-byte wheel passed isolated
Python 3.12 import and real stdio MCP startup/tool-list/empty-config checks. The
GitHub preview is an experimental source snapshot, not a PyPI or production release.

The historical private GitHub preview carried this source snapshot. Initial CI
exposed ANSI-colored CLI assertions,
a missing zsh dependency for a macOS Shortcut test, and a native-platform assumption
in the process-group test. These were corrected: assertions strip formatting, the
shell test requires its interpreter, and the process-group regression uses owned
POSIX children and explicit reaping. Those preview CI results remain in the private archive;
CI coverage does not establish Linux hardware onboarding.
