from helpers import codes, run

SRC_OK = """
net A
chip P1 { provider { out net=A v="5V" } }
chip L1 { consumer { in net=A; load i="1mA" } }
"""


def test_ok(tmp_path):
    a = run(tmp_path, SRC_OK)
    assert a.ok and codes(a) == []


def test_structure_errors_with_suggestions(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" imx="1A" } }
    chip L1 { cosumer { in net=A } }
    chip L2 { consumer { in net=A range="1A..2A"; load i=5 } }
    """)
    msgs = [f.message for f in a.diags.errors]
    assert any("did you mean 'imax'" in m for m in msgs)
    assert any("did you mean 'consumer'" in m for m in msgs)
    assert any("voltage range" in m for m in msgs)
    assert any("bare number 5" in m for m in msgs)
    assert a.design is None


def test_type_annotation_units(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" imax=(mA)500 } }
    chip L1 { consumer { in net=A; load i=(mA)600 } }
    """)
    assert "overload" in codes(a, "error")


def test_reference_errors(tmp_path):
    a = run(tmp_path, """
    net VBUS
    chip P1 { provider { out net=VBUSS v="5V" } }
    chip L1 { consumer { in from=P2; load i="1mA" } }
    """)
    msgs = [f.message for f in a.diags.errors]
    assert any("did you mean 'VBUS'" in m for m in msgs)
    a = run(tmp_path, """
    net VBUS
    chip P1 { provider { out net=VBUS v="5V" } }
    chip L1 { consumer { in from=P2; load i="1mA" } }
    """)
    assert any("no chip 'P2'" in f.message for f in a.diags.errors)


def test_semantic_requirements(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A } }
    chip U1 { converter kind=buck { in net=A; out net=B v="3.3V" } }
    chip L1 { consumer { in net=B } }
    """)
    assert {"missing-voltage", "missing-efficiency", "missing-load"} <= set(codes(a, "error"))


def test_part_library_merge_and_override(tmp_path):
    a = run(tmp_path, """
    use "lib/parts.kdl"
    net A
    net B
    chip P1 { provider { out net=A v="12V" } }
    chip U1 part=REG {
        converter reg { in net=A; out net=B v="5V" }
    }
    chip L1 { consumer { in net=B; load i="1A" } }
    """, files={"lib/parts.kdl": """
    part REG {
        converter reg kind=buck {
            in range="4V..20V"
            out range="1V..10V" imax="2A"
            eff 0.8
        }
    }
    """})
    assert a.ok, a.diags.items
    r = a.results["nominal"].funcs["U1.reg"]
    assert r.imax == 2 and r.eff == 0.8


def test_part_setpoint_outside_adjust_range(tmp_path):
    a = run(tmp_path, """
    part REG { converter reg kind=buck { in; out range="1V..3V"; eff 0.9 } }
    net A
    net B
    chip P1 { provider { out net=A v="12V" } }
    chip U1 part=REG { converter reg { in net=A; out net=B v="5V" } }
    chip L1 { consumer { in net=B; load i="1A" } }
    """)
    assert "setpoint-range" in codes(a, "error")


def test_part_kind_mismatch(tmp_path):
    a = run(tmp_path, """
    part SW { switch sw { in; out; rdson "1mΩ" } }
    net A
    chip U1 part=SW { series sw { in net=A; out net=A; r "1mΩ" } }
    """)
    assert "kind-mismatch" in codes(a, "error")


def test_used_file_missing(tmp_path):
    a = run(tmp_path, 'use "nope.kdl"\n' + SRC_OK)
    assert "file-not-found" in codes(a, "error")


def test_duplicate_function_names(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" } }
    chip L1 { consumer { in net=A; load i="1mA" }; consumer { in net=A; load i="1mA" } }
    """)
    assert "duplicate-function" in codes(a, "error")


def test_direct_link_creates_anonymous_net(tmp_path):
    a = run(tmp_path, """
    chip P1 { provider { out v="5V" } }
    chip L1 { consumer { in from=P1.provider.out; load i="1mA" } }
    chip L2 { consumer { in from=P1; load i="2mA" } }
    """)
    assert a.ok
    nets = a.results["nominal"].nets
    assert list(nets) == ["~P1.provider.out"] and nets["~P1.provider.out"].i == 0.003


def test_direct_link_joins_named_net_and_conflicts(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    chip P1 { provider { out net=A v="5V" } }
    chip L1 { consumer { in from=P1; load i="1mA" } }
    chip L2 { consumer { in net=B from=P1; load i="1mA" } }
    """)
    assert "conflicting-link" in codes(a, "error")


def test_topology_rules(tmp_path):
    a = run(tmp_path, """
    net A
    net B
    net C
    net UNUSED
    chip P1 { provider { out net=A v="5V" } }
    chip P2 { provider { out net=A v="5V" } }
    chip L1 { consumer { in net=B; load i="1mA" } }
    chip S1 { series { in net=C; out net=C; r "1mΩ" } }
    chip L2 { consumer { in; load i="1mA" } }
    """)
    assert set(codes(a, "error")) == {"multiple-drivers", "undriven-net", "cycle", "unconnected-input"}
    assert "unused-net" in codes(a, "warning")


def test_shared_drivers_allowed(tmp_path):
    a = run(tmp_path, """
    net A
    chip P1 { provider { out net=A v="5V" share=#true } }
    chip P2 { provider { out net=A v="5V" share=#true } }
    chip L1 { consumer { in net=A; load i="1A" } }
    """)
    assert a.ok
    f = a.results["nominal"].funcs
    assert f["P1.provider"].iout == f["P2.provider"].iout == 0.5


def test_scenario_errors(tmp_path):
    a = run(tmp_path, SRC_OK + """
    scenario a base=b
    scenario b base=a
    scenario c base=zz { set L1 ii="1A"; set L9 on=#false; set P1 i="1A" }
    """)
    errs = codes(a, "error")
    assert errs.count("unknown-property") == 2
    assert {"scenario-cycle", "undefined-scenario", "bad-reference"} <= set(errs)


def test_waivers(tmp_path):
    src = """
    net A
    chip P1 { provider { out net=A v="5V" imax="1A" } }
    chip L1 { consumer { in net=A; load i="2A" } }
    waive overload P1 reason="bench supply is larger"
    waive input-range L1 reason="stale"
    """
    a = run(tmp_path, src)
    assert a.ok
    assert codes(a, include_waived=True).count("overload") == 1 and "overload" not in codes(a)
    assert "unused-waiver" in codes(a, "warning")
