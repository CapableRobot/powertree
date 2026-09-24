import os
import subprocess

import pytest

from helpers import codes, run
from powertree import paths
from powertree.analysis import analyze
from powertree.report import findings_text, scenario_text

CARD = """
design card
port VIN net=VIN dir=in
net VIN
net V5
chip U1 { converter buck kind=buck { in net=VIN range="6V..30V"; out net=V5 v="5V" imax="1A"; eff 0.9 } }
chip D count=4 desc="LED {n}" { consumer led { in net=V5; load i="10mA" imin="0A" imax="20mA" } }
scenario run
scenario idle { loads min; set D:2..3 on=#false }
waive converter-load U1 reason="card-level review"
"""


def test_paths_selectors():
    assert paths.match("IO:*.U3", "IO:7.U3")
    assert paths.match("IO:2..4.D:*", "IO:3.D:12")
    assert not paths.match("IO:2..4.D:*", "IO:5.D:1")
    assert paths.match("D:3", "D:3") and not paths.match("D:3", "D:30")
    assert paths.match_prefix("IO:*", "IO:1.U3.buck")
    assert paths.group_key("IO:3.D:12.led") == "IO:*.D:*.led"
    assert paths.split("IO:2..4.U3") == ["IO:2..4", "U3"]


def test_chip_count_and_templates(tmp_path):
    a = run(tmp_path, """
    net V5
    net "OUT{n}" count=3
    chip P1 { provider { out net=V5 v="5V" } }
    chip S count=3 desc="switch {n}" { switch { in net=V5; out net="OUT{n}"; rdson "1Ω" } }
    chip L count=3 { consumer { in net="OUT{n}"; load i="10mA" } }
    """)
    assert a.ok, a.diags.items
    d = a.design
    assert list(d.nets) == ["V5", "OUT1", "OUT2", "OUT3"]
    assert d.chips["S:2"].desc == "switch 2"
    assert d.chips["L:3"].functions["consumer"].ins[0].net == "OUT3"
    r = a.results["nominal"]
    assert r.funcs["P1.provider"].iout == pytest.approx(0.03)


def test_template_errors(tmp_path):
    a = run(tmp_path, """
    net "A{n}"
    net B count=2
    chip P1 { provider { out net="X{n}" v="5V" } }
    """)
    assert codes(a, "error").count("template") == 3


def test_board_instances_ports_and_scenarios(tmp_path):
    a = run(tmp_path, """
    use "card.kdl"
    net BUS
    net "S{n}" count=3
    chip PS { provider { out net=BUS v="24V" } }
    chip F count=3 { series { in net=BUS; out net="S{n}"; r "0.1Ω" } }
    board C count=3 design=card { port VIN net="S{n}" }
    scenario all { set C:* scenario=run }
    scenario mixed base=all { loads max; set C:2..3 scenario=idle }
    scenario pulled base=all { set C:1 on=#false }
    """, files={"card.kdl": CARD})
    assert a.ok, a.diags.items
    d = a.design
    assert set(d.boards) == {"C:1", "C:2", "C:3"}
    assert d.chips["C:2.D:4"].functions["led"].ins[0].net == "C:2.V5"
    assert d.chips["C:2.U1"].functions["buck"].ins[0].net == "S2"      # port net aliased to parent
    m = a.results["mixed"].funcs
    assert m["C:1.D:1.led"].iin == pytest.approx(0.02)                # system loads max
    assert m["C:2.D:1.led"].iin == 0                                  # board idle: loads min (0 A)
    assert not m["C:3.D:2.led"].on and m["C:3.D:4.led"].on            # D:2..3 off inside board
    assert a.results["mixed"].board_scenarios == {"C:1": "run", "C:2": "idle", "C:3": "idle"}
    p = a.results["pulled"].funcs
    assert not p["C:1.U1.buck"].on and p["C:2.U1.buck"].powered
    assert a.results["pulled"].funcs["F:1.series"].iout == 0
    # board waiver was imported with the instance prefix
    assert any(w.target == "C:2.U1" for w in d.waivers)
    text = scenario_text(a, a.results["mixed"])
    assert "C:*.D:*" in text and "Board" in text


