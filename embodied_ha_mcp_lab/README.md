# Embodied HA MCP Lab app

This folder is the Home Assistant app descriptor and Python package. Its GHCR image
contains an immutable, commit-pinned Embodied HA source bundle at `/app`, while the Lab
backend and UI live at `/lab/embodied_ha_mcp_lab`.

The image starts only `python3 -m embodied_ha_mcp_lab.mcp_lab`. It does not start EHA's
`run.sh`, daemon, audio daemon, MQTT discovery, or an LLM harness.

## Configuration

- `tested_harness`: `claude`, `codex`, or `agy`; controls harness-dependent MCP exposure
  only. The Lab itself does not invoke a resident LLM.
- `timeout_seconds`: total initialize/call timeout, 1–300 seconds (default 45).
- `seed_data_dir`: first-start source for the two allowlisted configuration files
  (container path; default `/config`, which is this app's own `addon_config`).

The app is administrator-only, advanced, experimental, and boot-manual. Its private
`/data` volume and its own `addon_config` are writable, but the resident Home Assistant
configuration is not mounted. HA API, audio, and optional MQTT permissions exist because
the selected MCP tools must exercise the same real capabilities as EHA. Those external
permissions are not an isolation boundary.

See the repository-root README for the state, evidence, API, and release contracts.
