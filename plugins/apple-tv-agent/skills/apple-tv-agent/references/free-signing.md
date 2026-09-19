# Free helper signing maintenance

Use this workflow when the owner has requested free Personal Team maintenance.
A skill describes an on-demand repair. Before an operation that needs WDA, the
agent checks the helper's profile and either starts the healthy helper or runs one
bounded repair when it is due. Direct Companion control is independent.

## Status and limits

Apple accounts do not expire every seven days. The installed helper's development
provisioning profile expires. Recovery requires provisioning, signing, installation,
and a verified helper launch. A successful sign-in or rebuild alone is insufficient.

This project has verified initial free signing, installation, and repeated native
helper startup. It has NOT yet verified a renewal that advances the expiration
of an existing free profile, or a complete on-demand renewal cycle. Invoking this
workflow makes renewal attempted maintenance, not guaranteed availability.

The current supported maintenance candidate uses full Xcode on the local Mac,
its saved Apple account session and Keychain signing identity, the original WDA
source, and the same device and app identifiers. Runtime can avoid Xcode; this
renewal workflow cannot yet. A sleeping/offline Mac or Apple TV delays it. Apple
may require interactive sign-in, 2FA, or agreement acceptance; report that exact
required step instead of promising automatic login or storing a password here.

## Check on use, then act only when due

1. Run `apple-tv-agent doctor` on the local bridge. Read the configured device and
   helper from its private `agent.yaml`; keep IDs, signing data, and logs local.
2. Compare the cached signed helper's exact expiration timestamp to current UTC.
   More than 48 hours remaining: no renewal is needed. Missing cached evidence:
   report unknown expiration; don't infer health from a successful account login.
3. With at most 48 hours remaining, try one bounded renewal. Reuse the existing
   free Personal Team, bundle ID and target UDID. Do not buy membership, rotate
   accounts or app IDs, revoke certificates, or modify unrelated profiles.
4. Coordinate with existing device/helper ownership. Defer while another bridge
   owns the device or the user is actively watching. Do not kill other sessions.

## Rebuild and verify the candidate

Locate the original trusted WDA source, private signing `.xcconfig`, and full
Xcode developer directory. If missing, report the missing prerequisite. Use a
new private derived-data directory so the current working artifact is retained.
The command shape is:

```sh
DEVELOPER_DIR="$WDA_DEVELOPER_DIR" "$WDA_DEVELOPER_DIR/usr/bin/xcodebuild" \
  -project "$WDA_PROJECT" -scheme WebDriverAgentRunner_tvOS \
  -configuration Debug -destination "id=$WDA_DEVICE_UDID" \
  -derivedDataPath "$WDA_CANDIDATE_DIR" -xcconfig "$WDA_SIGNING_CONFIG" \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  CODE_SIGN_STYLE=Automatic build-for-testing
```

These variables are explicit local inputs, not bundled credentials. Send build
output to a private mode-0600 log; use a bounded process and clean up its exact
owned process group on timeout. Do not dump build logs into the agent transcript.

Inspect the candidate app's signature and decode its `embedded.mobileprovision`
with macOS `security cms`. Verify tvOS, the same team, bundle identifier and
registered target, and an expiration both later than the previous profile and
at least 48 hours ahead. Preserve the original signed artifact until acceptance.

**A rebuilt app with the same expiration is not renewed.** Xcode can reuse cached
profiles. Apple's documented automatic-signing remedy is to back up and remove
unwanted cached provisioning profiles before rebuilding. Apple currently documents
`~/Library/MobileDevice/Provisioning Profiles/`; the prepared Xcode 26/27 installation
stores its cache under `~/Library/Developer/Xcode/UserData/Provisioning Profiles/`.
Inspect those locations only. If needed, back up only profiles positively matched
to this helper's team, app ID and device; never clear all profiles. Retry at most
once and restore backed-up files if no renewal results. A server-returned unchanged
profile remains a failed renewal attempt.

## Install, prove, then record

Install the candidate over the existing helper on the exact configured device;
do not uninstall first. The known-good path on this physical tvOS 26.6 target is
the generated `.xctestrun`. During live acceptance this command installed and
started the runner, so treat it as a combined install-and-launch gate:

```sh
DEVELOPER_DIR="$WDA_DEVELOPER_DIR" "$WDA_DEVELOPER_DIR/usr/bin/xcodebuild" \
  -xctestrun "$WDA_CANDIDATE_XCTESTRUN" \
  -destination "id=$WDA_DEVICE_UDID" test-without-building
```

The test command remains in the foreground while its WDA process runs. Own it with
the existing bounded process manager, wait for ready status, confirm the previously
pinned WDA device identity, then stop that exact owned process. Start the installed
helper again through the existing native runtime and obtain a fresh observation
(an independently returned screenshot is sufficient when the screen has no
semantic labels). Stop the owned helper after the check.

Only after those checks update the private configuration's `xctestrun_path` to the
accepted candidate, preserving all other fields and mode 0600. Record old/new
expiry, candidate location, installation result, identity check and observation
result in a private maintenance record. If launch or verification fails, retain
the old artifact/config and report precisely which step failed; reinstall the
prior still-valid build where possible. Do not claim success based on build exit 0.

## Invocation and notification

Check when a requested operation needs WDA; do not reinstall on every use. An agent
running this workflow should remain quiet on unchanged healthy state, record
successful maintenance locally, and notify for actual renewal, approaching expiry
with failed renewal, or required user action. One bounded attempt per invocation
avoids a login/build retry loop. A public package still needs fresh-user onboarding
and multi-cycle reliability evidence before describing this as unattended support.

Sources: [Apple Personal Team limits](https://developer.apple.com/help/account/basics/about-your-developer-account),
[Apple profile regeneration and automatic-signing cache behavior](https://developer.apple.com/help/account/provisioning-profiles/edit-download-or-delete-profiles),
and [Appium's current tvOS WDA build command](https://github.com/appium/appium-xcuitest-driver/blob/master/docs/getting-started/provisioning-profile/generic-device-config.md).
The provisioning flag semantics were also checked against the installed Xcode 27
`xcodebuild -help` output; the initial install-and-launch result is recorded in
the repository's physical-device report.
