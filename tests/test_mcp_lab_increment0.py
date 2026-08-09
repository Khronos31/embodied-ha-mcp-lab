import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import yaml

from embodied_ha_mcp_lab.auth import Authenticator

ROOT = Path(__file__).resolve().parents[1]
LAB_DIR = ROOT / "embodied_ha_mcp_lab"
CONTRACT = ROOT / "tested_eha.json"
PACKAGING_VERIFIER = ROOT / "scripts" / "verify_mcp_lab_packaging.py"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-mcp-lab-image.yml"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
LAB_DOCKERFILE = ROOT / ".github" / "docker" / "mcp-lab.Dockerfile"


from embodied_ha_mcp_lab import mcp_lab, source_identity


def test_lab_manifest_uses_an_independent_runtime_release():
    lab = yaml.safe_load((LAB_DIR / "config.yaml").read_text(encoding="utf-8"))
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert lab["slug"] == "embodied_ha_mcp_lab"
    assert lab["version"] == "0.1.1"
    assert contract["version"] == "2.1.14"
    assert re.fullmatch(r"[0-9a-f]{40}", contract["revision"])
    assert contract["repository"] == "Khronos31/embodied-ha"
    assert lab["advanced"] is True
    assert lab["stage"] == "experimental"
    assert lab["image"] == "ghcr.io/khronos31/embodied-ha-mcp-lab"
    assert "environment" not in lab
    assert lab["ingress_port"] == 8099
    assert lab["map"] == [
        {"type": "homeassistant_config", "read_only": False, "path": "/config"}
    ]
    assert lab["audio"] is True
    assert "hassio_api" not in lab
    assert lab["homeassistant_api"] is True
    assert lab["services"] == ["mqtt:want"]
    assert lab["options"] == {
        "tested_harness": "claude",
        "timeout_seconds": 45,
    }
    assert not (LAB_DIR / "Dockerfile").exists()
    assert not list(LAB_DIR.glob("*-mcp.py"))


def test_eha_source_and_history_are_not_committed_to_the_lab_repository():
    assert not (ROOT / "embodied_ha").exists()
    assert not list(LAB_DIR.glob("*-mcp.py"))
    dockerfile = LAB_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY .upstream/embodied-ha-repo/embodied_ha/ /app/" in dockerfile


def test_dockerfile_bakes_source_identity_after_copy():
    dockerfile = LAB_DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.index(
        "COPY .upstream/embodied-ha-repo/embodied_ha/ /app/"
    ) < dockerfile.index("source_identity.py write")
    assert "--lab-version" in dockerfile
    assert "--tested-eha-version" in dockerfile
    assert "apt-get install" in dockerfile
    assert "python:3.11-slim-bookworm@sha256:" in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert "COPY embodied_ha_mcp_lab/ /lab/embodied_ha_mcp_lab/" in dockerfile
    assert 'CMD ["python3", "-m", "embodied_ha_mcp_lab.mcp_lab"]' in dockerfile
    assert (
        "EHA_MCP_LAB_AUTH_FILE=/config/embodied-ha-mcp-lab/"
        "eha-mcp-lab-token.config.toml"
    ) in dockerfile


def test_canary_reads_the_direct_token_from_the_persistent_lab_root():
    workflow = TEST_WORKFLOW.read_text(encoding="utf-8")
    assert (
        '"/config/embodied-ha-mcp-lab/eha-mcp-lab-token.config.toml"'
        in workflow
    )
    assert 'Path("/config/eha-mcp-lab-token.config.toml")' not in workflow


