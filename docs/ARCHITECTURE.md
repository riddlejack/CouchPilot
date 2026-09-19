# CouchPilot architecture

CouchPilot is an Apple TV controller for an existing MCP agent. The agent interprets the request; the local bridge owns pairing, device identity, connections, and observations.

- `agent_cli.py`: discovery, guided pairing, configuration, and diagnostics.
- `agent_mcp.py`: six tools — devices, observe, act, status, apps, control.
- `agent_control.py`: per-device ownership, observation receipts, and verified postconditions.
- `adapters/apple_tv.py` and `connection.py`: paired pyatv discovery and reusable connections.
- `wda.py` and `wda_runtime.py`: structured screen state, optional images, and managed signed-helper sessions.

Direct control works without WDA. Visual press/select/type requires a recent observation receipt, consumes it before dispatch, and observes afterward. Selecting a label requires a unique, already-focused element. Direct remote-button commands remain explicitly unverified; they do not inherit visual-action guarantees. Neither a successful request nor a cached image proves the user's goal was achieved.

The native runtime can launch an already installed helper without Xcode in the running path. Initial signing and free-profile renewal still require the documented Apple developer setup. Access is checked on demand; there is no scheduled renewal service.

Apple TV wake/sleep and volume buttons remain available. Whether these also affect a connected TV or receiver depends on the hardware and HDMI-CEC configuration; CouchPilot does not separately control those devices. Sony/Android TV, Sonos, Siri, and room/scene services are outside the active package. See [the archive note](HISTORY.md).

See [setup](AGENT_QUICKSTART.md), [compatibility](COMPATIBILITY.md), and [live evidence](../research/AGENT_ACCEPTANCE_2026-09-19.md).
