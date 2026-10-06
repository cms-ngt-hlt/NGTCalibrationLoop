"""
Tests for scenario-player/fake_conddb.py: provenance manifests.
"""
import threading

import fake_conddb  # scenario-player/fake_conddb.py, on sys.path via tests/conftest.py


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
