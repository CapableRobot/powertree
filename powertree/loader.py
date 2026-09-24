"""Pipeline layers 1-3: syntax, structure, model building and references."""
from __future__ import annotations

import difflib
import os
from typing import Any

from . import schema
from .diagnostics import Diagnostics
from .kdl import KdlError, Node, Span, parse
from .model import (Chip, Design, EffTable, Efficiency, Function, Load, Net, Port,
                    ScenarioDef, Waiver)
from .units import fmt, VOLT

# properties a scenario `set` may change, per function kind
SETTABLE: dict[str, dict[str, object]] = {
    "*": {"on": schema.BOOL},
    "consumer": {"i": schema.A, "p": schema.W, "r": schema.OHM},
    "provider": {"v": schema.VSPEC, "imax": schema.A},
    "converter": {"v": schema.VSPEC},
}


def _suggest(word: str, options) -> str:
    m = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" (did you mean '{m[0]}'?)" if m else ""


def _conv(node: Node, key: str, t, default=None):
    v = node.props.get(key)
    return default if v is None else schema.convert(v, t)


def _arg(node: Node, t, default=None):
    return schema.convert(node.args[0], t) if node.args else default


def _fname(fn: Node) -> str:
    return str(fn.args[0].value) if fn.args else fn.name


# ---------------------------------------------------------------------------
# Layer 1 + 2: parse files, validate structure, collect parts
# ---------------------------------------------------------------------------
class _Files:
    def __init__(self, diags: Diagnostics):
        self.diags = diags
        self.seen: set[str] = set()
        self.parts: dict[str, tuple[Node, str]] = {}

    def read(self, path: str, via: Span | None = None) -> list[Node] | None:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            self.diags.error("file-not-found", f"cannot read '{path}': {e.strerror}", via)
            return None
        try:
            nodes = parse(text, path)
        except KdlError as e:
            self.diags.error("syntax", e.message, e.span)
            return None
        schema.validate(nodes, self.diags)
        return nodes

    def collect(self, nodes: list[Node], path: str, library: bool) -> None:
        self.seen.add(os.path.realpath(path))
        base = os.path.dirname(path)
        for n in nodes:
            if n.name == "part" and n.args:
                name = str(n.args[0].value)
                if name in self.parts:
                    prev = self.parts[name][0]
                    self.diags.error("duplicate-part",
                                     f"part '{name}' is defined more than once (also at {prev.span})",
                                     n.span)
                else:
                    self.parts[name] = (n, path)
            elif n.name == "use" and n.args and isinstance(n.args[0].value, str):
                target = os.path.normpath(os.path.join(base, n.args[0].value))
                if os.path.realpath(target) in self.seen:
                    continue
                sub = self.read(target, n.args[0].span)
                if sub is not None:
                    self.collect(sub, target, library=True)
            elif library and n.name not in ("part", "use"):
                self.diags.warning("ignored-in-library",
                                   f"'{n.name}' in a used file is ignored (only part and use are read)",
                                   n.span)


# ---------------------------------------------------------------------------
# part merge
# ---------------------------------------------------------------------------
def _merge_props(lib: Node, inst: Node) -> Node:
    out = Node(lib.name, lib.type, inst.span, args=inst.args or lib.args,
               props={**lib.props, **inst.props},
               prop_spans={**lib.prop_spans, **inst.prop_spans})
    return out


def _merge_function(lib: Node, inst: Node) -> Node:
    out = _merge_props(lib, inst)
    names: list[str] = []
    for c in lib.children + inst.children:
        if c.name not in names:
            names.append(c.name)
    for cname in names:
        lc, ic = lib.children_named(cname), inst.children_named(cname)
        if cname in ("in", "out"):
            for k in range(max(len(lc), len(ic))):
                if k < len(lc) and k < len(ic):
                    out.children.append(_merge_props(lc[k], ic[k]))
                else:
                    out.children.append(lc[k] if k < len(lc) else ic[k])
        else:
            out.children.extend(ic if ic else lc)
    return out