def test_docker_context_includes_the_complete_lab_package():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!embodied_ha_mcp_lab/**" in dockerignore
    for required in (
        "auth.py",
        "execution_queue.py",
        "ledger.py",
        "runner.py",
        "runtime.py",
        "service.py",
        "state_repository.py",
        "web/index.html",
        "web/app.js",
        "web/style.css",
    ):
        assert (LAB_DIR / required).is_file()


def test_identity_is_stable_and_detects_source_changes(tmp_path):
    (tmp_path / "mcp-config.py").write_text("config = 1\n", encoding="utf-8")
    (tmp_path / "camera-mcp.py").write_text("camera = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")

    first = source_identity.build_identity(
        tmp_path,
        lab_version="0.1.0",
        tested_eha_version="2.1.14",
    )
    output = tmp_path / source_identity.IDENTITY_FILENAME
    source_identity.write_identity(output, first)
    second = source_identity.build_identity(
        tmp_path,
        lab_version="0.1.0",
        tested_eha_version="2.1.14",
    )

    assert first == second
    assert first["file_count"] == 2
    assert first["mcp_config_sha256"] is not None
    assert source_identity.verify_identity(tmp_path, output) is True

    (tmp_path / "camera-mcp.py").write_text("camera = 2\n", encoding="utf-8")
    changed = source_identity.build_identity(
        tmp_path,
        lab_version="0.1.0",
        tested_eha_version="2.1.14",
    )
    assert changed["bundle_sha256"] != first["bundle_sha256"]
    assert source_identity.verify_identity(tmp_path, output) is False


def test_identity_verify_cli_fails_for_a_mismatched_fixture(tmp_path):
    (tmp_path / "mcp-config.py").write_text("config = 1\n", encoding="utf-8")
    output = tmp_path / source_identity.IDENTITY_FILENAME
    source_identity.write_identity(output, source_identity.build_identity(tmp_path))

    matching = subprocess.run(
        [
            sys.executable,
            str(LAB_DIR / "source_identity.py"),
            "verify",
            "--root",
            str(tmp_path),
            "--identity",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert matching.returncode == 0

    (tmp_path / "mcp-config.py").write_text("config = 2\n", encoding="utf-8")
    mismatched = subprocess.run(
        matching.args,
        check=False,
        capture_output=True,
        text=True,
    )
    assert mismatched.returncode == 1
    assert "does not match" in mismatched.stderr


def test_packaging_verifier_rejects_version_or_tag_mismatch(tmp_path):
    lab_version = yaml.safe_load((LAB_DIR / "config.yaml").read_text(encoding="utf-8"))[
        "version"
    ]
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    github_output = tmp_path / "github-output"
    valid = subprocess.run(
        [
            sys.executable,
            str(PACKAGING_VERIFIER),
            "--root",
            str(ROOT),
            "--release-ref",
            f"mcp-lab-v{lab_version}",
            "--requested-version",
            lab_version,
            "--github-output",
            str(github_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0
    assert json.loads(valid.stdout) == {
        "image": "ghcr.io/khronos31/embodied-ha-mcp-lab",
        "lab_version": lab_version,
        "tested_eha_repository": contract["repository"],
        "tested_eha_revision": contract["revision"],
        "tested_eha_source_subdir": contract["source_subdir"],
        "tested_eha_version": contract["version"],
    }
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        f"lab_version={lab_version}",
        f"tested_eha_version={contract['version']}",
        f"tested_eha_repository={contract['repository']}",
        f"tested_eha_revision={contract['revision']}",
        f"tested_eha_source_subdir={contract['source_subdir']}",
    ]

    mismatched = subprocess.run(
        [
            sys.executable,
            str(PACKAGING_VERIFIER),
            "--root",
            str(ROOT),
            "--release-ref",
            "v0.0.0",
            "--requested-version",
            lab_version,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert mismatched.returncode == 1
    assert "must be exactly" in mismatched.stderr


def test_packaging_verifier_checks_the_pinned_upstream_version(tmp_path):
    upstream = tmp_path / "embodied_ha"
    upstream.mkdir()
    (upstream / "config.yaml").write_text('version: "2.1.14"\n', encoding="utf-8")
    for filename in ("Dockerfile", "run.sh", "mcp-config.py"):
        (upstream / filename).write_text("fixture\n", encoding="utf-8")

    command = [
        sys.executable,
        str(PACKAGING_VERIFIER),
        "--root",
        str(ROOT),
        "--upstream-root",
        str(upstream),
    ]
    matching = subprocess.run(command, check=False, capture_output=True, text=True)
    assert matching.returncode == 0

    (upstream / "config.yaml").write_text('version: "2.1.13"\n', encoding="utf-8")
    mismatched = subprocess.run(command, check=False, capture_output=True, text=True)
    assert mismatched.returncode == 1
    assert "does not match" in mismatched.stderr


def test_publish_workflow_is_manual_tag_gated_and_commit_pinned():
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert 'test "$RELEASE_REF_TYPE" = "tag"' in workflow_text
    assert "--release-ref" in workflow_text
    assert "mcp-lab-v<version>" in workflow_text
    assert "candidate-${{ github.sha }}" in workflow_text
    assert "imagetools create --tag" in workflow_text
    assert "Release tag already exists and is immutable" in workflow_text
    assert "build-push-action" not in workflow_text
    assert ":latest" not in workflow_text

    action_refs = re.findall(
        r"^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow_text, re.MULTILINE
    )
    assert len(action_refs) == 3
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_main_workflow_builds_and_canaries_both_exact_candidate_architectures():
    workflow_text = TEST_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert "platforms: linux/amd64,linux/arm64" in workflow_text
    assert "candidate-${{ github.sha }}" in workflow_text
    assert "runtime_matches_build" in workflow_text
    assert 'entry["name"] for entry in server_entries' in workflow_text
    assert "assert len(servers) == 13" in workflow_text
    assert "linux/amd64" in workflow_text
    assert "linux/arm64" in workflow_text
    assert "docker exec -i" in workflow_text
    action_refs = re.findall(
        r"^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow_text, re.MULTILINE
    )
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_public_identity_reports_matching_and_drifted_runtime(tmp_path):
    (tmp_path / "config.yaml").write_text('version: "9.9.9"\n', encoding="utf-8")
    (tmp_path / "mcp-config.py").write_text("config = 1\n", encoding="utf-8")
    baked_path = tmp_path / source_identity.IDENTITY_FILENAME
    baked = source_identity.build_identity(
        tmp_path,
        lab_version="0.7.0",
        tested_eha_version="9.9.9",
        build_arch="aarch64",
        source_repository="example/eha",
        source_revision="abc123",
    )
    source_identity.write_identity(baked_path, baked)

    matching = mcp_lab.public_identity(tmp_path, baked_path)
    assert matching["build_manifest_present"] is True
    assert matching["runtime_matches_build"] is True
    assert matching["lab_version"] == "0.7.0"
    assert matching["tested_eha_version"] == "9.9.9"
    assert matching["source_repository"] == "example/eha"
    assert matching["source_revision"] == "abc123"

    (tmp_path / "mcp-config.py").write_text("config = 2\n", encoding="utf-8")
    drifted = mcp_lab.public_identity(tmp_path, baked_path)
    assert drifted["runtime_matches_build"] is False
    assert drifted["runtime_bundle_sha256"] != drifted["mcp_bundle_sha256"]


def test_public_identity_does_not_trust_a_malformed_build_manifest(tmp_path):
    (tmp_path / "config.yaml").write_text('version: "9.9.9"\n', encoding="utf-8")
    (tmp_path / "mcp-config.py").write_text("config = 1\n", encoding="utf-8")
    malformed = tmp_path / source_identity.IDENTITY_FILENAME
    malformed.write_text(
        '{"schema_version": 1, "bundle_sha256": "not-a-hash"}\n',
        encoding="utf-8",
    )

    identity = mcp_lab.public_identity(tmp_path, malformed)
    assert identity["build_manifest_present"] is False
    assert identity["runtime_matches_build"] is None
    assert identity["tested_eha_version"] == "9.9.9"


def test_health_and_authenticated_identity_endpoints(tmp_path):
    identity = {"lab_mode": "mcp_lab", "build_id": "sha256:test"}
    authenticator = Authenticator(tmp_path / "token", {"127.0.0.1"})
    server = mcp_lab.create_server("127.0.0.1", 0, None, authenticator, identity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/healthz", timeout=2) as response:
            assert response.status == 200
            assert json.load(response) == {"mode": "mcp_lab", "ok": True}
        with urlopen(f"{base_url}/api/identity", timeout=2) as response:
            assert response.status == 200
            assert json.load(response) == identity
        try:
            urlopen(f"{base_url}/api/call", timeout=2)
        except HTTPError as error:
            assert error.code == 404
            assert json.load(error) == {"error": "not_found"}
        else:
            raise AssertionError("unknown route must remain closed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
