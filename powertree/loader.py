"""Pipeline layers 1-3: syntax, structure, model building (with part merge,
counts and board hierarchy flattening) and reference resolution."""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import paths, schema
from .diagnostics import Diagnostics
from .kdl import KdlError, Node, Span, parse
from .libs import library_roots, stamp
from .model import (Board, Chip, Design, EffTable, Efficiency, Function, Library, Load, Net, Port,
                    ScenarioDef, SetOp, Waiver)
from .units import VOLT, VSpec, fmt

# properties a scenario `set` may change, per target kind
SETTABLE: dict[str, dict[str, object]] = {
    "*": {"on": schema.BOOL},
    "consumer": {"i": schema.A, "p": schema.W, "r": schema.OHM},
    "provider": {"v": schema.VSPEC, "imax": schema.A},
    "converter": {"v": schema.VSPEC},
    "board": {"scenario": schema.NAME, "on": schema.BOOL},
}
_BAD_REF_CHARS = re.compile(r"[.:{}]")


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
# Layers 1 + 2: files, `use`, library roots, structure validation
# ---------------------------------------------------------------------------
@dataclass
class _Def:
    nodes: list[Node]
    file: str
    ns: str
    span: Span


class _Files:
    def __init__(self, diags: Diagnostics, roots: dict[str, Library]):
        self.diags = diags
        self.roots = roots
        self.seen: dict[str, str] = {}              # realpath -> namespace
        self.parts: dict[tuple[str, str], _Def] = {}
        self.designs: dict[tuple[str, str], _Def] = {}

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

    def _target(self, spec: str, base: str, ns: str, span: Span) -> tuple[str, str] | None:
        m = re.match(r"^([A-Za-z_][\w-]+):(?![\\/])(.+)$", spec)
        if m:
            name, rel = m.groups()
            lib = self.roots.get(name)
            if lib is None:
                known = ", ".join(self.roots) or "none configured"
                self.diags.error("library", f"unknown library '{name}'{_suggest(name, self.roots)} "
                                            f"(known: {known})", span)
                return None
            return os.path.normpath(os.path.join(lib.path, rel)), name
        return os.path.normpath(os.path.join(base, spec)), ns

    def collect(self, nodes: list[Node], path: str, ns: str, main: bool) -> None:
        self.seen[os.path.realpath(path)] = ns
        if ns and ns in self.roots:
            self.roots[ns].files.append(path)
        base = os.path.dirname(path)
        design = next((n for n in nodes if n.name == "design" and n.args), None)
        if design is not None and not main:
            key = (ns, str(design.args[0].value))
            if key in self.designs:
                self.diags.error("duplicate-design", f"design '{key[1]}' is defined more than once "
                                                     f"(also at {self.designs[key].span})", design.span)
            else:
                self.designs[key] = _Def(nodes, path, ns, design.span)
        for n in nodes:
            if n.name == "part" and n.args:
                key = (ns, str(n.args[0].value))
                if key in self.parts:
                    self.diags.error("duplicate-part", f"part '{key[1]}' is defined more than once "
                                                       f"(also at {self.parts[key].span})", n.span)
                else:
                    self.parts[key] = _Def([n], path, ns, n.span)
            elif n.name == "use" and n.args and isinstance(n.args[0].value, str):
                t = self._target(n.args[0].value, base, ns, n.args[0].span)
                if t is None:
                    continue
                target, tns = t
                if os.path.realpath(target) in self.seen:
                    continue
                sub = self.read(target, n.args[0].span)
                if sub is not None:
                    self.collect(sub, target, tns, main=False)
            elif not main and design is None and n.name not in ("part", "use"):
                self.diags.warning("ignored-in-library",
                                   f"'{n.name}' in a used file without a design node is ignored",
                                   n.span)

    def lookup(self, table: dict, name: str, what: str, span: Span, required: bool) -> _Def | None:
        if ":" in name:
            ns, bare = name.split(":", 1)
            d = table.get((ns, bare))
            if d is None and required:
                self.diags.error(f"unknown-{what}", f"no {what} '{bare}' in library '{ns}'", span)
            return d
        hits = [(k, v) for k, v in table.items() if k[1] == name]
        if len(hits) > 1:
            where = ", ".join(f"{k[0] + ':' if k[0] else ''}{name} ({v.file})" for k, v in hits)
            self.diags.error(f"ambiguous-{what}", f"{what} '{name}' is defined in several places: {where}; "
                                                   f"qualify it as <library>:{name}", span)
            return None
        if hits:
            return hits[0][1]
        names = {(f"{k[0]}:{k[1]}" if k[0] else k[1]) for k in table}
        # part numbers are similar by nature: only flag near-identical names when not required
        m = difflib.get_close_matches(name, list(names), n=1, cutoff=0.6 if required else 0.9)
        sugg = f" (did you mean '{m[0]}'?)" if m else ""
        if required:
            self.diags.error(f"unknown-{what}", f"no {what} '{name}'{sugg}", span)
        elif sugg:
            self.diags.warning(f"unknown-{what}", f"{what} '{name}' is not in any loaded file{sugg}; "
                                                   f"treating it as a label", span)
        return None


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


