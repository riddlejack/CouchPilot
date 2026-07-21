# Installation and onboarding

## Portable Mac (same LAN)

```bash
cd "/path/to/apple tv control"
uv sync --extra dev
cp config/homes.example.yaml ~/.config/home-media/homes.yaml
chmod 700 ~/.config/home-media
chmod 600 ~/.config/home-media/homes.yaml
```

Optional environment overrides:

- `HOME_MEDIA_CONFIG` — path to homes.yaml
- `HOME_MEDIA_PYATV_STORAGE` — pyatv credential file (default `~/.config/home-media/pyatv.conf`)
- `HOME_MEDIA_CREDENTIALS_DIR` — Android TV certs and other secrets
- `HOME_MEDIA_USE_FAKES=1` — in-memory adapters for demos/tests

## CLI

```bash
uv run home-media --json discover
uv run home-media --json rooms list
uv run home-media --json status --room theater
uv run home-media --json volume set --room theater --level 20 --dry-run
```

## Pairing (interactive, same process)

Prefer one command so the pairing handler stays in memory:

```bash
uv run home-media --json pair interactive --room theater --protocol companion
```

You will be prompted for the on-screen PIN (hidden input). Do **not** pass a PIN
on the command line. Split `pair start` / `pair finish` only works inside one
long-lived process (e.g. MCP lifespan); across separate CLI processes it fails.

Credentials land in `~/.config/home-media/pyatv.conf` (mode 0600), never in Git.

Live note: Living Room is already Companion-paired. Theater remains a human pairing gate.

## Living Room screenshot observer

The live Netflix state machine requires a capture-proven binding for the exact
Apple TV. Living Room is already configured locally. Ordinary use is:

```bash
uv run home-media --json content prepare --room living_room \
  --title "Avatar: The Last Airbender" --provider netflix --goal search_ready
```

The observer uses the separately installed Python 3.14 `pymobiledevice3`
environment through a subprocess boundary, then local macOS Vision OCR. It does
not send screenshots to a hosted vision model. Other rooms must be paired and
bound individually; never copy Living Room's observer identifier to another room.

Raw diagnostic input is intentionally off by default. Enable
`HOME_MEDIA_ENABLE_RAW_REMOTE=1` only for a bounded debug command, not for
normal agent/MCP operation.

## MCP (Cursor / Codex)

stdio only. Example Cursor MCP config fragment:

```json
{
  "mcpServers": {
    "home-media": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/apple tv control", "home-media-mcp"],
      "env": {
        "HOME_MEDIA_CONFIG": "/Users/YOU/.config/home-media/homes.yaml"
      }
    }
  }
}
```

Do not bind an unauthenticated HTTP MCP server to the LAN.

## Security

- No PINs, tokens, cookies, or Apple Account material in the repository.
- Audit logs redact secrets.
- Volume ceiling default 40; override requires an explicit flag.
- Power off requires `--confirm` / `confirm_power_off=true`.
- Kill switch: `mutations_enabled: false` in homes.yaml.
- Never install on `home-media` until explicitly authorized.

## Limitations

- No universal Apple TV resume/watch-history API.
- No remote app installation via pyatv.
- Exact volume requires Sonos/absolute route; CEC is relative only.
- Apple TV wake does not prove the physical TV is on.
- Deep link open does not prove the exact episode/progress without now-playing evidence.
- Netflix `search_ready` is live verified only on Living Room today; result Select
  and playback/resume remain intentionally unsupported.
