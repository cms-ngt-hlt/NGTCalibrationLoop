"""
Tests for scenario-player/fake_conddb.py: provenance manifests and the conditions-DB ledger. The
ledger's writer uses fcntl.flock (like faults.py's locked_json_file), so its tests skip off POSIX.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import fake_conddb  # scenario-player/fake_conddb.py, on sys.path via tests/conftest.py

posix_only = pytest.mark.skipif(os.name != "posix", reason="the ledger writer uses fcntl.flock")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("NGT_DEV_HOME", str(tmp_path))
    monkeypatch.delenv("NGT_CONDDB_DIR", raising=False)
    return tmp_path / fake_conddb.CONDDB_DIR_NAME / fake_conddb.LEDGER_NAME


# --- provenance manifests ------------------------------------------------------


def test_write_then_read_provenance_round_trips(tmp_path):
    path = tmp_path / "PromptCalibProdEcalPedestals.root"
    fake_conddb.write_provenance(
        path,
        step="step3",
        calibration="EcalPedestals",
        run="398600",
        lumisections={52, 51},
        inputs=["/some/dir/run398600_LS0051To0052_ecalPedsStep2.root"],
    )

    manifest = fake_conddb.read_provenance(path)
    assert manifest["step"] == "step3"
    assert manifest["calibration"] == "EcalPedestals"
    assert manifest["run"] == 398600  # normalised to an int even from a string
    assert manifest["lumisections"] == [51, 52]  # sorted
    assert manifest["inputs"] == ["run398600_LS0051To0052_ecalPedsStep2.root"]  # basenames only
    assert path.read_text(encoding="utf-8").startswith(fake_conddb.PROVENANCE_MAGIC)


def test_write_provenance_never_exposes_a_half_written_manifest(tmp_path):
    path = tmp_path / "promptCalibConditions.db"
    fake_conddb.write_provenance(
        path, step="step4", calibration="EcalPedestals", run=1, lumisections={1}, inputs=["a.root"]
    )
    seen = []

    def reader():
        for _ in range(400):
            seen.append(fake_conddb.read_provenance(path))

    def writer():
        for n in range(2, 60):
            fake_conddb.write_provenance(
                path, step="step4", calibration="EcalPedestals", run=1, lumisections=set(range(1, n)), inputs=[]
            )

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen and all(m is not None and m["lumisections"] for m in seen), "a reader saw a half-written manifest"


def test_write_provenance_leaves_no_temp_files_behind(tmp_path):
    path = tmp_path / "out.root"
    fake_conddb.write_provenance(path, step="step2", calibration="EcalPedestals", run=1, lumisections={1}, inputs=[])
    assert [q.name for q in tmp_path.iterdir()] == ["out.root"]


def test_read_provenance_of_a_file_that_is_not_ours_is_none(tmp_path):
    raw = tmp_path / "run398600_ls0051.root"
    raw.write_bytes(b"(FAKE) raw data placeholder\n")
    assert fake_conddb.read_provenance(raw) is None
    assert fake_conddb.read_provenance(tmp_path / "nope.root") is None


# --- the ledger ----------------------------------------------------------------


def test_read_ledger_of_a_missing_file_is_empty(ledger):
    assert fake_conddb.read_ledger() == []


@posix_only
def test_append_payload_assigns_increasing_seq_and_a_timestamp(ledger):
    first = fake_conddb.append_payload({"calibration": "EcalPedestals", "run": 398600, "lumisections": [51]})
    second = fake_conddb.append_payload({"calibration": "EcalPedestals", "run": 398600, "lumisections": [51, 52]})

    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["uploaded_at"] and second["uploaded_at"]
    assert [row["seq"] for row in fake_conddb.read_ledger()] == [1, 2]


@posix_only
def test_append_payload_writes_one_json_object_per_line_with_seq_first(ledger):
    fake_conddb.append_payload({"calibration": "BeamSpot", "run": 1, "lumisections": [1]})
    fake_conddb.append_payload({"calibration": "BeamSpot", "run": 2, "lumisections": [2]})

    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)
    assert lines[0].startswith('{"seq": 1')  # so a hand-read of the file scans in upload order


@posix_only
def test_append_payload_is_safe_from_several_processes_worth_of_writers(ledger):
    """Several calibrations upload at once, each upload its own process: no row lost, no seq reused."""
    row = {"calibration": "EcalPedestals", "run": 398600}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: fake_conddb.append_payload({**row, "lumisections": [i]}), range(24)))

    rows = fake_conddb.read_ledger()
    assert sorted(row["seq"] for row in rows) == list(range(1, 25))


@posix_only
def test_read_ledger_skips_a_torn_trailing_line(ledger):
    fake_conddb.append_payload({"calibration": "EcalPedestals", "run": 398600, "lumisections": [51]})
    with ledger.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "calibration": "Ecal')

    assert [row["seq"] for row in fake_conddb.read_ledger()] == [1]


def test_ngt_conddb_dir_overrides_ngt_dev_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NGT_DEV_HOME", str(tmp_path / "devhome"))
    monkeypatch.setenv("NGT_CONDDB_DIR", str(tmp_path / "elsewhere"))
    assert fake_conddb.ledger_path() == str(tmp_path / "elsewhere" / fake_conddb.LEDGER_NAME)


def test_unset_ngt_dev_home_resolves_to_the_same_place_seed_py_uses(monkeypatch):
    """The player seeds runs through seed.py and reads the ledger through fake_conddb."""
    import seed  # scenario-player/seed.py

    monkeypatch.delenv("NGT_DEV_HOME", raising=False)
    monkeypatch.delenv("NGT_CONDDB_DIR", raising=False)

    assert fake_conddb.ledger_path() == str(seed.NGT_DEV_HOME / fake_conddb.CONDDB_DIR_NAME / fake_conddb.LEDGER_NAME)
