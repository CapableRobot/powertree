import math

import pytest

from helpers import codes, run
from powertree.model import EffTable, Efficiency
from powertree.solver import efficiency

CHAIN = """
net VIN
net V5
net V33
chip J1 { provider { out net=VIN v="12V ±5%" imax="2A" } }
chip U1 { converter buck kind=buck { in net=VIN; out net=V5 v="5V" imax="3A"; iq "1mA"; eff 0.8 } }
chip U2 { converter ldo kind=linear { in net=V5; out net=V33 v="3.3V" imax="1A"; iq "100uA" } }
chip L1 { consumer { in net=V33; load i="100mA" imax="200mA" } }
chip L2 { consumer { in net=V5; load p="1W" } }
"""


def test_linear_and_switching_hand_calc(tmp_path):
    a = run(tmp_path, CHAIN)
    assert a.ok, a.diags.items
    r = a.results["nominal"]
    ldo = r.funcs["U2.ldo"]
    assert ldo.iin == pytest.approx(0.1001)                     # Iin = Iout + Iq
    assert ldo.heat == pytest.approx(5 * 0.1001 - 3.3 * 0.1)
    buck = r.funcs["U1.buck"]
    iout = 0.1001 + 1 / 5                                       # LDO input + 1 W at 5 V
    assert buck.iout == pytest.approx(iout)
    pin = 5 * iout / 0.8 + 12 * 1e-3
    assert buck.iin == pytest.approx(pin / 12)
    assert r.chip_heat["U1"] == pytest.approx(pin - 5 * iout)
    assert r.source_power == pytest.approx(pin)
    assert r.board_heat == pytest.approx(pin)                   # everything ends up as heat
    assert r.nets["VIN"].lo == pytest.approx(11.4)


def test_constant_power_behind_resistance_converges(tmp_path):
    # V = 5 - I*R, P = V*I  =>  R I^2 - 5 I + P = 0
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="5V" } }
    chip R1 { series { in net=A; out net=B; r "1Ω" } }
    chip L1 { consumer { in net=B; load p="2W" } }
    """)
    assert a.ok
    r = a.results["nominal"]
    i = (5 - math.sqrt(25 - 8)) / 2
    assert r.funcs["L1.consumer"].iin == pytest.approx(i, rel=1e-6)
    assert r.nets["B"].v == pytest.approx(5 - i, rel=1e-6)
    assert r.funcs["R1.series"].heat == pytest.approx(i * i, rel=1e-6)
    assert r.converged and r.iterations > 3


def test_resistive_load_and_provider_resistance(tmp_path):
    a = run(tmp_path, """
    net A
    chip B1 { provider { out net=A v="10V"; r "1Ω" } }
    chip L1 { consumer { in net=A; load r="9Ω" } }
    """)
    assert a.ok, a.diags.items
    r = a.results["nominal"]
    assert r.funcs["L1.consumer"].iin == pytest.approx(1.0)
    assert r.nets["A"].v == pytest.approx(9.0)
    assert r.chip_heat["B1"] == pytest.approx(1.0)


def test_collapse_reported(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="5V" } }
    chip R1 { series { in net=A; out net=B; r "10Ω" } }
    chip L1 { consumer { in net=B; load p="5W" } }
    """)
    assert set(codes(a, "error")) & {"collapse", "no-convergence"}


ORING = """
net ADP
net BAT
net SYS
chip J1 { provider { out net=ADP v="12V" } }
chip B1 { provider { out net=BAT v="14V" } }
chip U1 { oring { in net=ADP %s; in net=BAT %s; out net=SYS imax="5A"; vf "0.5V" } }
chip L1 { consumer { in net=SYS; load i="1A" } }
scenario normal
scenario unplugged { set J1 on=#false }
"""


def test_oring_highest_voltage_wins(tmp_path):
    a = run(tmp_path, ORING % ("", ""))
    r = a.results["normal"]
    assert r.funcs["B1.provider"].iout == 1 and r.funcs["J1.provider"].iout == 0
    assert r.nets["SYS"].v == pytest.approx(13.5)


def test_oring_priority_and_failover(tmp_path):
    a = run(tmp_path, ORING % ("priority=1", "priority=2"))
    r = a.results["normal"]
    assert r.funcs["J1.provider"].iout == 1 and r.nets["SYS"].v == pytest.approx(11.5)
    r = a.results["unplugged"]
    assert r.funcs["B1.provider"].iout == 1 and not r.funcs["J1.provider"].powered