def _chip_functions(chip: Node, parts: dict, diags: Diagnostics) -> list[Node]:
    part_name = chip.props.get("part")
    part = parts.get(part_name.value)[0] if part_name and part_name.value in parts else None
    if part is None:
        return list(chip.children)
    inst = {_fname(f): f for f in chip.children}
    out: list[Node] = []
    for lf in part.children:
        name = _fname(lf)
        if name in inst:
            f = inst.pop(name)
            if f.name != lf.name:
                diags.error("kind-mismatch",
                            f"'{name}' is a {lf.name} in part '{part_name.value}' "
                            f"(at {lf.span}) but a {f.name} here", f.span)
                continue
            out.append(_merge_function(lf, f))
        else:
            out.append(lf)
    out.extend(inst.values())
    return out


# ---------------------------------------------------------------------------
# Layer 3a: build model objects
# ---------------------------------------------------------------------------
_PORTS = {  # kind -> (min ins, max ins, outs)
    "provider": (0, 0, 1), "converter": (1, 1, 1), "switch": (1, 1, 1),
    "series": (1, 1, 1), "oring": (1, 99, 1), "consumer": (1, 1, 0),
}


def _build_port(pn: Node, direction: str, f: Function, index: int) -> Port:
    p = Port(direction, f, index, span=pn.span)
    p.net = _conv(pn, "net", schema.NAME)
    p.from_ref = _conv(pn, "from", schema.NAME)
    p.imax = _conv(pn, "imax", schema.A)
    p.share = _conv(pn, "share", schema.BOOL, False)
    p.priority = _conv(pn, "priority", schema.INT)
    v = _conv(pn, "v", schema.VSPEC)
    rng = _conv(pn, "range", schema.VRANGE)
    vmin, vmax = _conv(pn, "vmin", schema.V), _conv(pn, "vmax", schema.V)
    if direction == "out":
        if v is not None and (vmin is not None or vmax is not None):
            from .units import VSpec
            v = VSpec(v.nom, vmin if vmin is not None else v.lo, vmax if vmax is not None else v.hi)
        p.v = v
        p.adjust = rng
    else:
        p.v = v
        lo = hi = None
        if v is not None and v.lo < v.hi:
            lo, hi = v.lo, v.hi
        if rng is not None:
            lo, hi = rng
        lo = vmin if vmin is not None else lo
        hi = vmax if vmax is not None else hi
        if lo is not None or hi is not None:
            p.accept = (lo if lo is not None else float("-inf"), hi if hi is not None else float("inf"))
    return p


def _build_eff(nodes: list[Node], f: Function, diags: Diagnostics) -> Efficiency | None:
    if not nodes:
        return None
    e = Efficiency(span=nodes[0].span)
    if any(n.args or "expr" in n.props for n in nodes):
        if len(nodes) > 1:
            diags.error("efficiency", f"{f.path}: only datasheet tables may use several 'eff' nodes",
                        nodes[1].span, f.path)
            return None
        n = nodes[0]
        if n.args and "expr" in n.props:
            diags.error("efficiency", f"{f.path}: give either a constant or expr=, not both", n.span, f.path)
            return None
        if n.args:
            e.constant = _arg(n, schema.RATIO)
            if not 0 < e.constant <= 1:
                diags.error("efficiency", f"{f.path}: efficiency must be in (0, 1], got {e.constant}",
                            n.span, f.path)
        else:
            e.expr = _conv(n, "expr", schema.EXPR)
        return e
    for n in nodes:
        pts = []
        for p in n.children_named("point"):
            iout, eff = _conv(p, "iout", schema.A), _conv(p, "eff", schema.RATIO)
            if iout is None or eff is None:
                continue
            if iout <= 0 or not 0 < eff <= 1:
                diags.error("efficiency", f"{f.path}: points need iout > 0 and 0 < eff <= 1", p.span, f.path)
                continue
            pts.append((iout, eff))
        if not pts:
            diags.error("efficiency", f"{f.path}: efficiency table has no points", n.span, f.path)
            continue
        e.tables.append(EffTable(_conv(n, "vin", schema.V), _conv(n, "vout", schema.V),
                                 sorted(pts), n.span))
    return e


