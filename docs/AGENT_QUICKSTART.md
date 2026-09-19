# CouchPilot quickstart

Status: experimental local-checkout packaging, 2026-09-19. The package is not published on PyPI and the plugin is a repo artifact, so install from a checkout or a locally built wheel that contains this code. No OpenAI, Anthropic, or other model API key is required by the bridge.

## Choose the two capability routes

The direct route is the default. It uses a stable discovery ID plus one-time Companion pairing for status, installed apps, wake, sleep, transport, explicit remote buttons, URL/app launch, and text entry. It does not see the screen.

The optional visual route connects to a correctly signed WebDriverAgent (WDA) helper for screenshots, semantic labels, focus, and UI actions. The helper's device identity is pinned when configured. Apple still requires the helper to be signed and provisioned; a free Personal Team profile lasts seven days.

## Install and run guided setup

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are required.

```sh
git clone https://github.com/riddlejack/CouchPilot.git
cd CouchPilot
uv tool install --editable .
apple-tv-agent setup
apple-tv-agent doctor
```

`setup` discovers local tvOS devices, asks you to select and name one, and handles Companion pairing through an on-screen code. AirPlay metadata pairing is optional. Re-running setup preserves existing visual-helper settings and the configured country, subscriptions, and preferred profile; this preservation was verified on the tested target. Configuration defaults to `~/.config/home-media/agent.yaml`; set `APPLE_TV_AGENT_CONFIG` to use another private path. Treat that file and the pyatv credential store as secrets.

To test a non-editable distribution without publishing, build and install a local wheel:

```sh
uv build --no-sources
uv tool install --force dist/home_media-*.whl
```

## Updating an existing installation

After pulling changes, run `uv tool install --force --editable .`. Existing Apple TV pairing and `agent.yaml` configuration remain in place. Only the Apple TV agent commands are shipped now; see [the archive note](HISTORY.md) if you used the earlier room controller or Siri broker.

## Register the MCP server

The guided setup prints these commands. Both clients launch the same installed stdio server:

```sh
codex mcp add apple-tv-agent -- apple-tv-agent-mcp
claude mcp add apple-tv-agent -- apple-tv-agent-mcp
```

The repo-contained plugin is in `plugins/apple-tv-agent`. It is not installed by the Python package and does not modify a personal plugin marketplace. Its MCP declaration expects `apple-tv-agent-mcp` on `PATH`.

Other clients can use this portable stdio declaration:

```json
{
  "mcpServers": {
    "apple-tv-agent": {
      "command": "apple-tv-agent-mcp",
      "args": []
    }
  }
}
```

The server exposes six tools:

| Tool | Purpose |
| --- | --- |
| `devices` | List configured keys and available routes without private identifiers. |
| `observe` | Return fresh text state and an observation receipt; images are opt-in. |
| `act` | Perform one visual action and return the next observation. |
| `status` | Read power and now-playing metadata through paired control. |
| `apps` | List installed apps and bundle IDs. |
| `control` | Wake, sleep, transport, press an explicit remote button, open URLs, launch apps, or type through pairing. |

For UI input, call `observe`, pass its fresh `observation_id` to `act`, and verify the goal from the resulting observation. A receipt is consumed before dispatch so an uncertain mutation is not blindly replayed. `select` accepts only one exact, unique semantic match that is already focused; it sends one remote Select input and rejects an unfocused match without mutating the device. Move focus with one bounded direction at a time and reobserve before Select.

## Manual direct configuration

Use the individual commands when scripting setup or modifying an existing configuration:

```sh
apple-tv-agent discover
apple-tv-agent configure living-room \
  --name "Living Room" \
  --stable-id '<stable_id_from_discover>'
apple-tv-agent pair living-room
apple-tv-agent preferences --country US --subscription Netflix --profile primary
apple-tv-agent doctor
```

Pairing prompts for the on-screen code without placing it in shell history. Installed apps are a device inventory; they do not prove that a user has a subscription or that a title is available.

## Add visual observation when needed

An already running WDA can be pinned by its private endpoint:

```sh
apple-tv-agent configure living-room \
  --name "Living Room" \
  --stable-id '<stable_id_from_discover>' \
  --endpoint 'http://<private-lan-address>:8100'
```

On an already paired Mac with developer support mounted, the native backend starts a previously installed signed helper through an isolated, pinned `pymobiledevice3` 11.15.5 subprocess:

```sh
apple-tv-agent configure living-room \
  --name "Living Room" \
  --stable-id '<stable_id_from_discover>' \
  --endpoint 'http://<private-lan-address>:8100' \
  --udid '<developer_device_id>' \
  --backend native \
  --bundle-id '<installed_wda_runner_bundle_id>'
```

The default launcher is `uvx --from pymobiledevice3==11.15.5`, so first use may download that GPL-3.0-or-later tool into uv's cache. The core package does not import or redistribute it. An optional matching `--xctestrun` lets `doctor` audit the cached signature and profile expiration; native startup itself uses the installed bundle ID.

The Xcode backend can instead start an already built and signed helper with cached `.xctestrun` products:

```sh
apple-tv-agent configure living-room \
  --name "Living Room" \
  --stable-id '<stable_id_from_discover>' \
  --endpoint 'http://<private-lan-address>:8100' \
  --udid '<developer_device_id>' \
  --backend xcode \
  --xctestrun '/private/path/to/WebDriverAgentRunner_tvOS.xctestrun' \
  --developer-dir '/Applications/Xcode.app/Contents/Developer'
```

On the prepared test Mac, the real bridge started the installed helper on first observation, returned semantic state, stopped its owned helper on MCP shutdown, and started it again in a fresh MCP session. Sleep/wake and subsequent visual observation also worked in one bridge session. This runtime invoked no `xcodebuild`, Appium, `sudo`, or `DEVELOPER_DIR`.

The result still reused an already paired Mac, mounted developer support, and a correctly signed helper. It does not prove fresh-device setup, Developer Disk Image setup, helper installation, signing renewal, or operation after the profile expires. The guided `setup` command configures direct control; it preserves an existing visual binding but does not install, sign, or reverify WDA.

## Free signing renewal

Free profiles can be reprovisioned. For an owner who accepts periodic renewal,
[the maintenance workflow](../plugins/apple-tv-agent/skills/apple-tv-agent/references/free-signing.md)
checks expiry and rebuilds, signs, installs, and verifies the same helper when due.
The skill checks current access at the start of each user task and attempts repair
when expired or within 48 hours of expiration. No background automation is needed.
The workflow uses full Xcode and a working saved account session. It may need human
sign-in/2FA, and a complete unattended renewal cycle remains unverified.

## Remote and cloud agents

Run the bridge on a machine beside the Apple TV and let the agent call it through an authenticated, user-initiated private connection. The agent host may be macOS, Windows, Linux, or cloud-hosted; the local bridge still owns LAN discovery, pairing secrets, and any signed helper lifecycle. Do not publish WDA port 8100, MJPEG port 9100, RemoteXPC, or raw device discovery to the internet.

This package exposes stdio MCP. It does not bundle a remote transport, authentication service, or public endpoint. A remote deployment must provide its own authenticated private connection to the local stdio bridge.

See [COMPATIBILITY.md](COMPATIBILITY.md) for verified boundaries, [the live acceptance record](../research/AGENT_ACCEPTANCE_2026-09-19.md) for the demonstrated workflows, and [the access assessment](../research/ACCESS_AND_DISTRIBUTION_2026-09-19.md) for the primary-source basis.
