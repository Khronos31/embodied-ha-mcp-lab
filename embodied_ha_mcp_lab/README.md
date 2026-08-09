# Embodied HA MCP Lab

This folder defines a second Home Assistant app slug. Its Lab-only GHCR image copies
`embodied_ha/` as read-only tested source, then starts the separate `mcp_lab.py` entrypoint.
The resident Dockerfile, entrypoint, runtime, and image are not modified.

Increment 0 is identity-only. It serves `/healthz` and `/api/identity`; it does not execute
MCP tools or language-model harnesses yet.
The add-on remains marked advanced and experimental because it is a development/inspection
surface rather than a resident-facing feature.

## Packaging contract

- The Lab add-on folder has no Supervisor Docker build context. A manually dispatched,
  tag-gated workflow uses the repository root and `.github/docker/mcp-lab.Dockerfile` to
  publish `ghcr.io/khronos31/embodied-ha-mcp-lab`.
- Lab has its own release version. The build manifest separately records the EHA version
  whose source was copied into `/app`.
- The image bakes `/lab/.eha-source-identity.json` for the copied `/app` EHA source. The
  identity endpoint reports build-time and runtime hashes, so image drift is visible.
- The GHCR package must be public before Home Assistant can pull it anonymously. First
  publication is therefore not a completed release until an unauthenticated pull succeeds.
- Increment 0 intentionally grants only Ingress. HA API, `/config`, audio, MQTT, and other
  capabilities are added only with the runner increments that require them.

Do not reuse or overwrite an already published version tag.

The first release is Lab `0.1.0`, testing the EHA `2.1.14` commit pinned in the
repository-root `tested_eha.json`, from tag `mcp-lab-v0.1.0`.
Updating the tested EHA source does not implicitly change the resident add-on artifact, but
it requires a new Lab version and image rather than overwriting an existing GHCR tag.
