# Embodied HA MCP Lab

A separate, experimental Home Assistant app for exercising the real MCP servers
shipped by [Embodied HA](https://github.com/Khronos31/embodied-ha), without starting a
resident daemon or an LLM.

The Lab is intended for human and SCS-agent investigation of odd behavior, invalid
arguments, edge cases, and cross-run state. It forwards the editor body unchanged into
the selected MCP `tools/call` request and retains raw evidence.

## Safety boundary

The Lab isolates its local state from Embodied HA residents. It does **not** sandbox the
outside world. Calls can control the real Home Assistant, operate physical devices, play
sound, capture household audio/images, contact LAN or Internet services, post to AI
Lounge, or consume model quota. Existing EHA gates remain in force, but a Lab state reset
cannot undo any of those effects.

Do not paste passwords, API keys, tokens, private keys, or other secrets into the raw
editor. Raw input, stdout, and stderr are intentionally retained in the run ledger and
local Git history.

## Runtime model

- Each discovery or call starts a fresh MCP child process.
- Mutable state persists on one serialized `generation/<random>` branch.
- Every state-changing run becomes a commit in a separate, remote-free local Git
  repository. Commit messages contain only opaque run IDs.
- Reset starts a new generation at the fixed empty baseline; old branches and evidence
  remain available locally.
- `preferences.json` and `floorplan_room_graph_draft.json` are read directly from the
  Lab directory on every fresh MCP process. Editing either file takes effect on the next
  discovery or call without reinstalling the app.

Persistent runtime data is in Home Assistant's
`/config/embodied-ha-mcp-lab/` directory:

```text
preferences.json               Lab-only MCP configuration
floorplan_room_graph_draft.json Lab-only room graph
state/worktree/                 Current MCP state generation
state/repository/               Separate local Git object database
log/mcp_lab_runs.jsonl          Raw evidence ledger (0600)
eha-mcp-lab-token.config.toml   Direct API bearer token (0600)
```

The ledger retains up to 30 days or 100 MiB, whichever boundary is reached first.

Before the first manual start, place `preferences.json` and
`floorplan_room_graph_draft.json` in that directory. Missing files are initialized with
empty defaults. The generated direct API token is stored beside them with mode `0600`;
EHA's `files/read_file` policy denies that filename.

Version `0.1.1` no longer reads the former
`/addon_configs/<repository-id>_embodied_ha_mcp_lab/` directory. Keep that legacy
directory until the installed canary confirms the new control root; deletion is a
separate manual operation.

## UI and internal API

The administrator-only Ingress UI provides exactly two selectors (server/tool), the live
tool schema, a plain raw textarea, result evidence, generation status, and reset.

SCS can call the same internal API using the generated token:

```bash
read -r EHA_MCP_LAB_TOKEN < \
  /config/embodied-ha-mcp-lab/eha-mcp-lab-token.config.toml
curl -H "Authorization: Bearer ${EHA_MCP_LAB_TOKEN}" \
  http://ADDON_IP:8099/api/servers
curl -H "Authorization: Bearer ${EHA_MCP_LAB_TOKEN}" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-binary '{"room":"study"}' \
  http://ADDON_IP:8099/api/servers/body/tools/move_to/call
```

The direct port is not published to the LAN. Other apps on the internal app network are
inside the same network trust boundary; possession of the token is the direct API gate.

## Source and release identity

The tested EHA source is pinned in [`tested_eha.json`](tested_eha.json). Release automation
checks out that exact commit and builds a commit-addressed multi-architecture candidate.
CI starts that exact candidate under amd64 and arm64/QEMU, verifies its identity, and
discovers every live MCP server. The release workflow only promotes the already-tested
candidate digest to a write-once version tag. The API reports the Lab version, tested EHA
version/revision, bundle hash, `mcp-config.py` hash, and whether runtime source still
matches the build.

The first image is not installable until the tag-gated GHCR workflow succeeds, the package
is made public, and an anonymous pull of the exact `0.1.0` tag succeeds. Never overwrite a
published Lab tag.

## Development checks

```bash
python3 -m pip install -r requirements-dev.txt
ruff check .
pytest -q
python3 scripts/verify_mcp_lab_packaging.py \
  --upstream-root .upstream/embodied-ha-repo/embodied_ha
```

The optional Playwright smoke uses only fixture state:

```bash
python3 scripts/run_ui_preview.py \
  --control-root /tmp/eha-mcp-lab-ui \
  --seed-dir tests/fixtures/ui_seed
node tests/ui_smoke.mjs http://127.0.0.1:18099/
```

## License

MIT — see [LICENSE](LICENSE).