def _chip_functions(chip: Node, part: Node | None, diags: Diagnostics) -> list[Node]:
    part_name = chip.props.get("part")
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
    p.net = p.net_local = _conv(pn, "net", schema.NAME)
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
        p.vlim = _conv(pn, "vlim", schema.VLIM)
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
    # A function with a single input or output gets it implicitly if it is not written, so
    # `series { r "10mΩ" }` has ports RSENSE.series.in/out. An implicit port is unconnected until
    # something links to it (from=), and the topology checks report it if nothing does.
    if not f.ins and lo == hi == 1:
        f.ins.append(Port("in", f, 0, span=fn.span))
    if not f.outs and nout == 1:
        f.outs.append(Port("out", f, 0, span=fn.span))

    def count_msg(direction: str, lo: int, hi: int) -> str:
        if hi == 0:
            return f"{f.kind} {f.path} cannot have '{direction}' ports"
        if lo == hi:
            return f"{f.kind} {f.path} must have exactly one '{direction}' port"
        return f"{f.kind} {f.path} needs at least one '{direction}' port"

    if not lo <= len(f.ins) <= hi:
        diags.error("port-count", count_msg("in", lo, hi), fn.span, f.path)
    if len(f.outs) != nout:
        diags.error("port-count", count_msg("out", nout, nout), fn.span, f.path)

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



# ---------------------------------------------------------------------------
# Layer 3a: build the flattened model
# ---------------------------------------------------------------------------
def _tmpl(s: str | None, n: int | None, span: Span, diags: Diagnostics, what: str) -> str | None:
    if s is None or "{n}" not in s:
        return s
    if n is None:
        diags.error("template", f"{what} '{s}' uses {{n}} but its node has no count=", span)
        return s.replace("{n}", "?")
    return s.replace("{n}", str(n))


def _counts(node: Node, diags: Diagnostics) -> list[int | None]:
    c = node.props.get("count")
    if c is None:
        return [None]
    if c.value < 1:
        diags.error("bad-value", "count= must be at least 1", node.prop_spans["count"])
        return []
    return list(range(1, c.value + 1))


@dataclass
class _Ctx:
    prefix: str = ""                       # "" or "IO:2."
    board: Board | None = None
    nets: dict[str, str] = field(default_factory=dict)   # local net name -> flattened name
    aliases: dict[str, str] = field(default_factory=dict)  # port internal net -> parent flat net
    stack: tuple = ()


