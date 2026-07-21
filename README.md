# Home Media Control

Local-first, room-aware control for Apple TV, physical televisions, and Sonos.
Deterministic Python core with a CLI and a thin stdio MCP server over the same
typed operations.

## Status (2026-07-20)

- Package under `src/home_media/` with fakes, live adapters, and automated tests.
- Passive rediscovery: **5 Apple TVs** on tvOS 26.5; Theater Sony + Sonos present.
- **Living Room** is Companion-paired (live). Theater pairing remains a separate human gate.
- **Living Room Netflix `search_ready` is live verified** from Apple TV Home through
  exact query readback, with screenshot/OCR state observation and no result Select.
  See `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`.
- Content routing is a **provider-aware ladder** (deep link → Apple Search when
  covered → Netflix state machine → handoff). See `docs/PROVIDER_AUTOMATION_PLAN.md`.
- Raw remote input is debug-gated; routine operation uses semantic commands.
- Result selection, playback/resume, app installation, and universal TV settings
  remain outside the verified slice.

## Quick start

```bash
uv sync --extra dev
cp config/homes.example.yaml ~/.config/home-media/homes.yaml
chmod 700 ~/.config/home-media && chmod 600 ~/.config/home-media/homes.yaml

uv run home-media --json --fakes rooms list
uv run home-media --json --fakes content prepare --room living_room --title "Avatar" --provider netflix
uv run home-media --json content prepare --room living_room \
  --title "Avatar: The Last Airbender" --provider netflix --goal search_ready
uv run home-media-mcp                                # stdio MCP (set HOME_MEDIA_CONFIG)
```

Live CLI without `--fakes` **fails closed** if `homes.yaml` is missing.

See `docs/INSTALL.md` for pairing, MCP config, and security notes.

## Architecture (short)

```text
CLI / MCP → ApplicationService → planner/executor / prepare_content
                              ↘ room registry (stable IDs)
                              ↘ provider route ladder (Netflix first)
```

- Apple TV: `pyatv` 0.18 (Companion / AirPlay; connection reuse + discovery TTL)
- Sonos: SoCo in-process (exact volume + topology)
- Sony: `androidtvremote2` (optional HA later)
- Content: allowlisted https + Netflix `nflx://` title links; search_ready never selects

## Docs

1. `CODEX_HANDOFF_CURRENT.md` (authoritative continuation prompt)
2. `docs/MAC_MINI_CUTOVER_STATUS.md` (current runtime truth)
3. `docs/MAC_MINI_DEPLOYMENT.md` (source/runtime cutover)
4. `docs/PRODUCT_SPEC.md`
5. `docs/PROVIDER_AUTOMATION_PLAN.md` (content routing)
6. `docs/REMEDIATION_PLAN.md` (15 foundational repairs)
7. `docs/ARCHITECTURE.md`
8. `docs/ACCEPTANCE_TESTS.md`
9. `docs/DECISION_LOG.md`
10. `docs/CAPABILITY_REPORT.md`
11. `docs/INSTALL.md`
12. `docs/LIVING_ROOM_NETFLIX_LIVE_GATE.md`

Private LAN inventory lives under `discovery/` and is gitignored.

## Safety

Never commit credentials, PINs, HA tokens, or provider sessions. pyatv storage
defaults to `~/.config/home-media/pyatv.conf` (0600). Power off and volume above
ceiling require explicit confirmation/override. Kill switch: `mutations_enabled: false`.
Raw remote CLI and MCP tools require `HOME_MEDIA_ENABLE_RAW_REMOTE=1`. Pairing PIN is
prompted interactively — never via `--pin` argv.
