# Acceptance and verification plan

No feature is complete merely because a function exists. Completion requires
the appropriate automated proof and, where applicable, authorized live proof.

## Evidence vocabulary

- **Automated:** proven against unit/contract tests or recorded fixtures.
- **Live verified:** executed against the named physical device with an
  observable postcondition.
- **Degraded:** action was accepted but the requested end state could not be
  observed or only relative semantics exist.
- **Untested:** implemented but not exercised on the household device.
- **Unsupported:** the adapter or target cannot provide the requested semantic.

Final reports must use these labels explicitly.

## Automated gates

### Registry and identity

- [ ] Parses valid home/room/device configuration.
- [ ] Rejects duplicate stable IDs and ambiguous room aliases.
- [ ] Treats observed IP changes as endpoint updates, not new devices.
- [ ] Filters Mac and Sonos AirPlay advertisements from Apple TV inventory.
- [ ] Resolves `Theater` room to distinct Apple TV, physical TV, and audio targets.
- [ ] Never resolves a device mutation from a friendly name alone.

### Planner and safety

- [ ] Every mutation fails without an explicit, unique room.
- [ ] Dry-run emits the exact planned targets and performs no adapter mutation.
- [ ] Volume ceiling and override behavior are tested at boundaries.
- [ ] Multi-room mutation requires an explicit higher-risk intent.
- [ ] Factory reset, calibration, account purchase, and installation operations
      are unavailable or confirmation-gated.
- [ ] Idempotency prevents duplicate execution on agent retry.
- [ ] Per-room locks serialize mutations but not safe reads.

### Adapter contracts

- [ ] Unsupported capability, authentication failure, network timeout, device
      rejection, and verification failure remain distinguishable.
- [ ] Connections time out and retry only bounded transient failures.
- [ ] Endpoint rediscovery occurs after stale-address failure.
- [ ] Partial scene completion returns step-level results.
- [ ] Credentials and tokens are redacted from logs and exceptions.

### CLI and MCP parity

- [ ] The CLI and MCP invoke the same application service methods.
- [ ] JSON outputs conform to versioned schemas.
- [ ] Exit/error semantics are predictable.
- [ ] MCP exposes no generic shell or raw arbitrary-protocol tool.
- [ ] stdio server starts, lists tools, validates inputs, and shuts down cleanly.

### Content behavior

- [ ] Direct link preserves the target provider and expected app.
- [ ] Saved aliases resolve deterministically.
- [ ] Provider uncertainty lowers result confidence.
- [ ] `resume` cannot return exact-progress success without evidence.
- [ ] Raw AirPlay streaming and app/deep-link launch have separate error paths.

## Passive live discovery gate

- [ ] Current network/interface recorded without exposing credentials.
- [ ] Five known Apple TVs rediscovered or exact drift explained.
- [ ] Actual tvOS devices distinguished from Macs and Sonos.
- [ ] Theater Sony and Sonos endpoints rediscovered before use.
- [ ] No pairing, wake, navigation, playback, or volume mutation occurred.

## Theater Apple TV live gate

All tests below require the user to be present and approve the category.

- [ ] Companion/current-tvOS pairing succeeds and credentials are stored outside
      the repository.
- [ ] Status call succeeds after reconnecting from stored credentials.
- [ ] Installed apps list returns live bundle IDs.
- [ ] One agreed app opens and current app/now-playing evidence is recorded.
- [ ] Play/pause or a harmless remote key is observed on the correct device.
- [ ] `turn_on` wakes Apple TV.
- [ ] User reports whether the physical Sony TV also wakes via HDMI-CEC.
- [ ] Physical-TV power result is not inferred solely from Apple TV response.
- [ ] Power off is tested only after an explicit same-turn approval.

## Content live gate

For each prioritized provider:

- [ ] User supplies or approves one non-sensitive share/deep link.
- [ ] Correct provider app opens on Theater Apple TV.
- [ ] Expected title/episode is verified from now-playing metadata when possible.
- [ ] Resume/progress behavior is described exactly as observed.
- [ ] App home-page-only fallback is not labeled content success.

## Living Room Netflix search-ready gate (completed 2026-07-20)

- [x] Exact Living Room observer binding resolves before launch.
- [x] Apple TV Home precondition is captured and independently inspected.
- [x] Netflix launches from Apple TV Home.
- [x] A restored different query is classified as reusable search results.
- [x] The requested title replaces the prior query.
- [x] Real keyboard focus and exact text readback verify the request.
- [x] Final screenshot visibly shows the requested query and results.
- [x] `terminal_status=query_verified` and `verification_status=verified`.
- [x] `selected_result=false` and `playback_started=false`.
- [x] No user screen narration is required.
- [x] Raw remote/text tools remain disabled unless explicitly debug-enabled.

Evidence: `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`.

## Sonos live gate

- [ ] Sonos-native discovery identifies the real Theater zone/coordinator.
- [ ] Current volume is read.
- [ ] A user-approved safe level is set exactly.
- [ ] Read-after-write matches within documented tolerance.
- [ ] A grouped/bonded component is not mistaken for a user-facing zone.
- [ ] Failure to set exact volume never degrades silently to arbitrary steps.

## Sony physical-TV live gate

- [ ] Theater TV identity and current endpoint are confirmed.
- [ ] Pairing/authentication happens with the user present.
- [ ] Power state is read independently of Apple TV.
- [ ] Input list/state is read before changing it.
- [ ] A user-approved input change succeeds and is verified.
- [ ] CEC wake behavior is compared with direct Sony state.
- [ ] No picture calibration, service menu, reset, or account setting is touched.

## End-to-end scene

With the user present:

```text
Turn on the Theater TV, open an agreed streaming target, and set Theater audio to an
agreed safe volume.
```

Pass criteria:

- [ ] Correct room resolved without ambiguity.
- [ ] Apple TV wakes.
- [ ] Physical TV state is verified directly or marked unverified.
- [ ] Correct app/content target opens.
- [ ] Sonos reaches the exact requested level.
- [ ] Each step reports evidence and latency.
- [ ] Retrying the same MCP request does not produce duplicate disruptive work.
- [ ] A simulated failure in one step produces an honest partial result.

## Release checks

- [ ] Fresh environment install succeeds from lockfile.
- [ ] Full automated suite, lint, and type checks pass.
- [ ] Live tests are opt-in and cannot run accidentally in CI.
- [ ] Secret scan is clean.
- [ ] `git status` contains no credential or private runtime files.
- [ ] README covers CLI, MCP, pairing, configuration, security, and limitations.
- [ ] Every room has a generated capability report with one of the evidence
      labels above.
