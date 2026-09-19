# Apple TV observation spike (tvOS 26.5 / macOS 26)

> Historical record: some experimental components mentioned here are now archived. See [the scope change](../docs/HISTORY.md) for the current package and preserved source.

Date: 2026-07-20. This work was read-only: no remote buttons, focus changes,
settings changes, or app launches were sent to an Apple TV.

## Verdict

1. **Ship a persistent AVFoundation/CoreMediaIO frame daemon first.** The Mac
   mini discovers every Apple TV as a wireless screen-capture source in under
   five seconds, including while the Mac mini is on Ethernet with Wi-Fi off.
   Once a capture session is open, frames should arrive at video cadence rather
   than paying the current 1.7-1.9 second DVT setup/capture cost per observation.
2. **Keep the existing warm DVT worker as the supported fallback.** It is the
   only currently proven frame path all the way through the project. It should
   observe state boundaries, not every directional input.
3. **Prototype the tvOS 26 accessibility service in parallel.** A raw DTX
   handshake to `remoteAXService` works and reports device API 26. macOS ships
   Apple's matching private host parser, potentially exposing labels, roles,
   frames, children, and focus without OCR. A live native-host probe completed
   the handshake and cache transport, but did not expose a root tree. This is
   therefore an experimental lane, not the production dependency.
4. **Do not invest further in pymobiledevice3's legacy AccessibilityAudit
   adapter.** Its shim connection/queries time out against this tvOS 26.5 device.
   The current direct service is a different protocol.

## Public real-time frame surface

The system CoreMediaIO plug-in used by QuickTime is:

```
/System/Library/Frameworks/CoreMediaIO.framework/Versions/A/Resources/
  iOSScreenCapture.plugin
```

Its bundle identifier is `com.apple.cmio.DAL.iOSScreenCapture`. A process opts
into its sources by setting these public CoreMediaIO system properties before
building an `AVCaptureSession`:

- `kCMIOHardwarePropertyAllowScreenCaptureDevices`
- `kCMIOHardwarePropertyAllowWirelessScreenCaptureDevices`

The second property defaults to zero and is process-local. The reliable device
surface is `AVCaptureDevice.wasConnectedNotification`: wireless Apple TVs were
delivered as actual `AVCaptureDevice` objects there even though the legacy
`AVCaptureDevice.devices(for: .video)` inventory remained empty. Production
code must retain the notification's device object by `uniqueID`, then use:

```
AVCaptureDeviceInput(device: device)
  -> AVCaptureSession
  -> AVCaptureVideoDataOutput
  -> captureOutput(_:didOutput:from:)
```

The list-only probe is `research/airplay_capture_probe.swift`. The explicit,
opt-in first-frame PoC is `research/airplay_frame_capture_poc.swift`; compiling
it with `swiftc -parse` does not open a TV. Opening a source can trigger the
one-time AirPlay code flow and macOS Screen Recording permission.

The first-frame PoC has deliberately **not** been run live. Its remaining gate
is one observed Living Room run from the Mac mini: grant Screen Recording if macOS
asks, enter the one-time AirPlay code if tvOS asks, and verify a PNG plus warm
frame cadence. Device enumeration alone is proven; actual session creation,
pair persistence, and frame latency are not yet proven.

Production should be a signed LaunchAgent in the logged-in user session, with
a stable bundle identity so TCC and pairing grants persist. Keep one session per
actively controlled room (or a small idle pool), retain the latest pixel buffer
in memory, and let the computer-use loop request the latest frame at no capture
startup cost. FairPlay-protected video content may be black, but app chrome,
menus, search, and playback controls are the navigation surface that matters.

## tvOS 26 semantic accessibility surface

Living Room advertises both:

```
com.apple.accessibility.axAuditDaemon.remoteAXService
com.apple.accessibility.axAuditDaemon.remoteserver.shim.remote
```

The direct service claims `UsesRemoteXPC: true`, but a generic RemoteXPC
handshake is terminated. tvOS 26's daemon actually wraps the accepted socket in
`DTXSocketTransport`. A raw `DTXConnection`, with no capability handshake and a
host control handler for `hostAPIVersion`, connected successfully and returned
`26` from `deviceAPIVersion`.

The protocol surface is small:

- host replies to `hostAPIVersion`
- device replies to `deviceAPIVersion`
- host invokes `clientNeedsAccessibility:`
- device streams `processDataFromRemoteDevice:`
- host sends `processDataFromHost:`

Those payloads are Apple's private AccessibilityPlatformTranslation cache
protocol. Do not reverse-engineer the byte format first. macOS 26 already ships:

```
/System/Library/PrivateFrameworks/AccessibilityAudit.framework
/System/Library/PrivateFrameworks/AccessibilityPlatformTranslation.framework
```

The host entry point is:

