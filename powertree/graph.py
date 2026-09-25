"""Graphviz output with stacking of repeated instances.

Stacking rule: instances of the same count= group (same path with indices
replaced by *) are drawn as one stacked node when they are configured the same
in the scenario: same on/off state, `set` overrides, load level and board
scenario. The decision is made by colour refinement over the design graph
(functions, nets, chips, boards and their links), so stacks are coherent:
if card IO:1 differs from IO:2..4, its fuse, harness net and contents split
off with it, while identical cards stay together.

A stacked node shows per-instance values (a range if they differ by more than
1%) and the total over all instances it represents.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from . import paths
from .analysis import Analysis
from .paths import group_key
from .units import AMP, VOLT, WATT, fmt, pct

_FILL = {"provider": "#d9ead3", "converter": "#cfe2f3", "switch": "#fff2cc", "series": "#eeeeee",
         "oring": "#fce5cd", "consumer": "#ead1dc"}
_FONT = "Helvetica,Arial,sans-serif"


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Group:
    kind: str                 # f | n | c | b
    members: list             # concrete paths / names, in design order
    id: str

    @property
    def rep(self) -> str:
        return self.members[0]

    @property
    def n(self) -> int:
        return len(self.members)


def _parent_board(path: str) -> str:
    return path.rpartition(".")[0]


# ---------------------------------------------------------------------------
# partitioning
# ---------------------------------------------------------------------------
class Stacks:
    def __init__(self, a: Analysis, scenario: str, stack: bool = True, expand=()):
        d, r = a.design, a.results[scenario]
        eff = r.effective
        self.d, self.r = d, r
        expand = list(expand or ())

        def expanded(path: str) -> bool:
            return not stack or any(paths.match_prefix(sel, path) for sel in expand)

        def base(path: str) -> str:
            return path if expanded(path) else group_key(path)

        init: dict[tuple, object] = {}
        adj: dict[tuple, list] = defaultdict(list)

        def link(a_, role_a, b_, role_b):
            adj[a_].append((role_a, b_))
            adj[b_].append((role_b, a_))

        for b in d.boards.values():
            init[("b", b.path)] = ("b", base(b.path), b.design, r.board_scenarios.get(b.path))
        for b in d.boards.values():
            parent = _parent_board(b.path)
            if parent in d.boards:
                link(("b", b.path), "parent", ("b", parent), "child")
        for n in d.nets.values():
            if not stack or expanded(n.name.lstrip("~")):
                key = n.name
            elif n.anonymous:
                key = ("anon", base(n.name[1:]))
            elif n.template is not None:
                key = ("tmpl", base(n.prefix.rstrip(".")) if n.prefix else "", n.template)
            else:
                key = base(n.name)
            init[("n", n.name)] = ("n", key)
        for c in d.chips.values():
            init[("c", c.ref)] = ("c", base(c.ref), c.part)
            if c.board:
                link(("c", c.ref), "board", ("b", c.board), "chip")
        for f in d.functions():
            ov = eff.overrides.get(f.path, {}) if eff else {}
            cfg = (r.funcs[f.path].on, tuple(sorted((k, repr(v)) for k, v in ov.items())),
                   eff.load_level(f.path) if eff else "nom")
            k = ("f", f.path)
            init[k] = ("f", base(f.path), f.name, cfg)
            link(k, "chip", ("c", f.chip.ref), "func")
            for p in f.ins + f.outs:
                if p.net is not None:
                    link(k, p.direction, ("n", p.net), p.direction)

        def compress(values: dict) -> dict:
            ids: dict = {}
            return {k: ids.setdefault(v, len(ids)) for k, v in values.items()}

        color = compress(init)
        while True:
            sig = {k: (color[k], tuple(sorted((role, color[nb]) for role, nb in adj[k]))) for k in init}
            new = compress(sig)
            done = len(set(new.values())) == len(set(color.values()))
            color = new
            if done:
                break

        groups: dict[int, Group] = {}
        for k in init:                     # insertion order = design order
            g = groups.get(color[k])
            if g is None:
                groups[color[k]] = Group(k[0], [k[1]], f"{k[0]}:{k[1]}")
            else:
                g.members.append(k[1])
        self.of: dict[tuple, Group] = {}
        for g in groups.values():
            for m in g.members:
                self.of[(g.kind, m)] = g
        self.groups = list(groups.values())

    def by_kind(self, kind: str) -> list[Group]:
        return [g for g in self.groups if g.kind == kind]


# ---------------------------------------------------------------------------
# label helpers
# ---------------------------------------------------------------------------
def _compress_refs(names: list[str]) -> str:
    """['D:1','D:2','D:3','D:5'] -> 'D:1..3,5'"""
    idx: dict[str, list[int]] = defaultdict(list)
    plain: list[str] = []
    for nm in dict.fromkeys(names):
        m = re.match(r"^(.*):(\d+)$", nm)
        if m:
            idx[m.group(1)].append(int(m.group(2)))
        else:
            plain.append(nm)
    parts = list(plain)
    for base_, nums in idx.items():
        nums.sort()
        runs, start, prev = [], nums[0], nums[0]
        for x in nums[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            runs.append(f"{start}..{prev}" if prev > start + 1 else
                        (f"{start},{prev}" if prev == start + 1 else f"{start}"))
            if x is not None:
                start = prev = x
        parts.append(f"{base_}:{','.join(runs)}")
    return ", ".join(parts)


def _runs(nums: list[int]) -> str:
    return _compress_refs([f"x:{n}" for n in nums])[2:]


def _rng(values: list[float | None], dim) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "off"
    lo, hi = min(vals), max(vals)
    if hi - lo <= 0.01 * max(abs(lo), abs(hi)):
        return fmt(vals[0], dim)
    return f"{fmt(lo, dim)}–{fmt(hi, dim)}"


def _names(members: list[str], limit: int = 4) -> str:
    short = [m.rsplit(".", 1)[-1] if "." in m else m for m in members]
    s = ", ".join(short[:limit])
    return s + (f" +{len(short) - limit}" if len(short) > limit else "")


# ---------------------------------------------------------------------------
# DOT
# ---------------------------------------------------------------------------
def _fold_chains(edges: list[tuple[str, str]], wrap: int, level=None, solo=None):
    """Find linear chains (function -> net -> function ... with single in/out links) longer than
    `wrap` functions and fold them into rows.

    level(node) -> key: nodes must share it to be chained (same board cluster).
    solo(node) -> bool: a function whose chip also holds other functions cannot be chained,
    because the chain is drawn inside its own cluster and clusters must nest.

    Returns (edges to draw with constraint=false and ports, invisible anchor edges,
    folded chains as (level, [function, net, function, ...]))."""
    level = level or (lambda x: "")
    solo = solo or (lambda x: True)
    outs: dict[str, list[str]] = defaultdict(list)
    ins: dict[str, list[str]] = defaultdict(list)
    for a_, b_ in edges:
        outs[a_].append(b_)
        ins[b_].append(a_)

    def is_net(x: str) -> bool:
        return x.startswith("n:")

    def step(f: str):
        if len(outs[f]) != 1 or not solo(f):
            return None
        n = outs[f][0]
        if not is_net(n) or len(ins[n]) != 1 or len(outs[n]) != 1:
            return None
        g = outs[n][0]
        if len(ins[g]) != 1 or not solo(g) or not (level(f) == level(n) == level(g)):
            return None
        return n, g

    nodes = {x for e in edges for x in e if not is_net(x)}
    has_pred = {nx[1] for f in nodes if (nx := step(f))}
    loose: set[tuple[str, str]] = set()
    anchors: list[tuple[str, str]] = []
    chains: list[tuple[object, list[str]]] = []
    for head in sorted(nodes - has_pred):
        chain, nets, f = [head], [], head
        while (nx := step(f)) and nx[1] not in chain:
            nets.append(nx[0])
            chain.append(nx[1])
            f = nx[1]
        if wrap < 1 or len(chain) <= wrap:
            continue
        pred = ins[head][0] if ins[head] else None
        for k in range(wrap, len(chain), wrap):
            loose.add((nets[k - 1], chain[k]))          # the "carriage return" edge
            if pred is not None:
                anchors.append((pred, chain[k]))        # row starts level with the chain head
        members = [x for pair in zip(chain, nets + [None]) for x in pair if x is not None]
        chains.append((level(head), members))
    return loose, anchors, chains


def to_dot(a: Analysis, scenario: str, stack: bool = True, expand=(), collapse_boards: bool = False,
           wrap: int = 0, rankdir: str = "LR") -> str:
    d, r = a.design, a.results[scenario]
    st = Stacks(a, scenario, stack, expand)
    bad = {f.target for f in a.diags.items
           if f.scenario in (None, scenario) and f.severity == "error" and not f.waived and f.target}
    warn = {f.target for f in a.diags.items
            if f.scenario in (None, scenario) and f.severity == "warning" and not f.waived and f.target}

    def status(members: list[str]) -> tuple[str, list[str]]:
        errs = [m for m in members if any(paths.match_prefix(m, t) or t == m for t in bad)]
        if errs:
            return "red", errs
        if any(any(paths.match_prefix(m, t) or t == m for t in warn) for m in members):
            return "darkorange", []
        return "gray30", []

    title = (f"{d.name} — {scenario}  |  source {fmt(r.source_power, WATT)}, "
             f"heat {fmt(r.board_heat, WATT)}")
    L = ["digraph power {", f'  charset="UTF-8"; rankdir={rankdir}; newrank=true; compound=true; nodesep=0.25; ranksep=0.6;',
         f'  fontname="{_FONT}"; bgcolor=white;',
         f"  label={_q(title)}; labelloc=t;",
         f'  node [fontname="{_FONT}", fontsize=10, shape=box, style="rounded,filled"];',
         f'  edge [fontname="{_FONT}", fontsize=9, color=gray40];']

    board_groups = st.by_kind("b")
    chip_groups = st.by_kind("c")
    func_groups = st.by_kind("f")
    net_groups = st.by_kind("n")

    def board_of_group(g: Group) -> set[str]:
        return set(g.members)

    def net_owner(name: str) -> str:
        bare = name.lstrip("~")
        owners = [bp for bp in d.boards if bare.startswith(bp + ".")]
        return max(owners, key=len) if owners else ""

    def top_board(path: str) -> str:
        return path.split(".")[0] if path else ""

    collapsed: dict[str, Group] = {}         # top-level board path -> its group
    if collapse_boards:
        for g in board_groups:
            if _parent_board(g.rep) == "":
                for m in g.members:
                    collapsed[m] = g

    def hidden_path(board_path: str) -> bool:
        return bool(board_path) and top_board(board_path) in collapsed

    def chip_heat(refs) -> float:
        return sum(r.chip_heat[x] for x in refs)

    # -- functions and chips ---------------------------------------------------
    def func_node(g: Group, pad: str) -> None:
        f = next(ff for ff in d.chips[_chip_of(g.rep)].functions.values() if ff.path == g.rep)
        frs = [r.funcs[m] for m in g.members]
        kind = f.kind + (f"/{f.subkind}" if f.subkind else "")
        lines = [kind if f.name == f.kind else f"{f.name}  ({kind})"]
        each = "  each" if g.n > 1 else ""
        if f.kind == "consumer":
            lines.append(f"{_rng([x.vin for x in frs], VOLT)}  {_rng([x.iin for x in frs], AMP)}  "
                         f"{_rng([x.pin for x in frs], WATT)}{each}")
        elif f.kind == "provider":
            lines.append(f"{_rng([x.vout for x in frs], VOLT)}  {_rng([x.iout for x in frs], AMP)}{each}")
        else:
            lines.append(f"{_rng([x.vin for x in frs], VOLT)} → {_rng([x.vout for x in frs], VOLT)}")
            extra = _rng([x.iout for x in frs], AMP)
            effs = [x.eff for x in frs if x.eff is not None]
            if effs:
                extra += f"  η {pct(min(effs))}" if max(effs) - min(effs) < 0.001 else \
                    f"  η {pct(min(effs))}–{pct(max(effs))}"
            lines.append(extra + f"  heat {_rng([x.heat for x in frs], WATT)}{each}")
        if frs[0].loading is not None:
            loads = [x.loading for x in frs if x.loading is not None]
            lo, hi = min(loads), max(loads)
            lp = pct(hi) if hi - lo < 0.001 else f"{pct(lo)}–{pct(hi)}"
            lines.append(f"load {lp} of {fmt(frs[0].imax, AMP)}")
        if g.n > 1:
            if f.kind == "consumer":
                lines.append(f"total ×{g.n}: {fmt(sum(x.iin for x in frs), AMP)}  "
                             f"{fmt(sum(x.pin for x in frs), WATT)}")
            else:
                lines.append(f"total ×{g.n}: {fmt(sum(x.iout for x in frs), AMP)}  "
                             f"heat {fmt(sum(x.heat for x in frs), WATT)}")
        off = sum(1 for x in frs if not x.on)
        unp = sum(1 for x in frs if x.on and not x.powered)
        if off:
            lines.append("OFF" if off == g.n else f"{off} of {g.n} off")
        if unp:
            lines.append("unpowered" if unp == g.n else f"{unp} of {g.n} unpowered")
        color, errs = status(g.members)
        if errs and g.n > 1:
            lines.append(f"errors: {_names([_chip_of(e) for e in errs])}")
        powered = any(x.powered for x in frs)
        fill = _FILL[f.kind] if powered else "#f3f3f3"
        pen = 2.5 if color != "gray30" else 1
        shape = ', shape=box3d, style="filled"' if g.n > 1 else ""
        L.append(f"{pad}{_q(g.id)} [label={_q(chr(10).join(lines))}, fillcolor={_q(fill)}, "
                 f"color={color}, penwidth={pen}{shape}];")

    def chip_cluster(cg: Group, pad: str) -> None:
        chip = d.chips[cg.rep]
        rel = [m.rsplit(".", 1)[-1] if chip.board else m for m in cg.members]
        per_parent = len(set(rel))
        head = _compress_refs(rel) + (f"  ×{per_parent}" if per_parent > 1 else "")
        if chip.part:
            head += f"  {chip.part}"
        desc = chip.desc
        if chip.desc_template and "{n}" in chip.desc_template and cg.n > 1:
            nums = sorted({int(x.rsplit(":", 1)[1]) for x in rel if ":" in x})
            desc = chip.desc_template.replace("{n}", _runs(nums)) if nums else chip.desc
        if desc and not chip.part and len(desc.split()) == 1:
            lines = [f"{head}  {desc}"]          # single-word description beside the designator
        else:
            lines = [head] + ([desc] if desc else [])
        heat_each = _rng([r.chip_heat[m] for m in cg.members], WATT)
        lines.append(f"heat {heat_each}" + (f" each, {fmt(chip_heat(cg.members), WATT)} total"
                                              if cg.n > 1 else ""))
        style = 'style="rounded,bold"; penwidth=2; color=gray35;' if cg.n > 1 else \
            "style=rounded; color=gray60;"
        L.append(f"{pad}subgraph {_q('cluster_' + cg.id)} {{")
        L.append(f"{pad}  label={_q(chr(10).join(lines))}; {style} fontsize=10;")
        members = set(cg.members)
        for fg in func_groups:
            if _chip_of(fg.rep) in members:
                func_node(fg, pad + "  ")
        L.append(f"{pad}}}")

    def _chip_of(fpath: str) -> str:
        return fpath.rpartition(".")[0]

    # -- nets -------------------------------------------------------------------
    def net_node(g: Group, pad: str) -> None:
        net = d.nets[g.rep]
        nrs = [r.nets[m] for m in g.members]
        if net.anonymous:
            name = "(direct)"
        elif net.template is not None and g.n > 1:
            nums = sorted(d.nets[m].index for m in g.members)
            name = net.template.replace("{n}", "{" + _runs(nums) + "}")
        else:
            owner = net_owner(net.name)
            name = net.name[len(owner) + 1:] if owner else net.name
        if g.n > 1:
            name += f"  ×{g.n}"
        lines = [name, _rng([x.v for x in nrs], VOLT),
                 _rng([x.i for x in nrs], AMP) + ("  each" if g.n > 1 else "")]
        if g.n > 1:
            lines.append(f"total {fmt(sum(x.i for x in nrs), AMP)}")
        color = "red" if any(m in bad for m in g.members) else "gray30"
        fill = "#ffffff" if any(x.powered for x in nrs) else "#f3f3f3"
        per = ", peripheries=2" if g.n > 1 else ""
        L.append(f"{pad}{_q(g.id)} [shape=ellipse, style=filled, fillcolor={_q(fill)}, color={color}, "
                 f"label={_q(chr(10).join(lines))}{per}];")

    # -- boards -----------------------------------------------------------------
    def board_power(path: str) -> float:
        from .report import _board_power
        return _board_power(r, d, d.boards[path])

    def board_heat(path: str) -> float:
        return sum(h for ref, h in r.chip_heat.items() if ref.startswith(path + "."))

    def board_title(g: Group) -> list[str]:
        b = d.boards[g.rep]
        rel = [m.rsplit(".", 1)[-1] for m in g.members]
        per_parent = len(set(rel))
        head = _compress_refs(rel) + (f"  ×{per_parent}" if per_parent > 1 else "") + f"  ({b.design})"
        scen = r.board_scenarios.get(b.path)
        if scen:
            head += f"  scenario {scen}"
        heats = [board_heat(m) for m in g.members]
        pins = [board_power(m) for m in g.members]
        each = " each" if g.n > 1 else ""
        lines = [head, f"heat {_rng(heats, WATT)}{each}" +
                 (f", {fmt(sum(heats), WATT)} total" if g.n > 1 else ""),
                 f"power in {_rng(pins, WATT)}{each}" + (f", {fmt(sum(pins), WATT)} total" if g.n > 1 else "")]
        return lines

    def collapsed_node(g: Group, pad: str) -> None:
        b = d.boards[g.rep]
        lines = board_title(g) + [f"ports: {', '.join(b.ports) or 'none'}"]
        color, errs = status(g.members)
        if errs:
            lines.append(f"errors in: {_names(sorted({e.split('.')[0] for e in errs}))}")
        shape = "box3d" if g.n > 1 else "box"
        pen = 2.5 if color != "gray30" else 1.5
        L.append(f"{pad}{_q(g.id)} [shape={shape}, style=filled, fillcolor=\"#dfe9f5\", color={color}, "
                 f"penwidth={pen}, label={_q(chr(10).join(lines))}];")

    # -- edges ------------------------------------------------------------------
    agg: dict[tuple, list[float]] = defaultdict(list)
    per_board: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for n in d.nets.values():
        n_hidden = hidden_path(net_owner(n.name))
        ng = st.of[("n", n.name)]
        for p in n.drivers + n.sinks:
            f = p.func
            if p.direction == "out":
                cur = r.funcs[f.path].iout
            else:
                fr = r.funcs[f.path]
                cur = r.port_i.get(p.path, 0.0) if f.kind == "oring" else fr.iin
            if hidden_path(f.chip.board):
                if n_hidden:
                    continue
                tb = top_board(f.chip.board)
                key = (ng.id, collapsed[tb].id, p.direction)
                per_board[key][tb] += cur
                continue
            if n_hidden:
                continue
            fg = st.of[("f", f.path)]
            agg[(ng.id, fg.id, p.direction)].append(cur)
    for key, per in per_board.items():
        agg[key].extend(per.values())
    drawn = [((fid, nid) if direction == "out" else (nid, fid)) for (nid, fid, direction) in agg]

    chip_group_of_func = {g.id: st.of[("c", _chip_of(g.rep))] for g in func_groups}
    funcs_per_chip = defaultdict(int)
    for g in func_groups:
        funcs_per_chip[chip_group_of_func[g.id].id] += 1

    def node_level(x: str):
        if x.startswith("n:"):
            owner = net_owner(x[2:])
        elif x.startswith("f:"):
            owner = d.chips[_chip_of(x[2:])].board
        else:
            return ("top",)
        return st.of[("b", owner)].id if owner else ("top",)

    def node_solo(x: str) -> bool:
        return not x.startswith("f:") or funcs_per_chip[chip_group_of_func[x].id] == 1

    loose, anchors, chains = _fold_chains(drawn, wrap, node_level, node_solo)
    in_chain = {m for _, members in chains for m in members}

    def chip_in_chain(cg: Group) -> bool:
        return any(chip_group_of_func[g.id] is cg and g.id in in_chain for g in func_groups)

    def level_paths(lvl) -> set[str]:
        if lvl == ("top",):
            return {""}
        return set(next(g for g in board_groups if g.id == lvl).members)

    def emit_level(parent_paths: set[str], pad: str) -> None:
        """Emit boards, chips and nets whose parent board is in parent_paths ('' = top)."""
        for bg in board_groups:
            if _parent_board(bg.rep) not in parent_paths:
                continue
            if bg.rep in collapsed:
                if bg.id not in in_chain:
                    collapsed_node(bg, pad)
                continue
            lines = board_title(bg)
            color, _ = status(bg.members)
            style = ('style="rounded,dashed,bold"; penwidth=3;' if bg.n > 1 else
                     'style="rounded,dashed";')
            L.append(f"{pad}subgraph {_q('cluster_' + bg.id)} {{")
            L.append(f"{pad}  label={_q(chr(10).join(lines))}; {style} "
                     f"color={'red' if color == 'red' else 'steelblue'}; fontsize=11;")
            emit_level(set(bg.members), pad + "  ")
            L.append(f"{pad}}}")
        for cg in chip_groups:
            if d.chips[cg.rep].board in parent_paths and not chip_in_chain(cg):
                chip_cluster(cg, pad)
        for ng in net_groups:
            if net_owner(ng.rep) in parent_paths and ng.id not in in_chain:
                net_node(ng, pad)
        for k, (lvl, members) in enumerate(chains):
            if level_paths(lvl) != parent_paths:
                continue
            # a folded chain is kept together in its own invisible cluster, so its rows stay
            # adjacent and the row-to-row edges do not cross the rest of the graph
            L.append(f"{pad}subgraph {_q(f'cluster_chain_{k}')} {{")
            L.append(f"{pad}  style=invis; label=\"\";")
            for m in members:
                if m.startswith("n:"):
                    net_node(next(g for g in net_groups if g.id == m), pad + "  ")
                elif m.startswith("f:"):
                    chip_cluster(chip_group_of_func[m], pad + "  ")
                else:
                    collapsed_node(next(g for g in board_groups if g.id == m), pad + "  ")
            L.append(f"{pad}}}")

    emit_level({""}, "  ")

    drawn = [((fid, nid) if direction == "out" else (nid, fid)) for (nid, fid, direction) in agg]
    for ((nid, fid, direction), vals), (src, dst) in zip(agg.items(), drawn):
        total = sum(vals)
        label = fmt(total, AMP) if len(vals) == 1 else f"{_rng(vals, AMP)} ×{len(vals)} = {fmt(total, AMP)}"
        style = "" if total > 0 else ", style=dashed"
        if (src, dst) in loose:
            # The row-to-row edge is written reversed (next row -> net, drawn with dir=back) so
            # Graphviz treats it as a normal forward edge and routes it through the gap between
            # the rows; weight=0 stops it pulling the next row to the right. It runs from the
            # bottom of the net to the top border of the next row's chip cluster.
            extra = f"{style}, dir=back, weight=0, tailport=n, headport=s"
            if dst.startswith("f:"):
                extra += f", ltail={_q('cluster_' + chip_group_of_func[dst].id)}"
            L.append(f"  {_q(dst)} -> {_q(src)} [label={_q(label)}{extra}];")
            continue
        L.append(f"  {_q(src)} -> {_q(dst)} [label={_q(label)}{style}];")
    for src, dst in anchors:
        L.append(f"  {_q(src)} -> {_q(dst)} [style=invis];")
    for _, members in chains:
        # every row starts in the same rank as the chain's first function
        row_starts = {d_ for _, d_ in loose}
        starts = [m for m in members if m in row_starts]
        if starts:
            L.append("  { rank=same; " + " ".join(_q(x) for x in [members[0]] + starts) + " }")

    L.append("}")
    return "\n".join(L)
