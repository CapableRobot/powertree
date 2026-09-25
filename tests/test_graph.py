import os

from powertree.analysis import analyze
from powertree.graph import Stacks, _compress_refs, to_dot

RACK = os.path.join(os.path.dirname(__file__), "..", "examples", "rack", "rack.kdl")


def groups(st, kind):
    return sorted(tuple(g.members) for g in st.by_kind(kind))


def test_compress_refs():
    assert _compress_refs(["D:1", "D:2", "D:3", "D:5"]) == "D:1..3,5"
    assert _compress_refs(["D:1", "D:2"]) == "D:1,2"
    assert _compress_refs(["U1"]) == "U1"


def test_stacks_follow_configuration():
    a = analyze(RACK)
    st = Stacks(a, "all-run")
    assert groups(st, "b") == [("IO:1", "IO:2", "IO:3", "IO:4")]
    assert ("F:1", "F:2", "F:3", "F:4") in groups(st, "c")
    st = Stacks(a, "mostly-idle")
    assert groups(st, "b") == [("IO:1",), ("IO:2", "IO:3", "IO:4")]
    # the fuses and harness nets split with the boards they feed
    assert ("F:1",) in groups(st, "c") and ("F:2", "F:3", "F:4") in groups(st, "c")
    assert ("IO2_24V", "IO3_24V", "IO4_24V") in groups(st, "n")
    leds = [g for g in st.by_kind("f") if g.rep.endswith(".led") and g.rep.startswith("IO:2")]
    assert len(leds) == 1 and leds[0].n == 24


def test_expand_and_no_stack():
    a = analyze(RACK)
    st = Stacks(a, "mostly-idle", expand=["IO:3"])
    assert groups(st, "b") == [("IO:1",), ("IO:2", "IO:4"), ("IO:3",)]
    st = Stacks(a, "mostly-idle", stack=False)
    assert all(g.n == 1 for g in st.groups)


def test_dot_labels_and_collapse():
    a = analyze(RACK)
    dot = to_dot(a, "mostly-idle")
    assert "IO:2..4  ×3  (io-card)  scenario idle" in dot
    assert "IO{2..4}_24V  ×3" in dot
    assert "263 µA ×3 = 788 µA" in dot
    assert "total ×24:" in dot and "shape=box3d" in dot and "peripheries=2" in dot
    dot = to_dot(a, "mostly-idle", collapse_boards=True)
    assert "ports: VIN" in dot and "IO:2.V5" not in dot and ".led" not in dot
    plain = to_dot(a, "mostly-idle", stack=False)
    assert "box3d" not in plain and "×" not in plain


def test_errors_marked_on_stack(tmp_path):
    p = tmp_path / "d.kdl"
    p.write_text("""design d
net A
chip P { provider { out net=A v="5V" } }
chip D count=6 { consumer led { in net=A range="5.5V..6V"; load i="1mA" } }
""")
    a = analyze(str(p))
    dot = to_dot(a, "nominal")
    assert "errors: D:1, D:2, D:3, D:4 +2" in dot and "color=red" in dot


def test_chip_title_description_placement(tmp_path):
    p = tmp_path / "d.kdl"
    p.write_text("""design d
net A
chip U1 desc="eFuse" { provider { out net=A v="5V" } }
chip U2 part=TPS1 desc="eFuse" { consumer { in net=A; load i="1mA" } }
chip U3 desc="5V buck" { consumer { in net=A; load i="1mA" } }
chip K count=2 desc="Relay" { consumer { in net=A; load i="1mA" } }
""")
    dot = to_dot(analyze(str(p)), "nominal")
    assert 'label="U1  eFuse\nheat' in dot
    assert 'label="U2  TPS1\neFuse\nheat' in dot
    assert 'label="U3\n5V buck\nheat' in dot
    assert 'label="K:1,2  ×2  Relay\nheat' in dot
