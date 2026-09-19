# Home Media Control

## Apple TV Agent — current route (2026-09-19)

This checkout contains an experimental, agent-neutral Apple TV bridge and a repo-contained Codex plugin. Install it from this checkout, run the guided `apple-tv-agent setup`, and register the installed `apple-tv-agent-mcp` command with Codex, Claude Code, or another stdio MCP client. The bridge exposes six tools and needs no separate model API key. It is not published on PyPI or as an installable plugin.

### Current quickstart

```sh
git clone --branch codex/apple-tv-agent-preview https://github.com/riddlejack/CouchPilot.git
cd home-media-control
uv tool install --editable .
apple-tv-agent setup
apple-tv-agent preferences --country US --subscription Netflix --profile primary
apple-tv-agent doctor
codex mcp add apple-tv-agent -- apple-tv-agent-mcp
# Or: claude mcp add apple-tv-agent -- apple-tv-agent-mcp
```

See [the full agent quickstart](docs/AGENT_QUICKSTART.md) for visual-helper configuration, local-wheel installation, and remote-agent boundaries.

### Current verified scope

The real MCP bridge has a bounded live acceptance on one Apple TV 4K (2nd generation) running tvOS 26.6. Direct pairing, app inventory, sleep/wake, Netflix profile/search/episode preparation, pause verification, and a reversible picture-setting change were demonstrated. The optional native visual route launched a previously installed signed WDA helper on demand, survived a fresh-server restart, and stopped its owned helper on shutdown without Appium, `xcodebuild`, `sudo`, or `DEVELOPER_DIR`. An official App Store URL also opened the correct VLC product page; the existing account completed a free redownload and launched VLC.

The guided setup also completed against the target while preserving its existing visual-helper configuration and preferred profile. Visual `select` now activates only one exact, unique label that is already focused; it sends one remote Select input and rejects an unfocused target before mutation.

This remains a prepared-device result: it reused existing pairing, mounted developer support, and a helper with a seven-day Personal Team profile. It does not establish fresh-device visual onboarding, signing renewal without full Xcode, sustained reliability, or compatibility beyond the tested target. Check [the compatibility matrix](docs/COMPATIBILITY.md) and [live acceptance record](research/AGENT_ACCEPTANCE_2026-09-19.md). The [access assessment](research/ACCESS_AND_DISTRIBUTION_2026-09-19.md) explains the provisioning and distribution boundary.

Free-profile renewal can be handled on demand by the agent using the
[maintenance workflow](plugins/apple-tv-agent/skills/apple-tv-agent/references/free-signing.md).
It checks access at the start of each user task, then reprovisions, signs,
reinstalls, and verifies the helper when needed. There is no background scheduler. This still uses Xcode and may require Apple sign-in/2FA; a complete
unattended renewal cycle has not yet been demonstrated.

---

## Legacy room-control workflow (2026-07-21)

The remainder preserves the earlier room-oriented Home Media workflow and its July 2026 evidence. Its status, quickstart, and document ordering are historical; use the Apple TV Agent route above for the current setup.

Local-first, room-aware control for Apple TV, physical televisions, and Sonos.
Deterministic Python core with a CLI and a thin stdio MCP server over the same
typed operations.

### Historical status (2026-07-21)

- Package under `src/home_media/` with fakes, live adapters, and automated tests.
- Passive rediscovery: **5 Apple TVs** on tvOS 26.5; Theater Sony + Sonos present.
- **Living Room** is Companion-paired (live). Theater pairing remains a separate human gate.
- **Living Room Netflix `search_ready` is live verified** from Apple TV Home through
  exact query readback, with screenshot/OCR state observation and no result Select.
  See `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`.
- Content routing is a **provider-aware ladder** (deep link → Apple Search when
  covered → Netflix state machine → handoff). See `docs/PROVIDER_AUTOMATION_PLAN.md`.
- Raw remote input is debug-gated; routine operation uses semantic commands.
- The Mac mini branch now includes a process-scoped hub, redacted health,
  persistent exact-device screenshot worker, exact-frame Computer Use, an
  authenticated Siri broker, and a Fast GPT-5.6-low visual fallback.
- Exact-title selection and resume-then-pause are code-verified and live-verified
  on Living Room. The broker returned `playback_paused_verified` in 35.3 seconds,
  with an independent `idle` status five seconds later. Shortcut import/iPhone
  Siri delivery, app installation, and universal physical-TV settings remain
  outside the verified slice.

### Legacy quick start

```bash
uv sync --extra dev
cp config/homes.example.yaml ~/.config/home-media/homes.yaml
chmod 700 ~/.config/home-media && chmod 600 ~/.config/home-media/homes.yaml

uv run home-media --json --fakes rooms list
uv run home-media --json health                     # local, non-network startup health
uv run home-media --json --fakes content prepare --room living_room --title "Avatar" --provider netflix
uv run home-media --json content prepare --room living_room \
  --title "Avatar: The Last Airbender" --provider netflix --goal search_ready
uv run home-media-mcp                                # stdio MCP (set HOME_MEDIA_CONFIG)
```

Live CLI without `--fakes` **fails closed** if `homes.yaml` is missing.

See `docs/INSTALL.md` for pairing, MCP config, and security notes.

### Legacy architecture (short)

```text
CLI / MCP → ApplicationService → planner/executor / prepare_content
                              ↘ room registry (stable IDs)
                              ↘ provider route ladder (Netflix first)
```

- Apple TV: `pyatv` 0.18 (Companion / AirPlay; connection reuse + discovery TTL)
- Sonos: SoCo in-process (exact volume + topology)
- Sony: `androidtvremote2` (optional HA later)
- Content: provider-aware search, exact-frame title selection, and verified
  resume-then-pause; `search_ready` itself never selects

### Legacy docs

1. `CODEX_HANDOFF_CURRENT.md` (historically authoritative July continuation prompt)
2. `docs/MAC_MINI_CUTOVER_STATUS.md` (July runtime record)
3. `docs/MAC_MINI_DEPLOYMENT.md` (source/runtime cutover)
4. `docs/PRODUCT_SPEC.md`
5. `docs/APPLE_TV_COMPUTER_USE.md` (observe/action loop and verified fast paths)
6. `docs/SIRI_BROKER_RUNBOOK.md` (Shortcut, resident broker, and App Intent setup)
7. `docs/PROVIDER_AUTOMATION_PLAN.md` (content routing)
8. `docs/REMEDIATION_PLAN.md` (15 foundational repairs)
9. `docs/ARCHITECTURE.md`
10. `docs/ACCEPTANCE_TESTS.md`
11. `docs/DECISION_LOG.md`
12. `docs/CAPABILITY_REPORT.md`
13. `docs/INSTALL.md`
14. `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`

Private LAN inventory lives under `discovery/` and is gitignored.

### Legacy safety

Never commit credentials, PINs, HA tokens, or provider sessions. pyatv storage
defaults to `~/.config/home-media/pyatv.conf` (0600). Power off and volume above
ceiling require explicit confirmation/override. Kill switch: `mutations_enabled: false`.
Raw remote CLI and MCP tools require `HOME_MEDIA_ENABLE_RAW_REMOTE=1`. Pairing PIN is
prompted interactively — never via `--pin` argv.
