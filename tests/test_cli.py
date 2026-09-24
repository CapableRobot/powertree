import json
import os

from powertree.cli import main

EX = os.path.join(os.path.dirname(__file__), "..", "examples")


def test_report_json(capsys):
    assert main(["report", os.path.join(EX, "sensor-board.kdl"), "-s", "peak", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["results"]["peak"]["functions"]["U1.buck"]["iout"] > 0.5


def test_dot_and_svg(tmp_path, capsys):
    assert main(["dot", os.path.join(EX, "ups.kdl"), "-s", "on-battery"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("digraph") and '"cluster_U3"' in out
    svg = tmp_path / "g.svg"
    import shutil
    if shutil.which("dot"):
        assert main(["dot", os.path.join(EX, "ups.kdl"), "-o", str(svg)]) == 0
        assert svg.read_text().lstrip().startswith("<?xml")


def test_check_exit_codes(tmp_path, capsys):
    bad = tmp_path / "bad.kdl"
    bad.write_text('design x\nnet A\nchip L1 { consumer { in net=A; load i="1A" } }\n')
    assert main(["check", str(bad)]) == 1
    assert "undriven-net" in capsys.readouterr().out
    assert main(["check", os.path.join(EX, "sensor-board.kdl"), "--strict"]) == 0


def test_schema_reference(capsys):
    assert main(["schema"]) == 0
    out = capsys.readouterr().out
    assert "converter [name]" in out and "rdson <ohms>" in out


def test_report_summary(capsys):
    assert main(["report", os.path.join(EX, "rack", "rack.kdl"), "-s", "peak", "--summary"]) == 0
    out = capsys.readouterr().out
    assert "Board " in out and "Group " in out and "Source output" in out
    assert "Function " not in out and "Net " not in out and "Chip " not in out
    assert main(["report", os.path.join(EX, "rack", "rack.kdl"), "-s", "peak"]) == 0
    assert "Function " in capsys.readouterr().out
