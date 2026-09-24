"""Text and JSON reports, and Graphviz DOT output."""
from __future__ import annotations

import json

from .analysis import Analysis
from .diagnostics import Diagnostics
from .solver import Result
from .units import AMP, VOLT, WATT, fmt, pct


def _table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]

    def line(cells):
        return "  ".join(c.rjust(w) if i in right else c.ljust(w)
                         for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    return [line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows]


def _v(x):
    return fmt(x, VOLT) if x is not None else "off"


def scenario_text(a: Analysis, r: Result) -> str:
    d = a.design
    out = [f"=== {d.name} — scenario '{r.scenario}' (loads {r.loads}) "
           f"— {'converged' if r.converged else 'NOT converged'} in {r.iterations} iterations", ""]

    rows = []
    for n in d.nets.values():
        nr = r.nets[n.name]
        drivers = ", ".join(p.func.path for p in n.drivers)
        rng = f"{fmt(nr.lo, VOLT)} .. {fmt(nr.hi, VOLT)}" if nr.powered else ""
        rows.append([n.name if not n.anonymous else f"{n.name} (direct)", _v(nr.v), rng,
                     fmt(nr.i, AMP), drivers])
    out += ["Nets"] + _table(["net", "V nom", "V range", "load", "source"], rows, {1, 3}) + [""]

    rows = []
    for path, fr in r.funcs.items():
        f = fr.func
        kind = f.kind + (f"/{f.subkind}" if f.subkind else "")
        if not fr.on:
            state = "off"
        elif not fr.powered:
            state = "unpowered"
        else:
            state = ""
        rows.append([path, kind,
                     fmt(fr.vin, VOLT) if fr.vin is not None else "",
                     fmt(fr.vout, VOLT) if fr.vout is not None else "",
                     fmt(fr.iin, AMP) if f.ins else "",
                     fmt(fr.iout, AMP) if f.outs else "",
                     pct(fr.eff) if fr.eff is not None else "",
                     fmt(fr.heat, WATT),
                     pct(fr.loading) if fr.loading is not None else "",
                     state])
    out += ["Functions"] + _table(["function", "kind", "Vin", "Vout", "Iin", "Iout", "eff", "heat",
                                   "load%", "state"], rows, {2, 3, 4, 5, 6, 7, 8}) + [""]

    rows = []
    for ref, chip in d.chips.items():
        sinks = [f for f in chip.functions.values() if f.kind != "provider"]
        pin = fmt(sum(r.funcs[f.path].pin for f in sinks), WATT) if sinks else "—"
        rows.append([ref, chip.part or "", chip.desc or "", fmt(r.chip_heat[ref], WATT), pin])
    rows.sort(key=lambda x: -r.chip_heat[x[0]])
    out += ["Chips (sorted by heat)"] + _table(["chip", "part", "desc", "heat", "power in"], rows, {3, 4})
    out += ["",
            f"Source power:      {fmt(r.source_power, WATT)}",
            f"Consumer power:    {fmt(r.load_power, WATT)}"
            + (f"  (of which off-board {fmt(r.offboard_power, WATT)})" if r.offboard_power else ""),
            f"Heat on board:     {fmt(r.board_heat, WATT)}",
            f"System efficiency: {pct(r.system_efficiency) if r.system_efficiency is not None else '—'}",
            ""]
    return "\n".join(out)


def findings_text(diags: Diagnostics) -> str:
    items = diags.sorted()
    if not items:
        return "No findings."
    n_err = sum(1 for f in items if f.severity == "error" and not f.waived)
    n_warn = sum(1 for f in items if f.severity == "warning" and not f.waived)
    n_waived = sum(1 for f in items if f.waived)
    lines = [str(f) for f in items]
    lines.append(f"\n{n_err} error(s), {n_warn} warning(s), {n_waived} waived")
    return "\n".join(lines)


def to_json(a: Analysis) -> str:
    def res(r: Result):
        return {
            "scenario": r.scenario, "loads": r.loads, "converged": r.converged, "iterations": r.iterations,
            "nets": {k: vars(v) for k, v in r.nets.items()},
            "functions": {p: {"kind": fr.func.kind, "subkind": fr.func.subkind, "chip": fr.func.chip.ref,
                              "on": fr.on, "powered": fr.powered, "vin": fr.vin, "vout": fr.vout,
                              "iin": fr.iin, "iout": fr.iout, "pin": fr.pin, "pout": fr.pout,
                              "heat": fr.heat, "eff": fr.eff, "loading": fr.loading, "imax": fr.imax,
                              "offboard": fr.offboard}
                          for p, fr in r.funcs.items()},
            "chip_heat": r.chip_heat,
            "totals": {"source_power": r.source_power, "load_power": r.load_power,
                       "offboard_power": r.offboard_power, "board_heat": r.board_heat,
                       "system_efficiency": r.system_efficiency},
        }
    return json.dumps({"design": a.design.name if a.design else None,
                       "results": {k: res(v) for k, v in a.results.items()},
                       "findings": [f.to_dict() for f in a.diags.sorted()]}, indent=2)


# ---------------------------------------------------------------------------
_FILL = {"provider": "#d9ead3", "converter": "#cfe2f3", "switch": "#fff2cc", "series": "#eeeeee",
         "oring": "#fce5cd", "consumer": "#ead1dc"}


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_dot(a: Analysis, scenario: str) -> str:
    d, r = a.design, a.results[scenario]
    bad = {f.target for f in a.diags.items
           if f.scenario in (None, scenario) and f.severity == "error" and not f.waived and f.target}
    warn = {f.target for f in a.diags.items
            if f.scenario in (None, scenario) and f.severity == "warning" and not f.waived and f.target}
    title = (f"{d.name} — {scenario}  |  source {fmt(r.source_power, WATT)}, "
             f"heat {fmt(r.board_heat, WATT)}")
    L = ["digraph power {", "  rankdir=LR; newrank=true; nodesep=0.25; ranksep=0.6;",
         f"  label={_q(title)}; labelloc=t; fontname=Helvetica;",
         '  node [fontname=Helvetica, fontsize=10, shape=box, style="rounded,filled"];',
         "  edge [fontname=Helvetica, fontsize=9, color=gray40];"]
    for ref, chip in d.chips.items():
        title = f"{ref}" + (f"  {chip.part}" if chip.part else "") + (f"\n{chip.desc}" if chip.desc else "")
        title += f"\nheat {fmt(r.chip_heat[ref], WATT)}"
        L.append(f"  subgraph {_q('cluster_' + ref)} {{")
        L.append(f"    label={_q(title)}; style=rounded; color=gray60; fontsize=10;")
        for f in chip.functions.values():
            fr = r.funcs[f.path]
            kind = f.kind + (f"/{f.subkind}" if f.subkind else "")
            lines = [kind if f.name == f.kind else f"{f.name}  ({kind})"]
            if f.kind == "consumer":
                lines.append(f"{_v(fr.vin)}  {fmt(fr.iin, AMP)}  {fmt(fr.pin, WATT)}")
            elif f.kind == "provider":
                lines.append(f"{_v(fr.vout)}  {fmt(fr.iout, AMP)}")
            else:
                lines.append(f"{_v(fr.vin)} → {_v(fr.vout)}")
                extra = f"{fmt(fr.iout, AMP)}"
                if fr.eff is not None:
                    extra += f"  η {pct(fr.eff)}"
                lines.append(extra + f"  heat {fmt(fr.heat, WATT)}")
            if fr.loading is not None:
                lines.append(f"load {pct(fr.loading)} of {fmt(fr.imax, AMP)}")
            if not fr.on:
                lines.append("OFF")
            elif not fr.powered:
                lines.append("unpowered")
            fill = _FILL[f.kind] if fr.powered else "#f3f3f3"
            color = "red" if f.path in bad else ("darkorange" if f.path in warn else "gray30")
            pen = 2.5 if color != "gray30" else 1
            L.append(f"    {_q(f.path)} [label={_q(chr(10).join(lines))}, fillcolor={_q(fill)}, "
                     f"color={color}, penwidth={pen}];")
        L.append("  }")
    for n in d.nets.values():
        nr = r.nets[n.name]
        label = ("(direct)" if n.anonymous else n.name) + f"\n{_v(nr.v)}\n{fmt(nr.i, AMP)}"
        color = "red" if n.name in bad else "gray30"
        L.append(f"  {_q('net:' + n.name)} [shape=ellipse, style=filled, fillcolor={_q('#ffffff' if nr.powered else '#f3f3f3')}, "
                 f"color={color}, label={_q(label)}];")
        for p in n.drivers:
            i = r.funcs[p.func.path].iout
            L.append(f"  {_q(p.func.path)} -> {_q('net:' + n.name)} [label={_q(fmt(i, AMP))}];")
        for p in n.sinks:
            fr = r.funcs[p.func.path]
            i = fr.iin if p.func.kind != "oring" else (fr.iin if fr.active_in is p else 0.0)
            style = "" if i > 0 else ", style=dashed"
            L.append(f"  {_q('net:' + n.name)} -> {_q(p.func.path)} [label={_q(fmt(i, AMP))}{style}];")
    L.append("}")
    return "\n".join(L)
