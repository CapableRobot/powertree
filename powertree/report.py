"""Text and JSON reports, and Graphviz DOT output."""
from __future__ import annotations

import json
import os

from .analysis import Analysis
from .diagnostics import Diagnostics
from .solver import Result
from .paths import group_key
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


def _loc(span) -> str:
    if span is None:
        return ""
    f = span.file
    try:
        rel = os.path.relpath(f)
        f = rel if len(rel) < len(f) else f
    except ValueError:
        pass
    return f"{f}:{span.line}:{span.col}"


def libraries_text(a: Analysis) -> list[str]:
    out = []
    for lib in a.design.libraries:
        git = f"git {lib.git}" if lib.git else "not a git checkout"
        out.append(f"Library {lib.name}: {lib.path} ({git}, content {lib.sha}; from {lib.origin})")
    return out


def _board_power(r: Result, d, b) -> float:
    nets = set(b.ports.values())
    p = 0.0
    for f in d.functions():
        if not f.path.startswith(b.prefix):
            continue
        fr = r.funcs[f.path]
        for port in f.ins:
            if port.net in nets and fr.vin is not None:
                p += r.nets[port.net].v * r.port_i[port.path] if r.nets[port.net].v else 0.0
        for port in f.outs:
            if port.net in nets:
                p -= fr.pout
    return p


def scenario_text(a: Analysis, r: Result, summary: bool = False) -> str:
    """Full report, or with summary=True only the Board, Group and Power tables."""
    d = a.design
    out = [f"=== {d.name} — scenario '{r.scenario}' (loads {r.loads}) "
           f"— {'converged' if r.converged else 'NOT converged'} in {r.iterations} iterations"]
    out += libraries_text(a)
    out.append("")

    def power_in(chips) -> float:
        return sum(r.funcs[f.path].pin for c in chips for f in c.functions.values() if f.kind != "provider")

    if not summary:
        rows = []
        for n in d.nets.values():
            nr = r.nets[n.name]
            drivers = ", ".join(p.func.path for p in n.drivers)
            rng = f"{fmt(nr.lo, VOLT)} .. {fmt(nr.hi, VOLT)}" if nr.powered else ""
            power = fmt(nr.v * nr.i, WATT) if nr.powered else ""
            rows.append([n.name if not n.anonymous else f"{n.name} (direct)", _v(nr.v), rng,
                         fmt(nr.i, AMP), power, drivers])
        out += _table(["Net", "V nom", "V range", "Load", "Power", "Source"], rows, {1, 3, 4}) + [""]

        rows = []
        for path, fr in r.funcs.items():
            f = fr.func
            kind = f.kind + (f"/{f.subkind}" if f.subkind else "")
            state = "off" if not fr.on else ("unpowered" if not fr.powered else "")
            rows.append([path, kind,
                         fmt(fr.vin, VOLT) if fr.vin is not None else "",
                         fmt(fr.vout, VOLT) if fr.vout is not None else "",
                         fmt(fr.iin, AMP) if f.ins else "",
                         fmt(fr.iout, AMP) if f.outs else "",
                         pct(fr.eff) if fr.eff is not None else "",
                         fmt(fr.heat, WATT),
                         pct(fr.loading) if fr.loading is not None else "",
                         state])
        out += _table(["Function", "Kind", "Vin", "Vout", "Iin", "Iout", "Eff", "Heat", "Load%", "State"],
                      rows, {2, 3, 4, 5, 6, 7, 8}) + [""]

        rows = []
        for ref, chip in sorted(d.chips.items(), key=lambda kv: -r.chip_heat[kv[0]]):
            sinks = [f for f in chip.functions.values() if f.kind != "provider"]
            rows.append([ref, chip.part or "", chip.desc or "", fmt(r.chip_heat[ref], WATT),
                         fmt(power_in([chip]), WATT) if sinks else "—"])
        out += _table(["Chip", "Part", "Desc", "Heat", "Power in"], rows, {3, 4}) + [""]

    if d.boards:
        rows = []
        for b in d.boards.values():
            heat = sum(h for ref, h in r.chip_heat.items() if ref.startswith(b.prefix))
            rows.append([b.path, b.design, r.board_scenarios.get(b.path, "(default)"),
                         fmt(heat, WATT), fmt(_board_power(r, d, b), WATT)])
        out += _table(["Board", "Design", "Scenario", "Heat", "Power in"], rows, {3, 4}) + [""]

    groups: dict[str, list] = {}
    for ref in d.chips:
        k = group_key(ref)
        if k != ref:
            groups.setdefault(k, []).append(ref)
    board_groups: dict[str, list] = {}
    for bp in d.boards:
        k = group_key(bp)
        if k != bp:
            board_groups.setdefault(k, []).append(bp)
    if groups or board_groups:
        rows = []
        for k, refs in board_groups.items():
            heat = sum(h for ref, h in r.chip_heat.items() if any(ref.startswith(b + ".") for b in refs))
            pin = sum(_board_power(r, d, d.boards[b]) for b in refs)
            rows.append([k, "board", str(len(refs)), fmt(heat, WATT), fmt(pin, WATT)])
        for k, refs in groups.items():
            chips = [d.chips[x] for x in refs]
            rows.append([k, "chip", str(len(refs)), fmt(sum(r.chip_heat[x] for x in refs), WATT),
                         fmt(power_in(chips), WATT)])
        out += _table(["Group", "Type", "Count", "Heat", "Power in"], rows, {2, 3, 4}) + [""]

    src = r.source_power

    def share(x):
        return pct(x / src) if src > 0 else ""

    names = {"converter": "Converter loss", "switch": "Switch loss", "series": "Series loss",
             "oring": "OR-ing loss"}
    rows = [["Source output", fmt(src, WATT), share(src)]]
    for kind, label in names.items():
        if kind in r.losses and (kind in ("converter", "switch") or r.losses[kind] > 0):
            rows.append([f"  {label}", fmt(r.losses[kind], WATT), share(r.losses[kind])])
    onboard = r.load_power - r.offboard_power
    rows.append(["  On-board consumers", fmt(onboard, WATT), share(onboard)])
    rows.append(["  External consumers", fmt(r.offboard_power, WATT), share(r.offboard_power)])
    internal = r.losses.get("provider", 0.0)
    if internal > 0:
        rows.append(["Source internal loss", fmt(internal, WATT), ""])
    rows.append(["Heat on board", fmt(r.board_heat, WATT), share(r.board_heat)])
    rows.append(["System efficiency", pct(r.system_efficiency) if r.system_efficiency is not None else "—", ""])
    out += _table(["Power", "Value", "% of source"], rows, {1, 2}) + [""]
    return "\n".join(out)


