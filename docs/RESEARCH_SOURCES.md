# Research sources and decisions

Reviewed July 20, 2026. Re-check current releases and documentation before
implementation because tvOS and reverse-engineered protocol compatibility can
change quickly.

## Apple TV protocol foundation

- `pyatv` source: https://github.com/postlund/pyatv
- `pyatv` v0.18.0 release, June 19, 2026:
  https://github.com/postlund/pyatv/releases/tag/v0.18.0
- `atvremote` CLI documentation:
  https://pyatv.dev/documentation/atvremote/
- App listing, launch, and tested deep-link examples:
  https://pyatv.dev/development/apps/
- Protocol/concept documentation:
  https://pyatv.dev/documentation/concepts/
- Current raw `play_url`/AirPlay regression report on tvOS 26.2:
  https://github.com/postlund/pyatv/issues/2821

Decision: use `pyatv`; do not implement Apple protocols from scratch. Keep raw
AirPlay media streaming separate from Companion app/deep-link launching.

## Existing MCP prior art

- `mcp-pyatv` PyPI package:
  https://pypi.org/project/mcp-pyatv/
- Source repository:
  https://github.com/crlian/mcp-pyatv
- April 2026 status, tests, screenshot path, and known tvOS 26 limitations:
  https://github.com/crlian/mcp-pyatv/blob/main/STATUS.md

Decision: review and reuse sound ideas, but do not depend on its alpha MCP shape
as the entire product. Build the room-aware typed core independently.

## Apple power, CEC, and installation

- Apple TV control of televisions and receivers over HDMI-CEC:
  https://support.apple.com/guide/tv/apple-tv-4k-remote-control-receiver-atvbbe2477c9/tvos
- Apple volume/CEC troubleshooting:
  https://support.apple.com/en-us/108769
- Apple TV automatic app installation settings:
  https://support.apple.com/en-ca/guide/tv/atvbee653159/tvos
- Apple Configurator app installation with a personal Apple Account:
  https://support.apple.com/en-mide/guide/apple-configurator-mac/cad4cd08c03/mac
- Managed/supervised app distribution:
  https://support.apple.com/en-ie/guide/deployment/dep575bfed86/web
- Device-management InstallApplication command:
  https://developer.apple.com/documentation/devicemanagement/install-application-command

Decision: CEC can often wake the physical TV but must be verified per room.
Arbitrary app installation is not an Apple TV remote-protocol feature and stays
outside the reliable first slice.

## Content links

- Apple universal-link guidance:
  https://developer.apple.com/documentation/xcode/allowing-apps-and-websites-to-link-to-your-content/
- Apple tvOS deep-linking session:
  https://developer.apple.com/videos/play/wwdc2017/246/
- `pyatv` tested provider link examples:
  https://pyatv.dev/development/apps/

Decision: start with provider share/deep links and saved aliases. Provider
profile state, not a universal Apple API, normally owns resume position.

## Home Assistant and physical devices

- Apple TV integration and volume caveats:
  https://www.home-assistant.io/integrations/apple_tv/
- Native Home Assistant MCP server:
  https://www.home-assistant.io/integrations/mcp_server
- Home Assistant LLM/MCP developer architecture:
  https://developers.home-assistant.io/docs/core/llm/
- HDMI-CEC integration and physical connection requirement:
  https://www.home-assistant.io/integrations/hdmi_cec
- Sony Bravia integration:
  https://www.home-assistant.io/integrations/braviatv
- LG webOS integration:
  https://www.home-assistant.io/integrations/webostv/
- Samsung TV integration:
  https://www.home-assistant.io/integrations/samsungtv
- Vizio integration:
  https://www.home-assistant.io/integrations/vizio
- Home Assistant 2026.5 infrared platform:
  https://www.home-assistant.io/blog/2026/05/06/release-20265/

Decision: Home Assistant is the preferred broad adapter for heterogeneous
physical devices, but it is optional for the portable Apple TV core. Network
admin access does not create HDMI-CEC access: CEC requires Apple TV pass-through,
a computer/adapter physically on the HDMI bus, or a device/vendor API.

## Sonos

- Agent-oriented local Sonos CLI:
  https://sonoscli.sh/
- Official Sonos Control API overview:
  https://docs.sonos.com/reference/about-control-api
- Home Assistant Sonos integration:
  https://www.home-assistant.io/integrations/sonos/

Decision: exact volume should route to Sonos when that room uses Sonos. Compare
the current local CLI/SoCo path with Home Assistant before selecting the adapter.

## Relevant X bookmarks

The X connector exposes post creation time but not exact bookmark-save time. The
two newest 100-bookmark pages were reviewed.

- Dax, July 19, 2026: derive a purpose-built CLI from a captured HAR rather than
  repeatedly controlling a browser:
  https://x.com/thdxr/status/2078727284865827140
- Peter Steinberger: "Everything's either a fast or slow API now":
  https://x.com/steipete/status/2067821739556413651
- Steve Ruiz's agent-oriented `papercuts` CLI:
  https://x.com/steveruizok/status/2075303919664734295

Decision: the HAR-to-CLI technique is relevant for web-only services but is not
the Apple TV core. Prefer the already available local protocols. Any private
provider API capture needs explicit security and terms review.

