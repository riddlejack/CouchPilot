# Home Media App Intent source

These files are the narrow system-integration module for a future iOS app target.
They intentionally contain one background intent, not a screen-by-screen taxonomy:

- `ControlHomeMediaIntent` accepts the user's sentence and returns the broker's honest spoken
  result.
- `HomeMediaClient` calls only `POST /v1/intent`, creates a unique idempotency key, never retries,
  and reads the bearer token from the iOS Keychain.
- Its ephemeral URL session uses a 90-second request timeout and a 95-second resource timeout. Both
  sit outside the broker's 80-second HTTP boundary so the broker, not the client, owns the final
  mutation result. Do not shorten these or add an automatic retry.
- The visible app only needs a one-time setup screen that validates the broker base URL, writes the
  token with `BrokerTokenStore.replace(with:)`, and requests Local Network permission. Store only
  the origin in `homeMediaBrokerURL`, for example `http://home-media.local:8744`; the client
  appends `/v1/intent` itself. The generated Shortcut uses the full endpoint instead, so its import
  JSON must not be copied verbatim into this UserDefaults key.

Add both Swift files to an iOS application target that links `AppIntents` and `Security`. For the
current plain-HTTP `.local` prototype, add these entries to the target's Info.plist; do not enable
arbitrary HTTP loads:

```xml
<key>NSLocalNetworkUsageDescription</key>
<string>Home Media connects to your private TV controller on your home network.</string>
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key>
  <true/>
</dict>
```

A trusted HTTPS/Tailscale URL removes the ATS local-network exception. A direct `.local` URL does
not require Bonjour service declarations because this client does not browse or advertise a
Bonjour service.

The source is an implementation artifact, not a checked-in Xcode project. There is currently no
app target, app entry point, setup screen, provisioning configuration, or installable archive.
It still needs an Xcode 27/iOS 27 compile and on-device Siri phrase test before it can be called
shipped.