def _build_load(n: Node, f: Function, diags: Diagnostics) -> Load | None:
    models = [m for m in ("i", "p", "r") if m in n.props]
    if len(models) != 1:
        diags.error("load-model", f"{f.path}: load needs exactly one of i=, p= or r=", n.span, f.path)
        return None
    m = models[0]
    stray = [k for k in ("imin", "imax", "pmin", "pmax") if k in n.props and k[0] != m]
    if stray:
        diags.error("load-model", f"{f.path}: {', '.join(stray)} does not match load model '{m}='",
                    n.span, f.path)
        return None
    t = {"i": schema.A, "p": schema.W, "r": schema.OHM}[m]
    nom = _conv(n, m, t)
    lo = _conv(n, m + "min", t, nom) if m != "r" else nom
    hi = _conv(n, m + "max", t, nom) if m != "r" else nom
    if not lo <= nom <= hi:
        diags.error("load-model", f"{f.path}: load needs {m}min <= {m} <= {m}max", n.span, f.path)
    if m == "r" and nom <= 0:
        diags.error("load-model", f"{f.path}: load resistance must be > 0", n.span, f.path)
    return Load(m, nom, lo, hi, _conv(n, "offboard", schema.BOOL, False), n.span)


def _build_function(fn: Node, chip: Chip, diags: Diagnostics) -> Function:
    f = Function(fn.name, _fname(fn), chip, fn.span)
    f.desc = _conv(fn, "desc", schema.STR)
    f.on = _conv(fn, "on", schema.BOOL, True)
    f.subkind = _conv(fn, "kind", schema.STR)
    f.enable = _conv(fn, "enable", schema.NAME)
    for i, pn in enumerate(fn.children_named("in")):
        f.ins.append(_build_port(pn, "in", f, i))
    for i, pn in enumerate(fn.children_named("out")):
        f.outs.append(_build_port(pn, "out", f, i))

    lo, hi, nout = _PORTS[f.kind]
    if not lo <= len(f.ins) <= hi:
        want = "no" if hi == 0 else ("exactly one" if lo == hi else "at least one")
        diags.error("port-count", f"{f.kind} {f.path} needs {want} 'in' port(s)", fn.span, f.path)
    if len(f.outs) != nout:
        want = "no" if nout == 0 else "exactly one"
        diags.error("port-count", f"{f.kind} {f.path} needs {want} 'out' port(s)", fn.span, f.path)

    def one(name, t):
        n = fn.child(name)
        return _arg(n, t) if n is not None else None

    if f.kind == "provider":
        f.r = one("r", schema.OHM) or 0.0
    if f.kind in ("provider", "converter") and f.outs and f.outs[0].v is None:
        diags.error("missing-voltage", f"{f.kind} {f.path} output needs v=", f.outs[0].span, f.path)
    if f.kind == "converter":
        if f.subkind is None:
            diags.error("missing-property", f"converter {f.path} needs kind= (e.g. buck, linear)",
                        fn.span, f.path)
        f.iq = one("iq", schema.A) or 0.0
        f.eff = _build_eff(fn.children_named("eff"), f, diags)
        dn = fn.child("dropout")
        if dn is not None:
            f.dropout = _arg(dn, schema.V)
            f.dropout_at = _conv(dn, "at", schema.A)
        if f.is_linear:
            if f.eff is not None:
                diags.warning("ignored-efficiency",
                              f"{f.path}: eff is ignored for linear regulators (Iin = Iout + Iq)",
                              f.eff.span, f.path)
                f.eff = None
        elif f.subkind is not None:
            if f.eff is None and not any(d.code == "efficiency" and d.target == f.path for d in diags.items):
                diags.error("missing-efficiency", f"converter {f.path} (kind={f.subkind}) needs eff",
                            fn.span, f.path)
            if dn is not None:
                diags.warning("ignored-dropout", f"{f.path}: dropout only applies to kind=linear",
                              dn.span, f.path)
        if f.outs and f.outs[0].v is not None and f.outs[0].adjust is not None:
            v, (a, b) = f.outs[0].v, f.outs[0].adjust
            if not a <= v.nom <= b:
                diags.error("setpoint-range",
                            f"{f.path}: setpoint {fmt(v.nom, VOLT)} is outside the adjustable range "
                            f"{fmt(a, VOLT)}..{fmt(b, VOLT)}", f.outs[0].span, f.path)
    if f.kind == "switch":
        r = one("rdson", schema.OHM)
        if r is None:
            diags.error("missing-property", f"switch {f.path} needs rdson", fn.span, f.path)
        f.r = r or 0.0
    if f.kind == "series":
        r = one("r", schema.OHM)
        if r is None:
            diags.error("missing-property", f"series {f.path} needs r", fn.span, f.path)
        f.r = r or 0.0
    if f.kind == "oring":
        f.vf = one("vf", schema.V) or 0.0
        f.r = one("r", schema.OHM) or 0.0
    if f.kind == "consumer":
        ln = fn.child("load")
        if ln is None:
            diags.error("missing-load", f"consumer {f.path} needs a load", fn.span, f.path)
        else:
            f.load = _build_load(ln, f, diags)
    return f


