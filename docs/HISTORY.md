# Earlier experiments

CouchPilot now ships only the Apple TV agent. The former room/scene controller, Sony/Android TV and Sonos adapters, Siri HTTP broker, iPhone App Intents, Shortcut builder, and unused Apple TV OCR/capture experiments were removed from the active tree and package.

Use `apple-tv-agent` and `apple-tv-agent-mcp`. The former `home-media`, `home-media-mcp`, `home-media-broker`, and `home-media-shortcut` commands are retired. The Python distribution/import name and private configuration directory remain `home-media` / `home_media` for compatibility with existing installations. The optional legacy `screenshot` extra is also retired; managed WDA continues to use its separate, pinned native launcher.

Existing `agent.yaml`, paired credentials, and WDA settings are preserved. After updating an existing checkout, run `uv tool install --force --editable .` to replace the installed commands and remove retired entry points. Existing system services installed manually from old deployment examples are not managed by the current package.

Dated research and acceptance reports are historical evidence, not a list of currently shipped modules. Their limitations and failed experiments are retained.
