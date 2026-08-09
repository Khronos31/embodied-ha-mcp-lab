"""Create a deterministic identity for an immutable EHA source bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

IDENTITY_FILENAME = ".eha-source-identity.json"
_EXCLUDED_DIRS = {".git", "__pycache__"}


def _is_excluded(relative: Path) -> bool:
    if any(part in _EXCLUDED_DIRS for part in relative.parts):
        return True
    name = relative.name
    return (
        name == IDENTITY_FILENAME
        or name.startswith(f"{IDENTITY_FILENAME}.")
        or name.endswith((".pyc", ".pyo"))
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def source_entries(root: Path) -> list[dict[str, Any]]:
    """Return stable content records for every source file below *root*."""

    root = root.resolve()
    entries: list[dict[str, Any]] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root)
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            kind = "symlink"
        elif path.is_file():
            payload = path.read_bytes()
            kind = "file"
        else:
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "type": kind,
                "size": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )
    return entries


def build_identity(
    root: Path,
    *,
    lab_version: str = "",
    tested_eha_version: str = "",
    build_arch: str = "",
    source_repository: str = "",
    source_revision: str = "",
) -> dict[str, Any]:
    """Build the reproducible identity document for *root*."""

    root = root.resolve()
    entries = source_entries(root)
    mcp_config = root / "mcp-config.py"
    return {
        "schema_version": 1,
        "bundle_sha256": _sha256_bytes(_canonical_json(entries)),
        "file_count": len(entries),
        "mcp_config_sha256": (
            _sha256_bytes(mcp_config.read_bytes()) if mcp_config.is_file() else None
        ),
        "lab_version": lab_version.strip() or None,
        "tested_eha_version": tested_eha_version.strip() or None,
        "build_arch": build_arch.strip() or None,
        "source_repository": source_repository.strip() or None,
        "source_revision": source_revision.strip() or None,
        "files": entries,
    }


def write_identity(output: Path, identity: dict[str, Any]) -> None:
    """Atomically write an identity document."""

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(identity, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def verify_identity(root: Path, identity_path: Path) -> bool:
    """Return whether *root* still matches a previously written manifest."""

    try:
        expected = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(expected, dict) or not isinstance(
        expected.get("bundle_sha256"), str
    ):
        return False
    actual = build_identity(root)
    return actual["bundle_sha256"] == expected["bundle_sha256"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write", help="write an identity manifest")
    write_parser.add_argument("--root", type=Path, required=True)
    write_parser.add_argument("--output", type=Path, required=True)
    write_parser.add_argument("--lab-version", default="")
    write_parser.add_argument("--tested-eha-version", default="")
    write_parser.add_argument("--build-arch", default="")
    write_parser.add_argument("--source-repository", default="")
    write_parser.add_argument("--source-revision", default="")
    verify_parser = subparsers.add_parser("verify", help="verify an identity manifest")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "write":
        identity = build_identity(
            args.root,
            lab_version=args.lab_version,
            tested_eha_version=args.tested_eha_version,
            build_arch=args.build_arch,
            source_repository=args.source_repository,
            source_revision=args.source_revision,
        )
        write_identity(args.output, identity)
        return 0

    if verify_identity(args.root, args.identity):
        print("source bundle matches identity manifest")
        return 0
    print("source bundle does not match identity manifest", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
