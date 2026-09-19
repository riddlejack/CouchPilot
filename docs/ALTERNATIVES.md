# Apple TV MCP comparison

Reviewed 2026-09-19 against [trevor-nichols/appletv-mcp at 930a9bc](https://github.com/trevor-nichols/appletv-mcp/tree/930a9bc95073715dfd85d516cd969dd2e36f2e6b). Both projects expose a local MCP bridge and use pyatv for ordinary paired Apple TV commands.

| Area | CouchPilot | Apple TV MCP |
| --- | --- | --- |
| Screen connection | Signed WebDriverAgent helper, reused during the session | Separate one-shot DVT screenshot helper |
| Agent observations | UI labels, values, focus, foreground app; optional screenshots | Screenshots; focus inferred by the agent |
| Visual action checks | Recent, single-use observation receipts; unique focused-label selection; post-action observation | Screenshots and remote inputs are independent; no equivalent receipt requirement |
| Hardware evidence | Recorded settings change/restore, Netflix episode playback, and free app redownload on one prepared Apple TV | Screenshot helper documentation states fake-tested, not yet hardware-verified |
| Setup tradeoff | Visual route needs a signed helper; free signing expires and requires renewal | DVT screenshot design avoids installing a WDA app, but requires developer pairing/support |
| Packaging | Install from this repository | Published MCP and screenshot packages on PyPI |

CouchPilot's compact text observations can avoid image processing for some steps; comparative token cost and latency have not been benchmarked. We have observed delayed and empty state during transitions, so this is not a zero-latency claim. Direct buttons can still be sent without a visual receipt and are marked unverified.

The other project has useful dedicated seek/volume tools, connection recovery, and careful retry semantics. An agent could navigate the same Settings interfaces through screenshots if that capture route works; CouchPilot does not have exclusive permission to change tvOS settings. Our practical distinction is the structured observation and verification path, with bounded live evidence.

Sources: [their screenshot helper and verification status](https://github.com/trevor-nichols/appletv-mcp/blob/930a9bc95073715dfd85d516cd969dd2e36f2e6b/sidecars/appletv-screenshot/README.md), [their retry policy](https://github.com/trevor-nichols/appletv-mcp/blob/930a9bc95073715dfd85d516cd969dd2e36f2e6b/src/appletv_mcp/application/policies/retry.py), [our acceptance record](../research/AGENT_ACCEPTANCE_2026-09-19.md), and [our compatibility limits](COMPATIBILITY.md). Neither project establishes a turnkey solution to every content, headphone, and projector workflow; this review does not establish that no other solution exists.
