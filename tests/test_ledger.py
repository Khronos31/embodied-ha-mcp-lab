import datetime as dt
import json
import stat
import threading

from embodied_ha_mcp_lab.ledger import RunLedger


def event(identifier, timestamp=None, padding=""):
    return {
        "run_id": identifier,
        "timestamp": timestamp or dt.datetime.now(dt.timezone.utc).isoformat(),
        "padding": padding,
    }


def test_concurrent_appends_keep_private_valid_jsonl(tmp_path):
    ledger = RunLedger(tmp_path / "runs.jsonl")
    threads = [
        threading.Thread(target=ledger.append, args=(event(f"{index:032x}"),))
        for index in range(40)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 40
    assert len({json.loads(line)["run_id"] for line in lines}) == 40
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.lock_path.stat().st_mode) == 0o600


def test_age_and_size_rotation_are_explicit(tmp_path):
    ledger = RunLedger(tmp_path / "runs.jsonl", retention_days=30, max_bytes=700)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat()
    assert ledger.append(event("0" * 32, old, "x" * 50)) is None
    age_rotation = ledger.append(event("1" * 32, padding="x" * 50))
    assert age_rotation["dropped_events"] == 1

    observed_rotation = None
    for index in range(2, 20):
        rotation = ledger.append(event(f"{index:032x}", padding="y" * 100))
        observed_rotation = rotation or observed_rotation
    assert observed_rotation is not None
    assert ledger.path.stat().st_size <= 700
    assert all(json.loads(line) for line in ledger.path.read_text().splitlines())