def _build(nodes: list[Node], path: str, parts: dict, diags: Diagnostics) -> Design:
    designs = [n for n in nodes if n.name == "design"]
    if len(designs) != 1:
        diags.error("design", "a design file needs exactly one 'design <name>' node",
                    designs[1].span if len(designs) > 1 else None)
    d = Design(str(designs[0].args[0].value) if designs and designs[0].args else "design", path)

    for n in nodes:
        if n.name == "net":
            name = str(n.args[0].value)
            if name in d.nets:
                diags.error("duplicate-net", f"net '{name}' declared twice (also at {d.nets[name].span})",
                            n.span)
                continue
            d.nets[name] = Net(name, n.span, _conv(n, "desc", schema.STR))
        elif n.name == "chip":
            ref = str(n.args[0].value)
            if ref in d.chips:
                diags.error("duplicate-chip", f"chip '{ref}' defined twice (also at {d.chips[ref].span})",
                            n.span)
                continue
            chip = Chip(ref, n.span, _conv(n, "part", schema.NAME), _conv(n, "desc", schema.STR))
            fnodes = _chip_functions(n, parts, diags)
            if not fnodes:
                diags.warning("empty-chip", f"chip {ref} has no power functions", n.span, ref)
            for fn in fnodes:
                f = _build_function(fn, chip, diags)
                if f.name in chip.functions:
                    diags.error("duplicate-function",
                                f"chip {ref} has two functions named '{f.name}'; name them, "
                                f"e.g. '{fn.name} {fn.name}2'", fn.span, ref)
                    continue
                chip.functions[f.name] = f
            d.chips[ref] = chip
        elif n.name == "scenario":
            name = str(n.args[0].value)
            if name in d.scenarios:
                diags.error("duplicate-scenario", f"scenario '{name}' defined twice", n.span)
                continue
            s = ScenarioDef(name, n.span, desc=_conv(n, "desc", schema.STR))
            base = _conv(n, "base", schema.STR)
            if base:
                s.bases = [(b.strip(), n.prop_spans["base"]) for b in base.split(",") if b.strip()]
            for c in n.children:
                if c.name == "loads":
                    s.loads = c.args[0].value
                elif c.name == "set":
                    s.sets.append((str(c.args[0].value), dict(c.props), dict(c.prop_spans), c.span))
            d.scenarios[name] = s
        elif n.name == "rules":
            for r in n.children:
                val = next(iter(r.props.values()))
                d.rules[r.name] = schema.convert(val, schema.RATIO)
        elif n.name == "waive":
            d.waivers.append(Waiver(str(n.args[0].value), str(n.args[1].value),
                                    _conv(n, "reason", schema.STR), n.span))
    return d


# ---------------------------------------------------------------------------
# Layer 3b: resolve references
# ---------------------------------------------------------------------------
def find_function(d: Design, path: str) -> tuple[Function | None, str]:
    parts = path.split(".")
    chip = d.chips.get(parts[0])
    if chip is None:
        return None, f"no chip '{parts[0]}'{_suggest(parts[0], d.chips)}"
    if len(parts) == 1:
        if len(chip.functions) == 1:
            return next(iter(chip.functions.values())), ""
        return None, f"chip '{chip.ref}' has several functions ({', '.join(chip.functions)}); name one"
    f = chip.functions.get(parts[1])
    if f is None:
        return None, (f"chip '{chip.ref}' has no function '{parts[1]}'{_suggest(parts[1], chip.functions)}"
                      f"; it has: {', '.join(chip.functions)}")
    return f, ""


