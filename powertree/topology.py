"""Pipeline layer 4: graph rules on the resolved design (ERC for power)."""
from __future__ import annotations

from .diagnostics import Diagnostics
from .model import Design, Function


def check_topology(d: Design, diags: Diagnostics) -> None:
    for net in d.nets.values():
        net.drivers.clear()
        net.sinks.clear()
    for f in d.functions():
        for p in f.outs:
            if p.net is None:
                diags.warning("unconnected-output", f"{p.path} is not connected", p.span, f.path)
            else:
                d.nets[p.net].drivers.append(p)
        for p in f.ins:
            if p.net is None:
                diags.error("unconnected-input", f"{p.path} is not connected (give net= or from=)",
                            p.span, f.path)
            else:
                d.nets[p.net].sinks.append(p)

    for net in d.nets.values():
        if not net.drivers and not net.sinks:
            diags.warning("unused-net", f"net '{net.name}' has nothing attached", net.span, net.name)
            continue
        if not net.drivers:
            who = ", ".join(p.path for p in net.sinks)
            diags.error("undriven-net", f"net '{net.name}' has no source (feeds {who})", net.span, net.name)
        elif not net.sinks:
            diags.warning("unloaded-net", f"net '{net.name}' drives nothing", net.span, net.name)
        if len(net.drivers) > 1:
            ok = all(p.share or p.func.kind == "oring" for p in net.drivers)
            if not ok:
                who = ", ".join(p.path for p in net.drivers)
                diags.error("multiple-drivers",
                            f"net '{net.name}' has several outputs connected: {who}. Mark them "
                            f"share=#true (current sharing) or combine them with an oring",
                            net.span or net.drivers[0].span, net.name)

    order = _topo_sort(d, diags)
    if order is not None:
        d.topo_order = order


def downstream(f: Function, d: Design) -> list[Function]:
    out = []
    for p in f.outs:
        if p.net is not None:
            out.extend(s.func for s in d.nets[p.net].sinks)
    return out


def _topo_sort(d: Design, diags: Diagnostics) -> list[Function] | None:
    funcs = list(d.functions())
    indeg = {id(f): 0 for f in funcs}
    for f in funcs:
        for g in downstream(f, d):
            indeg[id(g)] += 1
    ready = [f for f in funcs if indeg[id(f)] == 0]
    order: list[Function] = []
    while ready:
        f = ready.pop(0)
        order.append(f)
        for g in downstream(f, d):
            indeg[id(g)] -= 1
            if indeg[id(g)] == 0:
                ready.append(g)
    if len(order) == len(funcs):
        return order
    stuck = [f for f in funcs if indeg[id(f)] > 0]
    cycle = _find_cycle(stuck, d)
    diags.error("cycle", "power path forms a loop: " + " -> ".join(f.path for f in cycle),
                cycle[0].span, cycle[0].path)
    return None


def _find_cycle(funcs: list[Function], d: Design) -> list[Function]:
    ids = {id(f) for f in funcs}
    state: dict[int, int] = {}
    stack: list[Function] = []

    def dfs(f: Function):
        state[id(f)] = 1
        stack.append(f)
        for g in downstream(f, d):
            if id(g) not in ids:
                continue
            if state.get(id(g)) == 1:
                return stack[stack.index(g):] + [g]
            if id(g) not in state:
                r = dfs(g)
                if r:
                    return r
        stack.pop()
        state[id(f)] = 2
        return None

    for f in funcs:
        if id(f) not in state:
            r = dfs(f)
            if r:
                return r
    return funcs[:1]