```
AXAuditRemoteDevice
  -initWithFileDescriptor:identifier:deviceSize:
  -startAccessibility
  .hostCacheManager
  .accessibilityOverlayView
```

Dynamic loading was also verified on the actual macOS 26 host: both private
frameworks load without an entitlement, all four classes resolve, and Objective-C
runtime introspection reports the expected method encodings. That removes
framework availability as a PoC risk; the unproved part is decoding a live tvOS
tree through the bridged descriptor.

A live bridged probe went further: `AXAuditRemoteDevice` negotiated API version
26, created a non-null `AXPHostCacheManager` and `AXPHostCacheOverlayView`, and
exchanged bytes in both directions (about 1.9 KB host-to-TV and 15.8 KB
TV-to-host in ten seconds). `accessibilityChildren` and the translated app root
still remained empty. The transport and Apple's decoder are active, but the
missing host-side root-publication condition must be understood before this can
be used for policy or focus detection.

`AXPHostCacheOverlayView.accessibilityChildren` yields translated remote
elements. `AXPMacPlatformElement` implements the ordinary macOS accessibility
surface, including `accessibilityLabel`, `accessibilityRole`,
`accessibilityFrame`, `accessibilityParent`, and attribute reads. It also has
action methods; the observer must deliberately expose only reads.

The clean PoC architecture preserves the existing userspace tunnel:

1. Python opens the exact-UDID pmd3 userspace tunnel and raw
   `remoteAXService` byte stream.
2. Python accepts one localhost socket and bridges bytes bidirectionally.
3. A small Objective-C helper passes that connected local socket descriptor to
   `AXAuditRemoteDevice`, calls `startAccessibility`, and emits a bounded,
   read-only JSON tree from the overlay's children.
4. If proved, keep the helper alive and publish semantic snapshots/events to the
   policy loop; never give the model the framework's action selectors.

The raw transport probe is `research/pmd3_ax_transport_probe.py`. It reports
only redacted metadata and protocol shape; it never enables the accessibility
cache or invokes an action. `research/pmd3_ax_tree_bridge.py` and
`research/ax_remote_tree_helper.m` are the bounded read-only native-host PoC;
their current honest terminal state is `remote accessibility tree did not
become ready`.

## Why this beats screenshot-per-arrow control

The controller should use a hybrid feedback loop:

- deterministic app recipes perform known macro transitions quickly;
- the persistent frame stream verifies the resulting screen at state
  boundaries;
- the semantic AX cache, when available, identifies selected elements and
  labels without OCR;
- DVT screenshot capture remains the compatibility fallback;
- the model is invoked only for ambiguity or recovery, not every arrow.

A 15-second title-ready target is realistic only with persistent connections
and macros. An LLM round-trip and a fresh 1.8-second screenshot after each of
four directional inputs consumes the whole latency budget before search text is
entered.

## Primary evidence

- Apple CoreMediaIO wireless screen-capture opt-in:
  https://developer.apple.com/documentation/coremediaio/kcmiohardwarepropertyallowwirelessscreencapturedevices
- Apple CoreMediaIO screen-capture opt-in:
  https://developer.apple.com/documentation/coremediaio/kcmiohardwarepropertyallowscreencapturedevices
- Apple `AVCaptureDevice`:
  https://developer.apple.com/documentation/avfoundation/avcapturedevice
- Apple capture/FairPlay behavior:
  https://developer.apple.com/documentation/uikit/uiscreen/iscaptured
- pymobiledevice3 source:
  https://github.com/doronz88/pymobiledevice3
- tvOS 26 daemon implementation showing DTX transport:
  https://github.com/EthanArbuckle/iPhone18-3_26.1_23B85_Restore/blob/90aa0cfe59d9682b4265e1354c8b19ec3c7823ab/System/Library/PrivateFrameworks/AccessibilityAudit.framework/Support/AccessibilityAudit/AccessibilityAudit.mm
- tvOS 26 remote AX device protocol:
  https://github.com/lechium/Apple_TVOS_26.2_23K51/blob/deb43ca7fcecf7a2eb0ef656075a4a6f5c6fb23e/System/Library/PrivateFrameworks/AccessibilityAudit/AXAuditRemoteDeviceAPIDevice-Protocol.h
- macOS 26.4 `AXAuditRemoteDevice` host header:
  https://github.com/thatmarcel/macOS-26.4-headers/blob/4d5d4f5eba9020ff6bf2879071d565dcce0f4db1/headers/AccessibilityAudit/AXAuditRemoteDevice.h
- macOS 26.4 translated platform element header:
  https://github.com/thatmarcel/macOS-26.4-headers/blob/4d5d4f5eba9020ff6bf2879071d565dcce0f4db1/headers/AccessibilityPlatformTranslation/AXPMacPlatformElement.h
