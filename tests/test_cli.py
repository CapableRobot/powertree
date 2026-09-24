import json
import os

import pytest

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
    assert "Function " in out and "IO:2.U2.buck" in out and "PS1.provider" in out
    assert "FAN1.consumer" not in out and ".led" not in out and ".coil" not in out
    assert "Net " not in out and "Chip " not in out
    assert main(["report", os.path.join(EX, "rack", "rack.kdl"), "-s", "peak"]) == 0
    assert "Function " in capsys.readouterr().out


def test_png_and_render_errors(tmp_path, monkeypatch, capsys):
    import shutil
    from powertree.render import RenderError, find_dot, render
    ups = os.path.join(EX, "ups.kdl")
    with pytest.raises(RenderError, match="unsupported"):
        render("digraph{}", str(tmp_path / "x.foo"), "foo")
    monkeypatch.setenv("POWERTREE_DOT", str(tmp_path / "missing-dot"))
    with pytest.raises(RenderError, match="POWERTREE_DOT"):
        find_dot()
    assert main(["dot", ups, "-o", str(tmp_path / "g.png")]) == 2
    assert "not found" in capsys.readouterr().err
    monkeypatch.delenv("POWERTREE_DOT")
    if not shutil.which("dot"):
        pytest.skip("Graphviz not installed")
    lo, hi = tmp_path / "lo.png", tmp_path / "hi.png"
    assert main(["dot", ups, "-o", str(lo), "--dpi", "72"]) == 0
    assert main(["dot", ups, "-o", str(hi), "--dpi", "200"]) == 0
    assert lo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert hi.stat().st_size > lo.stat().st_size
    assert main(["dot", ups, "-o", str(tmp_path / "g.out"), "-T", "svg"]) == 0
    assert (tmp_path / "g.out").read_text().lstrip().startswith("<?xml")