class _Builder:
    def __init__(self, files: _Files, diags: Diagnostics, path: str):
        self.files = files
        self.diags = diags
        self.d = Design("design", path)

    def top(self, nodes: list[Node]) -> Design:
        designs = [n for n in nodes if n.name == "design"]
        if len(designs) != 1:
            self.diags.error("design", "a design file needs exactly one 'design <name>' node",
                             designs[1].span if len(designs) > 1 else None)
        if designs and designs[0].args:
            self.d.name = str(designs[0].args[0].value)
        self.body(nodes, _Ctx(stack=(("", self.d.name),)))
        return self.d

    # -- nets and ports ---------------------------------------------------
    def _net_ref(self, local: str | None, ctx: _Ctx, span: Span) -> str | None:
        if local is None:
            return None
        flat = ctx.nets.get(local)
        if flat is None:
            self.diags.error("undefined-net", f"net '{local}' is not declared{_suggest(local, ctx.nets)}",
                             span)
        return flat

    def body(self, nodes: list[Node], ctx: _Ctx) -> None:
        d, diags = self.d, self.diags
        standalone = [c for n in nodes if n.name == "standalone" for c in n.children]
        nodes = [n for n in nodes if n.name != "standalone"]
        if ctx.board is None and standalone:
            nodes = nodes + standalone
            added = [n for n in standalone if n.name in ("chip", "board")]
            if added:
                names = ", ".join(str(n.args[0].value) for n in added if n.args)
                diags.info("standalone", f"standalone block adds {names} (only used when this file is "
                                         f"analysed on its own)", added[0].span)
        ports = [n for n in nodes if n.name == "port"]
        for n in nodes:
            if n.name != "net":
                continue
            base = str(n.args[0].value)
            counts = _counts(n, diags)
            if n.props.get("count") is not None and "{n}" not in base:
                diags.error("template", f"net '{base}' has count= but no {{n}} in its name", n.span)
                continue
            for k in counts:
                local = _tmpl(base, k, n.span, diags, "net name")
                if local in ctx.nets:
                    diags.error("duplicate-net", f"net '{local}' declared twice", n.span)
                    continue
                if local in ctx.aliases:
                    ctx.nets[local] = ctx.aliases[local]
                    continue
                flat = ctx.prefix + local
                ctx.nets[local] = flat
                d.nets[flat] = Net(flat, n.span, _conv(n, "desc", schema.STR))
        for pn in ports:
            pname, internal = str(pn.args[0].value), _conv(pn, "net", schema.NAME)
            if internal not in ctx.nets:
                diags.error("undefined-net", f"port {pname}: net '{internal}' is not declared"
                                             f"{_suggest(internal, ctx.nets)}", pn.span)
            elif ctx.board is None:
                d.nets[ctx.nets[internal]].port = pname
            if ctx.board is not None:
                ctx.board.port_dirs[pname] = _conv(pn, "dir", schema.STR, "bidir")

        for n in nodes:
            if n.name == "chip":
                self.chip(n, ctx)
            elif n.name == "board":
                self.board(n, ctx)
            elif n.name == "scenario":
                self.scenario(n, ctx)
            elif n.name == "rules":
                if ctx.board is not None:
                    diags.info("ignored-rules", "rules in an instantiated design are ignored; the top-level "
                                                "design's rules apply", n.span)
                    continue
                for r in n.children:
                    d.rules[r.name] = schema.convert(next(iter(r.props.values())), schema.RATIO)
            elif n.name == "waive":
                d.waivers.append(Waiver(str(n.args[0].value), ctx.prefix + str(n.args[1].value),
                                        _conv(n, "reason", schema.STR), n.span))

    # -- chips --------------------------------------------------------------
    def chip(self, n: Node, ctx: _Ctx) -> None:
        d, diags = self.d, self.diags
        base = str(n.args[0].value)
        if _BAD_REF_CHARS.search(base):
            diags.error("bad-name", f"chip reference '{base}' may not contain . : {{ }}", n.span)
            return
        part_name = _conv(n, "part", schema.NAME)
        part = None
        if part_name is not None:
            pdef = self.files.lookup(self.files.parts, part_name, "part", n.prop_spans["part"], required=False)
            part = pdef.nodes[0] if pdef else None
        fnodes = _chip_functions(n, part, diags)
        for k in _counts(n, diags):
            ref = ctx.prefix + (f"{base}:{k}" if k is not None else base)
            if ref in d.chips:
                diags.error("duplicate-chip", f"chip '{ref}' defined twice (also at {d.chips[ref].span})",
                            n.span)
                continue
            chip = Chip(ref, n.span, part_name, _tmpl(_conv(n, "desc", schema.STR), k, n.span, diags, "desc"),
                        board=ctx.board.path if ctx.board else "")
            if not fnodes:
                diags.warning("empty-chip", f"chip {ref} has no power functions", n.span, ref)
            for fn in fnodes:
                f = _build_function(fn, chip, diags)
                if f.name in chip.functions:
                    diags.error("duplicate-function",
                                f"chip {ref} has two functions named '{f.name}'; name them, "
                                f"e.g. '{fn.name} {fn.name}2'", fn.span, ref)
                    continue
                for p in f.ins + f.outs:
                    p.net_local = _tmpl(p.net_local, k, p.span, diags, "net")
                    p.net = self._net_ref(p.net_local, ctx, p.span)
                    fr = _tmpl(p.from_ref, k, p.span, diags, "from")
                    p.from_ref = ctx.prefix + fr if fr else None
                chip.functions[f.name] = f
            d.chips[ref] = chip

    # -- boards ---------------------------------------------------------------
    def board(self, n: Node, ctx: _Ctx) -> None:
        d, diags = self.d, self.diags
        base = str(n.args[0].value)
        if _BAD_REF_CHARS.search(base):
            diags.error("bad-name", f"board reference '{base}' may not contain . : {{ }}", n.span)
            return
        dname = _conv(n, "design", schema.NAME)
        ddef = self.files.lookup(self.files.designs, dname, "design", n.prop_spans["design"], required=True)
        if ddef is None:
            return
        key = (ddef.ns, dname.split(":")[-1])
        if key in ctx.stack or (key[1] == ctx.stack[0][1] and ddef.file == d.file):
            chain = " -> ".join(k[1] for k in ctx.stack) + f" -> {key[1]}"
            diags.error("board-cycle", f"design instantiates itself: {chain}", n.span)
            return
        child_ports = {str(p.args[0].value): p for p in ddef.nodes if p.name == "port" and p.args}
        for k in _counts(n, diags):
            path = ctx.prefix + (f"{base}:{k}" if k is not None else base)
            if path in d.boards or path in d.chips:
                diags.error("duplicate-board", f"'{path}' is defined twice", n.span)
                continue
            b = Board(path, key[1], ddef.file, n.span,
                      _tmpl(_conv(n, "desc", schema.STR), k, n.span, diags, "desc"))
            aliases: dict[str, str] = {}
            for bp in n.children_named("port"):
                pname = str(bp.args[0].value)
                if pname not in child_ports:
                    diags.error("unknown-port", f"design '{key[1]}' has no port '{pname}'"
                                                f"{_suggest(pname, child_ports)}; ports: "
                                                f"{', '.join(child_ports) or 'none'}", bp.span)
                    continue
                if pname in b.ports:
                    diags.error("duplicate-port", f"port '{pname}' bound twice", bp.span)
                    continue
                local = _tmpl(_conv(bp, "net", schema.NAME), k, bp.span, diags, "net")
                flat = self._net_ref(local, ctx, bp.span)
                if flat is None:
                    continue
                b.ports[pname] = flat
                aliases[str(child_ports[pname].props["net"].value)] = flat
            for pname in child_ports:
                if pname not in b.ports and not any(str(bp.args[0].value) == pname
                                                    for bp in n.children_named("port")):
                    diags.error("unbound-port", f"board {path}: port '{pname}' of design '{key[1]}' "
                                                f"is not connected", n.span)
            d.boards[path] = b
            self.body(ddef.nodes, _Ctx(path + ".", b, {}, aliases, ctx.stack + (key,)))

    # -- scenarios --------------------------------------------------------------
    def scenario(self, n: Node, ctx: _Ctx) -> None:
        owner = ctx.board.path if ctx.board else ""
        group = self.d.scenario_group(owner)
        name = str(n.args[0].value)
        if name in group:
            self.diags.error("duplicate-scenario", f"scenario '{name}' defined twice", n.span)
            return
        s = ScenarioDef(name, n.span, owner, desc=_conv(n, "desc", schema.STR))
        base = _conv(n, "base", schema.STR)
        if base:
            s.bases = [(b.strip(), n.prop_spans["base"]) for b in base.split(",") if b.strip()]
        for c in n.children:
            if c.name == "loads":
                s.loads = c.args[0].value
            elif c.name == "set":
                s.raw_sets.append((ctx.prefix + str(c.args[0].value), dict(c.props), dict(c.prop_spans),
                                   c.span))
        group[name] = s


