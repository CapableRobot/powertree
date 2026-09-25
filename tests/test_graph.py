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


def test_fold_long_chains(tmp_path):
    from powertree.graph import _fold_chains
    edges = [("f:P", "n:0"), ("n:0", "f:A"), ("f:A", "n:1"), ("n:1", "f:B"), ("f:B", "n:2"),
             ("n:2", "f:C"), ("f:C", "n:3"), ("n:3", "f:D"), ("f:D", "n:4"), ("n:4", "f:E")]
    loose, anchors, chains = _fold_chains(edges, 2)
    assert loose == {("n:1", "f:B"), ("n:3", "f:D")}
    assert anchors == []                       # head is a source: later rows start at rank 0
    assert chains == [("", ["f:P", "n:0", "f:A", "n:1", "f:B", "n:2", "f:C", "n:3", "f:D", "n:4", "f:E"])]
    loose, _, chains = _fold_chains(edges, 0)
    assert loose == set() and chains == []
    # a branch ends the chain: n:1 feeding two functions is not a chain link
    loose, _, _ = _fold_chains(edges + [("n:1", "f:X")], 2)
    assert ("n:1", "f:B") not in loose
    # a function that shares its chip with other functions, or sits in another board, ends it too
    loose, _, _ = _fold_chains(edges, 2, solo=lambda x: x != "f:B")
    assert ("n:1", "f:B") not in loose
    loose, _, _ = _fold_chains(edges, 2, level=lambda x: "b" if x in ("n:3", "f:D", "n:4", "f:E") else "")
    assert ("n:1", "f:B") in loose and ("n:3", "f:D") not in loose

    p = tmp_path / "d.kdl"
    p.write_text("""design d
net VIN
net A
net B
net C
chip J { provider { out net=VIN v="24V" } }
chip X { consumer { in net=VIN; load i="1mA" } }
chip F { series { in net=VIN; out net=A; r "1mΩ" } }
chip R { series { in net=A; out net=B; r "1mΩ" } }
chip Q { switch { in net=B; out net=C; rdson "1mΩ" } }
chip L { consumer { in net=C; load i="1A" } }
""")
    a = analyze(str(p))
    dot = to_dot(a, "nominal", wrap=2, rankdir="TB")
    assert "rankdir=TB" in dot
    assert '"f:Q.switch" -> "n:B" [dir=back, weight=0, tailport=w, headport=e];' in dot
    assert '{ rank=same; "f:F.series" "f:Q.switch" }' in dot
    dot = to_dot(a, "nominal", wrap=2)
    assert '"f:Q.switch" -> "n:B" [dir=back, weight=0, tailport=n, headport=s];' in dot
    assert '"f:Q.switch" [shape=plain' in dot            # chain chips are single nodes
    assert '"cluster_chain_0"' in dot and "compound=true" in dot
    assert '"n:VIN" -> "f:Q.switch" [style=invis]' in dot
    assert "constraint=false" not in to_dot(a, "nominal")


def test_edge_labels_only_where_a_net_has_several_edges(tmp_path):
    p = tmp_path / "d.kdl"
    p.write_text("""design d
net A
net B
chip P { provider { out net=A v="5V" } }
chip S { switch { in net=A; out net=B; rdson "1mΩ" } }
chip L1 { consumer { in net=B; load i="1A" } }
chip L2 { consumer { in net=B; load i="2A" } }
chip R { series { in net=A; r "1mΩ" } }
chip L3 { consumer { in from=R; load i="3A" } }
""")
    dot = to_dot(analyze(str(p)), "nominal")
    assert '"f:P.provider" -> "n:A";' in dot                     # single source: no label
    assert '"n:A" -> "f:S.switch" [label="3 A"]' in dot          # A has two loads
    assert '"f:S.switch" -> "n:B";' in dot
    assert '"n:B" -> "f:L1.consumer" [label="1 A"]' in dot
    # unnamed net: no bubble, direct edge, unlabelled (one load)
    assert '"f:R.series" -> "f:L3.consumer";' in dot and "(direct)" not in dot


def test_checked_layout_picks_variant(tmp_path):
    from powertree.graph import _row_check
    rows = [[["f:A", "n:1", "f:B"], ["f:C", "n:2"]]]
    good = {"f:A": (0, 10), "n:1": (1, 10), "f:B": (2, 10), "f:C": (0, 8), "n:2": (1, 8)}
    assert _row_check(rows, good, "LR") == (True, True)
    flipped = {**good, "f:C": (0, 12), "n:2": (1, 12)}
    assert _row_check(rows, flipped, "LR") == (False, True)
    between = {**good, "f:X": (0, 9)}
    assert _row_check(rows, between, "LR") == (True, False)

    p = tmp_path / "d.kdl"
    p.write_text("""design d
net A
net B
net C
chip J { provider { out net=A v="24V" } }
chip F { series { in net=A; out net=B; r "1mΩ" } }
chip R { series { in net=B; out net=C; r "1mΩ" } }
chip L { consumer { in net=C; load i="1A" } }
""")
    a = analyze(str(p))
    calls = []

    def fake_layout(text):                      # every variant comes out upside down
        calls.append(text)
        import re
        pos = {}
        for i, name in enumerate(re.findall(r'^\s*"([fn]:[^"]+)" \[', text, re.M)):
            pos[name] = (0.0 if name in ("f:J.provider", "f:R.series") else 1.0 + i,
                         5.0 if name in ("f:R.series", "n:C", "f:L.consumer") else 1.0)
        return pos
    dot = to_dot(a, "nominal", wrap=2, layout=fake_layout)
    assert len(calls) == 4                         # all variants tried
    assert "tailport=s, headport=n" in dot         # rows reversed: return edge turned round