def test_board_errors(tmp_path):
    files = {"card.kdl": CARD}
    a = run(tmp_path, """
    use "card.kdl"
    net BUS
    chip PS { provider { out net=BUS v="24V" } }
    board C1 design=card { port VINN net=BUS }
    board C2 design=crad { port VIN net=BUS }
    board C.3 design=card { port VIN net=BUS }
    """, files=files)
    errs = codes(a, "error")
    assert {"unknown-port", "unbound-port", "unknown-design", "bad-name"} <= set(errs)
    msgs = " ".join(f.message for f in a.diags.errors)
    assert "did you mean 'VIN'" in msgs and "did you mean 'card'" in msgs


def test_board_scenario_errors(tmp_path):
    a = run(tmp_path, """
    use "card.kdl"
    net BUS
    chip PS { provider { out net=BUS v="24V" } }
    board C design=card { port VIN net=BUS }
    scenario s { set C scenario=idel; set C i="1A"; set X:* on=#false }
    """, files={"card.kdl": CARD})
    errs = codes(a, "error")
    assert {"undefined-scenario", "unknown-property", "bad-reference"} <= set(errs)


def test_nested_boards_and_cycle(tmp_path):
    files = {
        "card.kdl": CARD,
        "shelf.kdl": """
        design shelf
        use "card.kdl"
        port IN net=IN
        net IN
        board K count=2 design=card { port VIN net=IN }
        scenario quiet { set K:* scenario=idle }
        """,
        "loop.kdl": 'design loop\nuse "loop2.kdl"\nboard X design=loop2\n',
        "loop2.kdl": 'design loop2\nuse "loop.kdl"\nboard Y design=loop\n',
    }
    a = run(tmp_path, """
    use "shelf.kdl"
    net BUS
    chip PS { provider { out net=BUS v="24V" } }
    board SH design=shelf { port IN net=BUS }
    scenario q { set SH scenario=quiet }
    """, files=files)
    assert a.ok, a.diags.items
    assert "SH.K:2.D:1" in a.design.chips
    r = a.results["q"]
    assert r.funcs["SH.K:1.D:1.led"].iin == 0 and not r.funcs["SH.K:2.D:3.led"].on
    a = run(tmp_path, 'use "loop.kdl"\nboard L design=loop\n', files=files)
    assert "board-cycle" in codes(a, "error")


def test_standalone_board_hint(tmp_path):
    p = tmp_path / "card.kdl"
    p.write_text(CARD)
    a = analyze(str(p))
    assert any("port 'VIN'" in f.message for f in a.diags.errors)


LIB = """
part REG { converter reg kind=buck { in range="4V..30V"; out imax="2A"; eff 0.9 } }
"""


def _proj(tmp_path, libs):
    for name, text in libs.items():
        (tmp_path / name).mkdir(exist_ok=True)
        (tmp_path / name / "parts.kdl").write_text(text)
    (tmp_path / "powertree-project.kdl").write_text(
        "\n".join(f'library {n} path="{n}"' for n in libs) + "\n")


DESIGN = """
use "corp:parts.kdl"
use "alt:parts.kdl"
net A
net B
chip P { provider { out net=A v="12V" } }
chip U1 part=%s { converter reg { in net=A; out net=B v="5V" } }
chip L { consumer { in net=B; load i="1A" } }
"""


def test_library_roots_and_namespaces(tmp_path):
    _proj(tmp_path, {"corp": LIB, "alt": LIB.replace('imax="2A"', 'imax="3A"')})
    a = run(tmp_path, DESIGN % "REG")
    assert "ambiguous-part" in codes(a, "error")
    a = run(tmp_path, DESIGN % "alt:REG")
    assert a.ok and a.results["nominal"].funcs["U1.reg"].imax == 3
    libs = {l.name: l for l in a.design.libraries}
    assert set(libs) == {"corp", "alt"} and len(libs["corp"].sha) == 12
    a = run(tmp_path, DESIGN.replace('"alt:', '"nope:') % "corp:REG")
    assert "library" in codes(a, "error")


