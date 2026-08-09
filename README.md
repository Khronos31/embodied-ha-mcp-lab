# Embodied HA MCP Lab

Developer and audit tooling for exercising the real MCP servers shipped by
[Embodied HA](https://github.com/Khronos31/embodied-ha), without attaching the resident
daemon or an LLM.

Status: **Increment 0 — packaging and source identity only.** The current server exposes
`/healthz` and `/api/identity`; it does not execute MCP tools yet.

This repository is public for remote preservation and future Home Assistant add-on
installation. The add-on is not installable until its first GHCR image has been published,
made public, and verified with an anonymous pull. It remains marked advanced and
experimental.

The tested EHA source is pinned in [`tested_eha.json`](tested_eha.json). Release automation
checks out that exact commit into a temporary ignored directory, verifies its declared
version, and builds the Lab image without committing a copy of EHA source or history here.

## Development checks

```bash
python3 -m pip install -r requirements-dev.txt
ruff check embodied_ha_mcp_lab scripts tests
pytest -q
python3 scripts/verify_mcp_lab_packaging.py \
  --release-ref mcp-lab-v0.1.0 \
  --requested-version 0.1.0
```

## License

MIT — see [LICENSE](LICENSE).
