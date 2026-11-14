"""Tests for ngt_calibration_loop.lineage: an output's lumisections and inputs, beside it, and the union over a set of
outputs, which is unknown as soon as one of them has none."""

from ngt_calibration_loop import lineage


def test_an_outputs_lineage_lies_beside_it(tmp_path):
    output = tmp_path / "run403634_LS1246To1259_ecalPedsStep2.root"
    record = lineage.write(output, {1247, 1246, 1246}, [tmp_path / "b.root", tmp_path / "a.root"])

    assert record == {"lumisections": [1246, 1247], "inputs": [str(tmp_path / "a.root"), str(tmp_path / "b.root")]}
    assert lineage.path_of(output).name == "run403634_LS1246To1259_ecalPedsStep2.root.lineage.json"
    assert lineage.read(output) == record
    assert [p.name for p in tmp_path.iterdir()] == [lineage.path_of(output).name], "nothing partial left behind"


def test_the_lumisections_of_several_outputs_are_unknown_if_one_has_none(tmp_path):
    a, b, c = (tmp_path / name for name in ("a.root", "b.root", "c.root"))
    lineage.write(a, [51, 52], [])
    lineage.write(b, [53], [])

    assert lineage.lumisections_of([a, b]) == {51, 52, 53}
    assert lineage.lumisections_of([a, b, c]) is None
    assert lineage.lumisections_of([]) == set()
    lineage.path_of(c).write_text("{not json", encoding="utf-8")
    assert lineage.read(c) is None