def test_library_override_cli_and_env(tmp_path, monkeypatch):
    _proj(tmp_path, {"corp": LIB, "alt": LIB})
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "parts.kdl").write_text(LIB.replace('imax="2A"', 'imax="7A"'))
    src = DESIGN.replace('use "alt:parts.kdl"\n', "") % "corp:REG"
    run(tmp_path, src)
    p = str(tmp_path / "design.kdl")
    assert analyze(p).results["nominal"].funcs["U1.reg"].imax == 2
    monkeypatch.setenv("POWERTREE_LIBS", f"corp={tmp_path / 'other'}")
    a = analyze(p)
    assert a.results["nominal"].funcs["U1.reg"].imax == 7 and "POWERTREE_LIBS" in a.design.libraries[0].origin
    from powertree.loader import load
    d, _ = load(p, [f"corp={tmp_path / 'corp'}"])
    assert d.libraries[0].origin == "--lib"


def test_library_git_stamp(tmp_path):
    _proj(tmp_path, {"corp": LIB, "alt": LIB})
    lib = tmp_path / "corp"
    try:
        subprocess.run(["git", "init", "-q"], cwd=lib, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "."], cwd=lib, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "x"],
                       cwd=lib, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    a = run(tmp_path, DESIGN.replace('use "alt:parts.kdl"\n', "") % "corp:REG")
    git = a.design.libraries[0].git
    assert git and not git.endswith("+dirty")
    (lib / "parts.kdl").write_text(LIB + "\n// edit\n")
    a = analyze(str(tmp_path / "design.kdl"))
    assert a.design.libraries[0].git.endswith("+dirty")


def test_waiver_selectors(tmp_path):
    a = run(tmp_path, """
    net A
    chip P { provider { out net=A v="5V" imax="1A" } }
    chip L count=3 { consumer { in net=A; load i="500mA" } }
    waive overload P reason="ok"
    waive overload L:* reason="unused on purpose"
    """)
    assert a.ok
    assert "unused-waiver" in codes(a, "warning")


def test_power_table_conserves(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    net C
    chip P { provider { out net=A v="12V"; r "0.1Ω" } }
    chip U { converter kind=buck { in net=A; out net=B v="5V"; eff 0.8 } }
    chip S { switch { in net=B; out net=C; rdson "0.5Ω" } }
    chip L1 { consumer { in net=C; load i="1A" } }
    chip L2 { consumer { in net=B; load i="1A" offboard=#true } }
    """)
    r = a.results["nominal"]
    total = sum(v for k, v in r.losses.items() if k != "provider") + r.load_power
    assert total == pytest.approx(r.source_power)
    text = scenario_text(a, r)
    for label in ("Converter loss", "Switch loss", "On-board consumers", "External consumers",
                  "Source internal loss", "System efficiency"):
        assert label in text
    assert "\nNets\n" not in text and text.count("Net ") >= 1


def test_findings_columns_align(tmp_path):
    a = run(tmp_path, """
    net A
    net UNUSED
    chip P { provider { out net=A v="5V" imax="100mA" } }
    chip L { consumer { in net=A; load i="90mA" } }
    scenario a-long-name
    scenario b
    """)
    lines = [l for l in findings_text(a.diags).splitlines() if l and "error(s)" not in l]
    assert len(lines) >= 3
    # severity, scenario and location are padded, so the message starts at the same column
    offsets = set()
    for l in lines:
        sev, scen, loc = l.split()[:3]
        offsets.add(l.index(l.split()[3], l.index(loc) + len(loc)))
    assert len(offsets) == 1


def test_standalone_block_and_composition(tmp_path):
    card = CARD + """
    standalone {
        chip TP desc="bench" { provider { out net=VIN v="24V ±5%" } }
        scenario low { set TP v="7V" }
    }
    """
    p = tmp_path / "card.kdl"
    p.write_text(card)
    a = analyze(str(p), ["idle+low", "low+idle", "run"])
    assert a.ok, a.diags.items
    assert "standalone" in codes(a, "info")
    r = a.results["idle+low"]
    assert r.nets["VIN"].v == 7 and r.loads == "min"
    assert not r.funcs["D:2.led"].on
    assert a.results["run"].nets["VIN"].v == 24
    assert "undefined-scenario" in codes(analyze(str(p), ["run+nope"]), "error")
    # instantiated as a board, the standalone block is ignored: no extra driver on the port net
    a = run(tmp_path, """
    use "card.kdl"
    net BUS
    chip PS { provider { out net=BUS v="24V" } }
    board C design=card { port VIN net=BUS }
    """)
    assert a.ok and "C.TP" not in a.design.chips and "standalone" not in codes(a)
