"""Fail closed when MCP Lab packaging no longer matches its EHA source release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

LAB_IMAGE = "ghcr.io/khronos31/embodied-ha-mcp-lab"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
EHA_REPOSITORY = "Khronos31/embodied-ha"
EHA_SOURCE_SUBDIR = "embodied_ha"


def _yaml_scalar(path: Path, key: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^[ \t]*{re.escape(key)}:\s*[\"']?([^\"'\s#]+)",
        text,
        flags=re.MULTILINE,
    )
    if not match:
        raise ValueError(f"{path}: missing scalar {key}")
    return match.group(1)


def _load_contract(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("tested_eha.json must contain an object")
    required = {"repository", "revision", "source_subdir", "version"}
    if set(value) != required or not all(
        isinstance(value[key], str) for key in required
    ):
        raise ValueError("tested_eha.json has an unexpected schema")
    return value


def verify_packaging(
    root: Path,
    *,
    release_ref: str | None = None,
    requested_version: str | None = None,
    upstream_root: Path | None = None,
) -> dict[str, str]:
    root = root.resolve()
    lab_dir = root / "embodied_ha_mcp_lab"
    lab_config = lab_dir / "config.yaml"
    contract = _load_contract(root / "tested_eha.json")

    lab_version = _yaml_scalar(lab_config, "version")
    if not VERSION_PATTERN.fullmatch(lab_version):
        raise ValueError(f"unsupported Lab version: {lab_version!r}")
    if contract["repository"] != EHA_REPOSITORY:
        raise ValueError("unexpected EHA repository")
    if contract["source_subdir"] != EHA_SOURCE_SUBDIR:
        raise ValueError("unexpected EHA source subdirectory")
    if not REVISION_PATTERN.fullmatch(contract["revision"]):
        raise ValueError("EHA revision must be a full lowercase commit SHA")
    if not VERSION_PATTERN.fullmatch(contract["version"]):
        raise ValueError(f"unsupported EHA version: {contract['version']!r}")
    if _yaml_scalar(lab_config, "slug") != "embodied_ha_mcp_lab":
        raise ValueError("unexpected Lab slug")
    if _yaml_scalar(lab_config, "image") != LAB_IMAGE:
        raise ValueError("unexpected Lab image")
    if (lab_dir / "Dockerfile").exists() or list(lab_dir.glob("*-mcp.py")):
        raise ValueError(
            "Lab add-on folder must not contain a second MCP source bundle"
        )
    for required in (
        "mcp_lab.py",
        "source_identity.py",
        "runner.py",
        "runtime.py",
        "state_repository.py",
        "web/index.html",
        "web/app.js",
        "web/style.css",
    ):
        if not (lab_dir / required).is_file():
            raise ValueError(f"missing Lab implementation file: {required}")
    if not (root / ".github" / "docker" / "mcp-lab.Dockerfile").is_file():
        raise ValueError("missing CI-only Lab Dockerfile")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    if "!embodied_ha_mcp_lab/**" not in dockerignore:
        raise ValueError("Docker context does not include the complete Lab package")
    if (root / "embodied_ha").exists():
        raise ValueError("EHA source must not be committed inside the Lab repository")

    if upstream_root is not None:
        upstream_root = upstream_root.resolve()
        if (
            _yaml_scalar(upstream_root / "config.yaml", "version")
            != contract["version"]
        ):
            raise ValueError("checked-out EHA version does not match tested_eha.json")
        for required in ("Dockerfile", "run.sh", "mcp-config.py"):
            if not (upstream_root / required).is_file():
                raise ValueError(f"missing checked-out EHA source file: {required}")

    if requested_version is not None and requested_version != lab_version:
        raise ValueError(
            f"requested version {requested_version!r} does not match {lab_version!r}"
        )
    if release_ref is not None and release_ref != f"mcp-lab-v{lab_version}":
        raise ValueError(
            f"release ref {release_ref!r} must be exactly 'mcp-lab-v{lab_version}'"
        )

    return {
        "lab_version": lab_version,
        "tested_eha_version": contract["version"],
        "tested_eha_repository": contract["repository"],
        "tested_eha_revision": contract["revision"],
        "tested_eha_source_subdir": contract["source_subdir"],
        "image": LAB_IMAGE,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--release-ref")
    parser.add_argument("--requested-version")
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        result = verify_packaging(
            args.root,
            release_ref=args.release_ref,
            requested_version=args.requested_version,
            upstream_root=args.upstream_root,
        )
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        parser.exit(1, f"MCP Lab packaging verification failed: {error}\n")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"lab_version={result['lab_version']}\n")
            output.write(f"tested_eha_version={result['tested_eha_version']}\n")
            output.write(f"tested_eha_repository={result['tested_eha_repository']}\n")
            output.write(f"tested_eha_revision={result['tested_eha_revision']}\n")
            output.write(
                f"tested_eha_source_subdir={result['tested_eha_source_subdir']}\n"
            )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