# ---------------------------------------------------------------------------
# Layer 3b: resolve references
# ---------------------------------------------------------------------------
def find_function(d: Design, path: str) -> tuple[Function | None, str]:
    """Resolve CHIP or CHIP.function (CHIP may be hierarchical, e.g. IO:2.U3)."""
    if path in d.chips:
        chip, fname = d.chips[path], None
    else:
        head, _, fname = path.rpartition(".")
        chip = d.chips.get(head)
        if chip is None:
            last = path.split(".")[-1]
            scope = [c.rsplit(".", 1)[-1] for c in d.chips]
            return None, f"no chip '{path}'{_suggest(last, scope)}"
    if fname is None:
        if len(chip.functions) == 1:
            return next(iter(chip.functions.values())), ""
        return None, f"chip '{chip.ref}' has several functions ({', '.join(chip.functions)}); name one"
    f = chip.functions.get(fname)
    if f is None:
        return None, (f"chip '{chip.ref}' has no function '{fname}'{_suggest(fname, chip.functions)}"
                      f"; it has: {', '.join(chip.functions)}")
    return f, ""


def _resolve_out_port(d: Design, ref: str) -> tuple[Port | None, str]:
    base, _, last = ref.rpartition(".")
    if last in ("out", "in") and base:
        if last == "in":
            return None, f"'{ref}' is an input; from= must name an output"
        ref = base
    f, msg = find_function(d, ref)
    if f is None:
        return None, msg
    if not f.outs:
        return None, f"{f.kind} {f.path} has no output"
    return f.outs[0], ""