def findings_text(diags: Diagnostics) -> str:
    items = diags.sorted()
    if not items:
        return "No findings."
    rows = []
    for f in items:
        sev = "waived" if f.waived else f.severity
        msg = f"{f.message} [{f.code}]"
        if f.waived:
            msg += f" (was {f.severity}; {f.waived})"
        rows.append([sev, f.scenario or "-", _loc(f.span), msg])
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r[:3], widths)) + "  " + r[3] for r in rows]
    n_err = sum(1 for f in items if f.severity == "error" and not f.waived)
    n_warn = sum(1 for f in items if f.severity == "warning" and not f.waived)
    n_info = sum(1 for f in items if f.severity == "info" and not f.waived)
    n_waived = sum(1 for f in items if f.waived)
    lines.append(f"\n{n_err} error(s), {n_warn} warning(s), {n_info} info, {n_waived} waived")
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
            "board_scenarios": r.board_scenarios,
            "losses": r.losses,
            "totals": {"source_power": r.source_power, "load_power": r.load_power,
                       "offboard_power": r.offboard_power, "board_heat": r.board_heat,
                       "system_efficiency": r.system_efficiency},
        }
    libs = [{"name": l.name, "path": l.path, "origin": l.origin, "git": l.git, "sha": l.sha}
            for l in (a.design.libraries if a.design else [])]
    boards = {b.path: {"design": b.design, "file": b.file, "ports": b.ports}
              for b in (a.design.boards.values() if a.design else [])}
    return json.dumps({"design": a.design.name if a.design else None, "libraries": libs, "boards": boards,
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
    def chip_cluster(ref, chip, pad):
        short = ref[len(chip.board) + 1:] if chip.board else ref
        title = f"{short}" + (f"  {chip.part}" if chip.part else "") + (f"\n{chip.desc}" if chip.desc else "")
        title += f"\nheat {fmt(r.chip_heat[ref], WATT)}"
        L.append(f"{pad}subgraph {_q('cluster_' + ref)} {{")
        L.append(f"{pad}  label={_q(title)}; style=rounded; color=gray60; fontsize=10;")
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
            L.append(f"{pad}  {_q(f.path)} [label={_q(chr(10).join(lines))}, fillcolor={_q(fill)}, "
                     f"color={color}, penwidth={pen}];")
        L.append(f"{pad}}}")

    def net_owner(name: str) -> str:
        bare = name.lstrip("~")
        owners = [bp for bp in d.boards if bare.startswith(bp + ".")]
        return max(owners, key=len) if owners else ""

    def net_node(n, pad):
        nr = r.nets[n.name]
        owner = net_owner(n.name)
        short = n.name.lstrip("~")[len(owner) + 1:] if owner else n.name
        label = ("(direct)" if n.anonymous else short) + f"\n{_v(nr.v)}\n{fmt(nr.i, AMP)}"
        color = "red" if n.name in bad else "gray30"
        fill = "#ffffff" if nr.powered else "#f3f3f3"
        L.append(f"{pad}{_q('net:' + n.name)} [shape=ellipse, style=filled, fillcolor={_q(fill)}, "
                 f"color={color}, label={_q(label)}];")

    def board_cluster(path, pad):
        if path:
            b = d.boards[path]
            heat = sum(h for ref, h in r.chip_heat.items() if ref.startswith(b.prefix))
            scen = r.board_scenarios.get(path)
            title = f"{path}  ({b.design})" + (f"  scenario {scen}" if scen else "") + f"\nheat {fmt(heat, WATT)}"
            L.append(f"{pad}subgraph {_q('cluster_board_' + path)} {{")
            L.append(f"{pad}  label={_q(title)}; style=\"rounded,dashed\"; color=steelblue; fontsize=11;")
            inner = pad + "  "
        else:
            inner = pad
        for bp, b in d.boards.items():
            if bp.rpartition(".")[0] == path and bp != path:
                board_cluster(bp, inner)
        for ref, chip in d.chips.items():
            if chip.board == path:
                chip_cluster(ref, chip, inner)
        for n in d.nets.values():
            if net_owner(n.name) == path:
                net_node(n, inner)
        if path:
            L.append(f"{pad}}}")

    board_cluster("", "  ")
    for n in d.nets.values():
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