def _resolve_out_port(d: Design, ref: str) -> tuple[Port | None, str]:
    parts = ref.split(".")
    if parts[-1] in ("out", "in"):
        if parts[-1] == "in":
            return None, f"'{ref}' is an input; from= must name an output"
        parts = parts[:-1]
    f, msg = find_function(d, ".".join(parts))
    if f is None:
        return None, msg
    if not f.outs:
        return None, f"{f.kind} {f.path} has no output"
    return f.outs[0], ""


def _resolve(d: Design, diags: Diagnostics) -> None:
    ports = [p for f in d.functions() for p in f.ins + f.outs]
    for p in ports:
        if p.net is not None and p.net not in d.nets:
            diags.error("undefined-net", f"net '{p.net}' is not declared{_suggest(p.net, d.nets)}",
                        p.span, p.func.path)
    for p in ports:
        if p.from_ref is None:
            continue
        if p.direction == "out":
            diags.error("bad-reference", "from= is only valid on inputs", p.span, p.func.path)
            continue
        target, msg = _resolve_out_port(d, p.from_ref)
        if target is None:
            diags.error("bad-reference", f"from={p.from_ref}: {msg}", p.span, p.func.path)
            continue
        if target.net is None:
            anon = f"~{target.path}"
            target.net = anon
            d.nets.setdefault(anon, Net(anon, target.span, anonymous=True))
        if p.net is not None and p.net != target.net:
            diags.error("conflicting-link",
                        f"{p.path} has net={p.net} but from={p.from_ref} is on net {target.net}",
                        p.span, p.func.path)
            continue
        p.net = target.net

    for s in d.scenarios.values():
        for b, span in s.bases:
            if b not in d.scenarios:
                diags.error("undefined-scenario",
                            f"scenario '{s.name}' base '{b}' is not defined{_suggest(b, d.scenarios)}", span)
        resolved = []
        for path, props, spans, span in s.sets:
            f, msg = find_function(d, path)
            if f is None:
                diags.error("bad-reference", f"set {path}: {msg}", span)
                continue
            allowed = {**SETTABLE["*"], **SETTABLE.get(f.kind, {})}
            vals: dict[str, Any] = {}
            for k, v in props.items():
                if k not in allowed:
                    diags.error("unknown-property",
                                f"set {path}: '{k}' cannot be set on a {f.kind}{_suggest(k, allowed)}; "
                                f"settable: {', '.join(allowed)}", spans[k])
                    continue
                try:
                    vals[k] = schema.convert(v, allowed[k])
                except ValueError as e:
                    diags.error("bad-value", f"set {path}: '{k}' {e}", spans[k])
            if f.kind == "consumer" and len([k for k in vals if k in ("i", "p", "r")]) > 1:
                diags.error("load-model", f"set {path}: give only one of i=, p=, r=", span)
            if not props:
                diags.warning("empty-set", f"set {path} changes nothing", span)
            resolved.append((f.path, vals, spans, span))
        s.sets = resolved

    # scenario inheritance loops
    state: dict[str, int] = {}
    reported: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        state[name] = 1
        for b, _ in d.scenarios[name].bases:
            if b not in d.scenarios:
                continue
            if state.get(b) == 1:
                cyc = stack[stack.index(b):] + [b]
                if not reported & set(cyc):
                    reported.update(cyc)
                    diags.error("scenario-cycle", f"scenario inheritance loops: {' -> '.join(cyc)}",
                                d.scenarios[b].span)
            elif b not in state:
                visit(b, stack + [b])
        state[name] = 2

    for name in d.scenarios:
        if name not in state:
            visit(name, [name])

    for w in d.waivers:
        f, _ = find_function(d, w.target)
        if f is None and w.target not in d.chips and w.target not in d.nets:
            diags.error("bad-reference", f"waive target '{w.target}' is not a chip, function or net", w.span)


def load(path: str) -> tuple[Design | None, Diagnostics]:
    """Run layers 1-3 (and 4 via topology). Returns (design or None, diagnostics)."""
    from .topology import check_topology

    diags = Diagnostics()
    files = _Files(diags)
    nodes = files.read(path)
    if nodes is None:
        return None, diags
    files.collect(nodes, path, library=False)
    if diags.has_errors():
        return None, diags
    d = _build(nodes, path, files.parts, diags)
    if diags.has_errors():
        return None, diags
    _resolve(d, diags)
    if diags.has_errors():
        return None, diags
    check_topology(d, diags)
    if diags.has_errors():
        return None, diags
    return d, diags