def _candidates(d: Design) -> dict[str, tuple[str, Any]]:
    c: dict[str, tuple[str, Any]] = {}
    for b in d.boards.values():
        c[b.path] = ("board", b)
    for ch in d.chips.values():
        c[ch.ref] = ("chip", ch)
    for f in d.functions():
        c[f.path] = ("function", f)
    return c


def _resolve_set(d: Design, s: ScenarioDef, cands, diags: Diagnostics) -> None:
    for pattern, props, spans, span in s.raw_sets:
        matches = [k for k in cands if paths.match(pattern, k)]
        if not matches:
            last = pattern.split(".")[-1]
            diags.error("bad-reference", f"set {pattern}: matches no board, chip or function"
                                         f"{_suggest(last, [k.split('.')[-1] for k in cands])}", span)
            continue
        if not props:
            diags.warning("empty-set", f"set {pattern} changes nothing", span)
        op = SetOp(span)
        reported: set[tuple[str, str]] = set()

        def err(code, key, msg, sp):
            if (code, key) not in reported:
                reported.add((code, key))
                diags.error(code, f"set {pattern}: {msg}", sp)

        for m in matches:
            kind, obj = cands[m]
            if kind == "chip" and len(obj.functions) == 1:
                kind, obj = "function", next(iter(obj.functions.values()))
            if kind == "board":
                allowed = SETTABLE["board"]
            elif kind == "chip":
                allowed = SETTABLE["*"]
            else:
                allowed = {**SETTABLE["*"], **SETTABLE.get(obj.kind, {})}
            what = {"board": "a board", "chip": "a multi-function chip"}.get(kind, f"a {getattr(obj, 'kind', '')}")
            vals: dict[str, Any] = {}
            for k, v in props.items():
                if k not in allowed:
                    err("unknown-property", k, f"'{k}' cannot be set on {what}{_suggest(k, allowed)}; "
                                               f"settable: {', '.join(allowed)}", spans[k])
                    continue
                try:
                    vals[k] = schema.convert(v, allowed[k])
                except ValueError as e:
                    err("bad-value", k, f"'{k}' {e}", spans[k])
            if kind == "board":
                if "scenario" in vals:
                    if vals["scenario"] not in obj.scenarios:
                        err("undefined-scenario", obj.design,
                            f"design '{obj.design}' has no scenario '{vals['scenario']}'"
                            f"{_suggest(vals['scenario'], obj.scenarios)}", spans["scenario"])
                    else:
                        op.boards.append((obj.path, vals["scenario"]))
                if "on" in vals:
                    for f in d.functions():
                        if f.path.startswith(obj.prefix):
                            op.func_vals.setdefault(f.path, {})["on"] = vals["on"]
            elif kind == "chip":
                for f in obj.functions.values():
                    op.func_vals.setdefault(f.path, {}).update(vals)
            else:
                if obj.kind == "consumer" and len([k for k in vals if k in ("i", "p", "r")]) > 1:
                    err("load-model", "load", "give only one of i=, p=, r=", span)
                op.func_vals.setdefault(obj.path, {}).update(vals)
        s.ops.append(op)


