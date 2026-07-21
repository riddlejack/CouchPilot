# Mac mini cutover status

Updated: 2026-07-20 20:30 America/Chicago

## Verdict

The Mac mini is ready for autonomous code work and read-only Home Media control.
It is not yet the verified screenshot/navigation runtime because the transferred
pymobiledevice3 remote-pair record does not expose Apple TV tunnel services on
this host. Mutations remain disabled.

## Completed and verified

- Private GitHub repository created with `main` at initial commit `34fcd3a`.
- Repository visibility independently verified as private.
- The committed source was sanitized before history was created:
  - no credentials, screenshots, discovery inventory, live reports, virtual
    environments, or build artifacts;
  - no real household Apple TV UUIDs, Sonos RINCON IDs, or LAN addresses;
  - fake mode now uses a sanitized deterministic seed unless an explicit config
    path is supplied, and fake adapters derive their device set from that config.
- Mac mini checkout created from GitHub with a repository-scoped, write-enabled
  deploy key. No broad GitHub token was copied to the host.
- User-local toolchain installed:
  - uv 0.11.30;
  - CPython 3.12.13 for Home Media;
  - CPython 3.14.6 plus pymobiledevice3 9.36.2 for tvOS Wi-Fi screenshots.
- Private runtime files transferred directly over encrypted SSH with mode 0600:
  home configuration, pyatv credentials, observer binding, and one
  pymobiledevice3 remote-pair record.
- Mac mini `mutations_enabled` is explicitly `false`; the source-machine copy is
  unchanged and remains the rollback runtime.
- Mac mini code-only gate passes: 108 tests, Ruff, mypy, source build, and wheel
  build.
- Mac mini read-only live gate passes:
  - six configured rooms resolve exactly;
  - five Apple TVs rediscovered;
  - Living Room Apple TV status authenticated;
  - Living Room Sonos status present;
  - no Living Room device errors;
  - exactly one redacted screenshot binding resolves to Living Room.

Private command outputs are retained under the Mac mini Home Media configuration
directory, not in Git.

## Current blocker: screenshot host pairing

The Mac mini screenshot command reached the correct capture backend but returned:

```text
no_remote_pairing_tunnel_service
```

Evidence separating the failure layers:

- source Mac: 12 remote-pairing tunnel advertisements total, 6 matching the
  exact Living Room observer binding;
- Mac mini: 0 remote-pairing tunnel advertisements total;
- ordinary pyatv discovery and authenticated Living Room status work on the Mac
  mini, so general LAN reachability and Apple TV control credentials are good;
- therefore the missing capability is the host-specific screenshot/developer
  pairing relationship, not room resolution, pyatv authentication, OCR, or the
  provider state machine.

## Next human gate

The user must be physically present at Living Room for one fresh Mac mini
pymobiledevice3 remote pairing. The receiving agent should:

1. perform every read-only preflight first;
2. ask one question: confirm Living Room is visible and it is safe to display a
   pairing code;
3. initiate pairing for exactly Living Room;
4. request the displayed code once;
5. save the new Mac mini pairing record privately;
6. capture one screenshot and verify it is nonblank;
7. keep mutations disabled until that passes.

Do not copy the old observer identifier to a different room, try other Apple TVs,
or compensate with blind remote presses.

## Work that can continue before the human gate

- establish a `codex/mac-mini-runtime` branch;
- add a tested persistent capture-worker design with fail-closed exact-device
  binding and graceful shutdown;
- add stage timing/health instrumentation;
- implement the restricted typed-JSON SSH wrapper using fakes;
- design the singleton local hub API without binding it to the LAN;
- update install/service documentation and tests;
- investigate the current Mac mini Codex runtime and ensure `~/.local/bin` is in
  the execution PATH without weakening system security.

Do not install a LaunchAgent, open a listener, enable live mutations, or run a
Netflix navigation sequence until the relevant explicit gate.