def test_switch_off_unpowers_downstream(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="5V" } }
    chip S1 { switch { in net=A; out net=B; rdson "100mΩ" } }
    chip L1 { consumer { in net=B; load i="1A" } }
    scenario on
    scenario off { set S1 on=#false }
    """)
    on, off = a.results["on"], a.results["off"]
    assert on.nets["B"].v == pytest.approx(4.9) and on.chip_heat["S1"] == pytest.approx(0.1)
    assert off.nets["B"].v is None and off.funcs["L1.consumer"].iin == 0
    assert any(f.code == "unpowered" and f.scenario == "off" for f in a.diags.items)


def test_overload_and_rule_warning(tmp_path):
    a = run(tmp_path, CHAIN + """
    scenario nom
    scenario peak { loads max }
    rules { converter-load max="5%" }
    """)
    peak = [f for f in a.diags.items if f.scenario == "peak"]
    assert any(f.code == "converter-load" and f.target == "U2.ldo" for f in peak)
    a = run(tmp_path, CHAIN.replace('imax="1A"', 'imax="150mA"') + "scenario peak { loads max }")
    assert "overload" in codes(a, "error")


def test_input_range_and_headroom(tmp_path):
    base = """
    net A
    chip P1 { provider { out net=A v="5V ±5%%" } }
    chip L1 { consumer { in net=A %s; load i="1mA" } }
    rules { input-headroom min="5%%" }
    """
    a = run(tmp_path, base % 'range="4.8V..5.5V"')
    assert "input-range" in codes(a, "error")
    a = run(tmp_path, base % 'range="4.5V..5.4V"')
    assert codes(a) == ["input-headroom"]
    a = run(tmp_path, base % 'v="5V ±12%"')
    assert codes(a) == []


def test_ldo_dropout(tmp_path):
    src = """
    net A
    net B
    chip P1 { provider { out net=A v="%s" } }
    chip U1 { converter ldo kind=linear { in net=A; out net=B v="3.3V"; dropout "300mV" at="1A" } }
    chip L1 { consumer { in net=B; load i="1A" } }
    """
    assert "dropout" in codes(run(tmp_path, src % "3.5V"), "error")
    a = run(tmp_path, src % "3.65V ±5%")
    assert codes(a) == ["dropout-margin"]
    r = run(tmp_path, src % "3.5V").results["nominal"]
    assert r.nets["B"].v == pytest.approx(3.2)                 # output follows input in dropout
    assert codes(run(tmp_path, src % "5V")) == []


def test_buck_boost_sanity(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="3V" } }
    chip U1 { converter kind=buck { in net=A; out net=B v="5V"; eff 0.9 } }
    chip L1 { consumer { in net=B; load i="1mA" } }
    """)
    assert "converter-kind" in codes(a, "error")


def test_offboard_power_not_heat(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" } }
    chip L1 { consumer { in net=A; load i="1A" offboard=#true } }
    """)
    r = a.results["nominal"]
    assert r.offboard_power == 5 and r.board_heat == 0


def test_scenario_composition_order(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" } }
    chip L1 { consumer { in net=A; load i="1A" imin="10mA" imax="2A" } }
    chip L2 { consumer { in net=A; load i="1A" imax="3A" } }
    scenario base1 { loads max; set L1 p="1W" }
    scenario base2 { loads min }
    scenario child base="base1, base2" { set L1 i="7mA" }
    scenario other base="base2, base1"
    """)
    child, other = a.results["child"], a.results["other"]
    assert child.loads == "min" and child.funcs["L1.consumer"].iin == pytest.approx(0.007)
    assert child.funcs["L2.consumer"].iin == pytest.approx(1.0)       # min falls back to nominal
    assert other.loads == "max" and other.funcs["L1.consumer"].iin == pytest.approx(0.2)
    assert other.funcs["L2.consumer"].iin == pytest.approx(3.0)


def test_efficiency_table_interpolation():
    t12 = EffTable(12, 5, [(0.01, 0.6), (0.1, 0.8), (1.0, 0.9)])
    t24 = EffTable(24, 5, [(0.01, 0.5), (0.1, 0.7), (1.0, 0.8)])
    e = Efficiency(tables=[t12, t24])
    v, notes = efficiency(e, 12, 5, math.sqrt(0.1 * 1.0))        # log-midpoint
    assert v == pytest.approx(0.85) and notes == []
    v, _ = efficiency(e, 18, 5, 0.1)                              # vin midpoint
    assert v == pytest.approx(0.75)
    v, notes = efficiency(e, 12, 5, 2.0)
    assert v == pytest.approx(0.9) and "above" in notes[0]
    v, notes = efficiency(e, 40, 5, 0.1)
    assert v == pytest.approx(0.7) and "outside" in notes[0]
    _, notes = efficiency(e, 12, 3.3, 0.1)
    assert "no curve near" in notes[0]


def test_efficiency_expression_out_of_range(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="12V" } }
    chip U1 { converter kind=buck { in net=A; out net=B v="5V"; eff expr="1.2 - 0.1 * iout / 1A" } }
    chip L1 { consumer { in net=B; load i="1A" } }
    """)
    assert "efficiency" in codes(a, "error")


def test_examples_are_clean():
    import os
    from powertree.analysis import analyze
    ex = os.path.join(os.path.dirname(__file__), "..", "examples")
    for name in ("sensor-board.kdl", "ups.kdl"):
        a = analyze(os.path.join(ex, name))
        assert a.ok and not [f for f in a.diags.items if f.severity != "info" and not f.waived], name