def _scenario_cycles(group: dict[str, ScenarioDef], diags: Diagnostics) -> None:
    state: dict[str, int] = {}
    reported: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        state[name] = 1
        for b, _ in group[name].bases:
            if b not in group:
                continue
            if state.get(b) == 1:
                cyc = stack[stack.index(b):] + [b]
                if not reported & set(cyc):
                    reported.update(cyc)
                    diags.error("scenario-cycle", f"scenario inheritance loops: {' -> '.join(cyc)}",
                                group[b].span)
            elif b not in state:
                visit(b, stack + [b])
        state[name] = 2

    for name in group:
        if name not in state:
            visit(name, [name])


def _resolve(d: Design, diags: Diagnostics) -> None:
    ports = [p for f in d.functions() for p in f.ins + f.outs]
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
                        f"{p.path} has net={p.net_local} but from={p.from_ref} is on net {target.net}",
                        p.span, p.func.path)
            continue
        p.net = target.net

    cands = _candidates(d)
    groups = [d.scenarios] + [b.scenarios for b in d.boards.values()]
    for group in groups:
        for s in group.values():
            for b, span in s.bases:
                if b not in group:
                    diags.error("undefined-scenario",
                                f"scenario '{s.name}' base '{b}' is not defined{_suggest(b, group)}", span)
            _resolve_set(d, s, cands, diags)
        _scenario_cycles(group, diags)

    for w in d.waivers:
        if not any(paths.match_prefix(w.target, k) for k in list(cands) + list(d.nets)):
            diags.error("bad-reference", f"waive target '{w.target}' matches no board, chip, function or net",
                        w.span)


def load(path: str, libs: list[str] | None = None) -> tuple[Design | None, Diagnostics]:
    """Run layers 1-4. Returns (design or None, diagnostics)."""
    from .topology import check_topology

    diags = Diagnostics()
    roots = library_roots(path, libs, diags)
    files = _Files(diags, roots)
    nodes = files.read(path)
    if nodes is None or diags.has_errors():
        return None, diags
    files.collect(nodes, path, "", main=True)
    if diags.has_errors():
        return None, diags
    d = _Builder(files, diags, path).top(nodes)
    for lib in roots.values():
        if lib.files:
            stamp(lib)
            d.libraries.append(lib)
    if diags.has_errors():
        return None, diags
    _resolve(d, diags)
    if diags.has_errors():
        return None, diags
    check_topology(d, diags)
    if diags.has_errors():
        return None, diags
    return d, diags
