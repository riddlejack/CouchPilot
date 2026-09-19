# 🛋️ CouchPilot

**Let your agent control your Apple TV.**

I got sick of screwing around with Apple TV settings, hunting down shows across streaming apps, and relaying an agent’s troubleshooting advice through the remote. CouchPilot lets you ask your agent in plain English to queue up shows, change settings, and navigate apps—especially useful with a projector, where fixing the picture can take longer than picking a movie.

> “Find Breaking Bad and get an episode ready.”
>
> “Help me fix the washed-out picture.”
>
> “Find and install Crossy Road.”

A local, open-source bridge for **Codex, Claude Code, and other MCP agents**. No jailbreak. No separate model API key.

## How it works

```mermaid
flowchart TD
    You["You"] -->|"Plain English"| Agent["Your agent"]
    Agent <-->|"MCP tools and results"| Bridge["CouchPilot<br/>on your computer"]
    Bridge <-->|"pyatv<br/>Direct commands"| TV["Apple TV"]
    Bridge <-->|"Optional WDA helper<br/>Screen and UI"| TV
```

[pyatv](https://github.com/postlund/pyatv) handles power, playback, app launches, and typing over your local network. For tasks that need the screen, a signed [WebDriverAgent](https://github.com/appium/WebDriverAgent) helper on the Apple TV returns UI labels, focus, and screenshots. Your existing agent chooses the next action and checks the result, using compact text observations first and images when needed.

## Why CouchPilot?

Related [Apple TV MCP projects](https://github.com/trevor-nichols/appletv-mcp) already exist. As of September 2026, I haven’t found a turnkey repo or product covering the full watch-and-troubleshoot workflow I wanted. CouchPilot is my attempt to get there. [Research and alternatives →](docs/REASSESSMENT_2026-09-19.md#existing-projects-and-buy-versus-build-findings)

**[Get started →](docs/AGENT_QUICKSTART.md)** · [What works today](docs/COMPATIBILITY.md) · [MIT license](LICENSE)

**Early preview:** playback, app navigation, and a picture-setting change have been tested on a real Apple TV. Full visual control currently needs a Mac and a signed helper; free signing requires occasional renewal. Cross-service show discovery and seamless setup are still works in progress.
