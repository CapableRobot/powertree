"""Pipeline layer 5: steady-state DC solve and electrical checks for one scenario.

Method (see SPEC.md "Solver"):
  * Forward pass in topological order computes nominal net voltages and
    min/max voltage ranges from sources through converters and drops.
  * Backward pass in reverse order computes currents from loads upward.
  * Repeat until voltages and currents stop changing (needed for
    constant-power loads and resistive drops).
Currents are computed at nominal voltages; ranges use those currents.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .diagnostics import Diagnostics
from .expr import ExprError
from .model import Design, Efficiency, Function, Port
from .scenario import Effective
from .units import AMP, VOLT, Q, VSpec, fmt, pct

MAX_ITER = 200
TOL = 1e-12


@dataclass
class NetState:
    v: float
    lo: float
    hi: float


@dataclass
class FuncResult:
    func: Function
    on: bool = True
    powered: bool = False
    vin: float | None = None
    vout: float | None = None
    iin: float = 0.0
    iout: float = 0.0
    pin: float = 0.0
    pout: float = 0.0
    heat: float = 0.0
    eff: float | None = None
    loading: float | None = None
    imax: float | None = None
    active_in: Port | None = None
    offboard: bool = False


@dataclass
class NetResult:
    name: str
    anonymous: bool
    powered: bool
    v: float | None
    lo: float | None
    hi: float | None
    i: float


@dataclass
class Result:
    scenario: str
    loads: str
    funcs: dict[str, FuncResult]
    nets: dict[str, NetResult]
    chip_heat: dict[str, float]
    source_power: float
    load_power: float
    offboard_power: float
    board_heat: float
    converged: bool
    iterations: int
    diags: Diagnostics = field(default_factory=Diagnostics)
    losses: dict[str, float] = field(default_factory=dict)      # heat by function kind (non-consumer)
    port_i: dict[str, float] = field(default_factory=dict)      # input port path -> current
    board_scenarios: dict[str, str] = field(default_factory=dict)

    @property
    def system_efficiency(self) -> float | None:
        return self.load_power / self.source_power if self.source_power > 0 else None


# ---------------------------------------------------------------------------
def _eff_curve(points, iout):
    """Interpolate linearly in log(iout). Returns (eff, extrapolation or None)."""
    if iout <= points[0][0]:
        note = "below" if iout < points[0][0] * 0.999 else None
        return points[0][1], note
    if iout >= points[-1][0]:
        note = "above" if iout > points[-1][0] * 1.001 else None
        return points[-1][1], note
    for (i0, e0), (i1, e1) in zip(points, points[1:]):
        if i0 <= iout <= i1:
            t = (math.log(iout) - math.log(i0)) / (math.log(i1) - math.log(i0))
            return e0 + t * (e1 - e0), None
    return points[-1][1], None  # pragma: no cover


def efficiency(e: Efficiency, vin: float, vout: float, iout: float) -> tuple[float, list[str]]:
    notes: list[str] = []
    if e.constant is not None:
        return e.constant, notes
    if e.expr is not None:
        r = e.expr.eval(vin=Q(vin, VOLT), vout=Q(vout, VOLT), iout=Q(iout, AMP))
        return r.value, notes
    tabs = e.tables
    with_vout = sorted({t.vout for t in tabs if t.vout is not None})
    if with_vout:
        best = min(with_vout, key=lambda x: abs(x - vout))
        if abs(best - vout) > 0.1 * abs(vout):
            notes.append(f"no curve near vout={fmt(vout, VOLT)} (nearest {fmt(best, VOLT)})")
        tabs = [t for t in tabs if t.vout in (best, None)]
    by_vin = sorted([t for t in tabs if t.vin is not None], key=lambda t: t.vin)

    def curve(t):
        val, where = _eff_curve(t.points, iout)
        if where:
            lim = t.points[0][0] if where == "below" else t.points[-1][0]
            notes.append(f"iout {fmt(iout, AMP)} is {where} the curve ({where} {fmt(lim, AMP)}); clamped")
        return val

    if not by_vin:
        return curve(tabs[0]), notes
    if vin <= by_vin[0].vin or len(by_vin) == 1:
        t = by_vin[0] if vin <= by_vin[0].vin else by_vin[-1]
        if abs(vin - t.vin) > 0.1 * abs(t.vin):
            notes.append(f"vin {fmt(vin, VOLT)} is outside the curves (nearest {fmt(t.vin, VOLT)}); clamped")
        return curve(t), notes
    if vin >= by_vin[-1].vin:
        t = by_vin[-1]
        if abs(vin - t.vin) > 0.1 * abs(t.vin):
            notes.append(f"vin {fmt(vin, VOLT)} is outside the curves (nearest {fmt(t.vin, VOLT)}); clamped")
        return curve(t), notes
    for a, b in zip(by_vin, by_vin[1:]):
        if a.vin <= vin <= b.vin:
            ea, eb = curve(a), curve(b)
            k = (vin - a.vin) / (b.vin - a.vin)
            return ea + k * (eb - ea), notes
    return curve(by_vin[-1]), notes  # pragma: no cover


# ---------------------------------------------------------------------------
class _Solver:
    def __init__(self, d: Design, eff: Effective):
        self.d = d
        self.eff = eff
        self.order = d.topo_order
        self.ov = eff.overrides
        self.i_out: dict[int, float] = {}
        self.i_in: dict[int, float] = {}
        self.port_v: dict[int, NetState | None] = {}
        self.net_state: dict[str, NetState | None] = {}
        self.active_in: dict[str, Port] = {}
        self.eff_used: dict[str, float] = {}
        self.eff_notes: dict[str, list[str]] = {}
        self.errors: list[tuple[str, str, Function]] = []
        for f in d.functions():
            for p in f.outs:
                self.i_out[id(p)] = 0.0
            for p in f.ins:
                self.i_in[id(p)] = 0.0

    # effective parameters ---------------------------------------------------
    def on(self, f: Function) -> bool:
        return self.ov.get(f.path, {}).get("on", f.on)

    def vset(self, f: Function) -> VSpec:
        return self.ov.get(f.path, {}).get("v", f.outs[0].v)

    def imax(self, f: Function) -> float | None:
        if not f.outs:
            return None
        return self.ov.get(f.path, {}).get("imax", f.outs[0].imax)

    def load(self, f: Function) -> tuple[str, float, bool]:
        o = self.ov.get(f.path, {})
        for m in ("i", "p", "r"):
            if m in o:
                return m, o[m], f.load.offboard if f.load else False
        ld = f.load
        val = {"nom": ld.nom, "min": ld.min, "max": ld.max}[self.eff.load_level(f.path)]
        return ld.model, val, ld.offboard

    def vcap(self, f: Function, vin: float, i: float) -> float:
        """Highest output the converter can produce at this input (vlim=), or inf."""
        lim = f.outs[0].vlim
        if lim is None:
            return math.inf
        try:
            return lim.eval(vin=Q(vin, VOLT), iout=Q(i, AMP)).value
        except ExprError as ex:
            self.errors.append(("vlim", f"{f.path}: {ex}", f))
            return math.inf

    def dropout(self, f: Function, i: float) -> float:
        if f.dropout is None:
            return 0.0
        if f.dropout_at:
            return f.dropout * i / f.dropout_at
        return f.dropout

    # passes -----------------------------------------------------------------
    def net(self, name: str | None) -> NetState | None:
        if name is None:
            return None
        if name in self.net_state:
            return self.net_state[name]
        drivers = [self.port_v.get(id(p)) for p in self.d.nets[name].drivers]
        live = [s for s in drivers if s is not None]
        st = None
        if live:
            st = NetState(max(s.v for s in live), min(s.lo for s in live), max(s.hi for s in live))
        self.net_state[name] = st
        return st

    def forward(self) -> None:
        self.port_v.clear()
        self.net_state.clear()
        self.active_in.clear()
        for f in self.order:
            if not f.outs:
                continue
            out = f.outs[0]
            i = self.i_out[id(out)]
            st: NetState | None = None
            if self.on(f):
                if f.kind == "provider":
                    vs = self.vset(f)
                    st = NetState(vs.nom - i * f.r, vs.lo - i * f.r, vs.hi - i * f.r)
                elif f.kind == "converter":
                    src = self.net(f.ins[0].net)
                    if src is not None:
                        vs = self.vset(f)
                        vdo = self.dropout(f, i) if f.is_linear else 0.0
                        st = NetState(min(vs.nom, src.v - vdo if f.is_linear else math.inf,
                                          self.vcap(f, src.v, i)),
                                      min(vs.lo, src.lo - vdo if f.is_linear else math.inf,
                                          self.vcap(f, src.lo, i)),
                                      min(vs.hi, src.hi - vdo if f.is_linear else math.inf,
                                          self.vcap(f, src.hi, i)))
                elif f.kind in ("switch", "series"):
                    src = self.net(f.ins[0].net)
                    if src is not None:
                        dv = i * f.r
                        st = NetState(src.v - dv, src.lo - dv, src.hi - dv)
                elif f.kind == "oring":
                    cands = [(p, self.net(p.net)) for p in f.ins]
                    cands = [(p, s) for p, s in cands if s is not None]
                    if cands:
                        if any(p.priority is not None for p, _ in cands):
                            p, s = min(cands, key=lambda c: (c[0].priority if c[0].priority is not None
                                                             else math.inf, -c[1].v))
                        else:
                            p, s = max(cands, key=lambda c: c[1].v)
                        self.active_in[f.path] = p
                        dv = f.vf + i * f.r
                        st = NetState(s.v - dv, s.lo - dv, s.hi - dv)
            self.port_v[id(out)] = st

    def backward(self) -> None:
        for f in reversed(self.order):
            iout = 0.0
            if f.outs:
                out = f.outs[0]
                mine = self.port_v.get(id(out))
                if mine is not None and out.net is not None:
                    net = self.d.nets[out.net]
                    total = sum(self.i_in[id(s)] for s in net.sinks)
                    live = [p for p in net.drivers if self.port_v.get(id(p)) is not None]
                    if all(p.share for p in live):
                        active = live
                    else:
                        vmax = max(self.port_v[id(p)].v for p in live)
                        active = [p for p in live if self.port_v[id(p)].v >= vmax - 1e-9]
                    iout = total / len(active) if out in active else 0.0
                self.i_out[id(out)] = iout

            for p in f.ins:
                self.i_in[id(p)] = 0.0
            if not self.on(f) or not f.ins:
                continue
            if f.kind == "consumer":
                src = self.net(f.ins[0].net)
                if src is None:
                    continue
                model, val, _ = self.load(f)
                if model == "i":
                    ii = val
                elif src.v <= 0:
                    self.errors.append(("collapse", f"{f.path}: supply voltage collapsed to "
                                                    f"{fmt(src.v, VOLT)}", f))
                    ii = 0.0
                elif model == "p":
                    ii = val / src.v
                else:
                    ii = src.v / val
                self.i_in[id(f.ins[0])] = ii
            elif f.kind == "converter":
                src = self.net(f.ins[0].net)
                st = self.port_v.get(id(f.outs[0]))
                if src is None or st is None:
                    continue
                if f.is_linear:
                    self.i_in[id(f.ins[0])] = iout + f.iq
                elif src.v <= 0:
                    continue
                else:
                    pin = src.v * f.iq
                    if iout > 0:
                        try:
                            e, notes = efficiency(f.eff, src.v, st.v, iout)
                        except ExprError as ex:
                            self.errors.append(("efficiency", f"{f.path}: {ex}", f))
                            e, notes = 1.0, []
                        if not 0 < e <= 1:
                            self.errors.append(("efficiency", f"{f.path}: efficiency evaluates to "
                                                              f"{e:.3g} at iout={fmt(iout, AMP)}", f))
                            e = min(max(e, 1e-3), 1.0)
                        self.eff_used[f.path] = e
                        self.eff_notes[f.path] = notes
                        pin += st.v * iout / e
                    self.i_in[id(f.ins[0])] = pin / src.v
            elif f.kind in ("switch", "series"):
                if self.net(f.ins[0].net) is not None:
                    self.i_in[id(f.ins[0])] = iout
            elif f.kind == "oring":
                p = self.active_in.get(f.path)
                if p is not None:
                    self.i_in[id(p)] = iout

    def snapshot(self):
        vs = tuple((s.v, s.lo, s.hi) if s else None for s in (self.port_v.get(k) for k in sorted(self.port_v)))
        return tuple(self.i_out.values()), tuple(self.i_in.values()), vs

    def run(self) -> tuple[bool, int]:
        prev = None
        for it in range(1, MAX_ITER + 1):
            self.errors.clear()
            self.forward()
            self.backward()
            cur = self.snapshot()
            if prev is not None and _close(prev, cur):
                self.forward()   # voltages consistent with final currents
                return True, it
            prev = cur
        return False, MAX_ITER


def _close(a, b) -> bool:
    def flat(x):
        for v in x:
            if isinstance(v, tuple):
                yield from flat(v)
            else:
                yield v
    fa, fb = list(flat(a)), list(flat(b))
    if len(fa) != len(fb):
        return False
    for x, y in zip(fa, fb):
        if (x is None) != (y is None):
            return False
        if x is not None and abs(x - y) > TOL + 1e-9 * max(abs(x), abs(y)):
            return False
    return True


# ---------------------------------------------------------------------------
def solve(d: Design, eff: Effective) -> Result:
    s = _Solver(d, eff)
    converged, iters = s.run()
    diags = Diagnostics()
    sc = eff.name
    if not converged:
        diags.error("no-convergence", f"solution did not converge after {iters} iterations "
                                      f"(check constant-power loads behind large resistances)", scenario=sc)
    for code, msg, f in s.errors:
        diags.error(code, msg, f.span, f.path, sc)

    funcs: dict[str, FuncResult] = {}
    chip_heat: dict[str, float] = {c: 0.0 for c in d.chips}
    source_power = load_power = offboard = 0.0

    for f in d.functions():
        r = FuncResult(f, on=s.on(f))
        ins = [(p, s.net(p.net)) for p in f.ins]
        if f.kind == "oring":
            p = s.active_in.get(f.path)
            src = s.net(p.net) if p else None
            r.active_in = p
        else:
            src = ins[0][1] if ins else None
        r.vin = src.v if src else None
        r.iin = sum(s.i_in[id(p)] for p in f.ins)
        r.pin = (r.vin or 0.0) * r.iin
        if f.outs:
            st = s.port_v.get(id(f.outs[0]))
            r.vout = st.v if st else None
            r.iout = s.i_out[id(f.outs[0])]
            r.pout = (r.vout or 0.0) * r.iout
            r.imax = s.imax(f)
            if r.imax:
                r.loading = r.iout / r.imax
        r.powered = r.on and (r.vout is not None if f.outs else src is not None)
        if f.kind == "provider":
            vs = s.vset(f)
            r.pin = vs.nom * r.iout if r.powered else 0.0
            r.heat = r.iout ** 2 * f.r
            source_power += r.pout
        elif f.kind == "consumer":
            _, _, ob = s.load(f)
            r.offboard = ob
            r.heat = 0.0 if ob else r.pin
            load_power += r.pin
            offboard += r.pin if ob else 0.0
        else:
            r.heat = max(r.pin - r.pout, 0.0)
            if f.kind == "converter" and r.pin > 0:
                r.eff = r.pout / r.pin
        chip_heat[f.chip.ref] += r.heat
        funcs[f.path] = r

    nets = {}
    for n in d.nets.values():
        st = s.net(n.name)
        nets[n.name] = NetResult(n.name, n.anonymous, st is not None,
                                 st.v if st else None, st.lo if st else None, st.hi if st else None,
                                 sum(s.i_in[id(p)] for p in n.sinks))

    losses: dict[str, float] = {}
    for fr in funcs.values():
        if fr.func.kind != "consumer":
            losses[fr.func.kind] = losses.get(fr.func.kind, 0.0) + fr.heat
    res = Result(sc, eff.loads, funcs, nets, chip_heat, source_power, load_power, offboard,
                 sum(chip_heat.values()), converged, iters, diags, losses,
                 {p.path: s.i_in[id(p)] for f in d.functions() for p in f.ins},
                 dict(eff.board_scenarios))
    _checks(d, s, res)
    return res


def _checks(d: Design, s: _Solver, res: Result) -> None:
    diags, sc, rules = res.diags, res.scenario, d.rules
    group = {"provider": "provider-load", "converter": "converter-load", "switch": "switch-load",
             "series": "switch-load", "oring": "switch-load"}
    for f in d.functions():
        r = res.funcs[f.path]
        out = f.outs[0] if f.outs else None
        if f.kind == "consumer" and r.on and not r.powered:
            diags.info("unpowered", f"{f.path} is unpowered", f.span, f.path, sc)
        if r.loading is not None and r.powered:
            code = group[f.kind]
            limit = rules.get(code, 1.0)
            msg = (f"{f.path} output {fmt(r.iout, AMP)} is {pct(r.loading)} of imax {fmt(r.imax, AMP)}")
            if r.loading > 1.0:
                diags.error("overload", msg, out.span, f.path, sc)
            elif r.loading > limit:
                diags.warning(code, f"{msg} (rule max {pct(limit)})", out.span, f.path, sc)
        # input voltage checks
        for p in f.ins:
            if p.accept is None or not r.on:
                continue
            st = s.net(p.net)
            if st is None:
                continue
            a, b = p.accept
            acc = f"{fmt(a, VOLT) if math.isfinite(a) else '-∞'}..{fmt(b, VOLT) if math.isfinite(b) else '∞'}"
            seen = f"{fmt(st.lo, VOLT)}..{fmt(st.hi, VOLT)}"
            if st.lo < a - 1e-12 or st.hi > b + 1e-12:
                diags.error("input-range", f"{p.path} sees {seen} but accepts {acc}", p.span, f.path, sc)
                continue
            h = rules.get("input-headroom", 0.0)
            if h > 0:
                m_lo = (st.lo - a) / abs(a) if math.isfinite(a) and a else math.inf
                m_hi = (b - st.hi) / abs(b) if math.isfinite(b) and b else math.inf
                if min(m_lo, m_hi) < h:
                    diags.warning("input-headroom",
                                  f"{p.path} sees {seen}, accepts {acc}: margin "
                                  f"{pct(min(m_lo, m_hi))} < rule {pct(h)}", p.span, f.path, sc)
        if f.kind == "converter" and r.powered and r.vin is not None:
            vs = s.vset(f)
            if f.is_linear:
                vdo = s.dropout(f, r.iout)
                src = s.net(f.ins[0].net)
                if r.vin - vs.nom < vdo - 1e-12:
                    diags.error("dropout", f"{f.path} in dropout: vin {fmt(r.vin, VOLT)} - vout "
                                           f"{fmt(vs.nom, VOLT)} < dropout {fmt(vdo, VOLT)}",
                                f.span, f.path, sc)
                elif src.lo - vs.nom < vdo - 1e-12:
                    diags.warning("dropout-margin", f"{f.path} enters dropout at minimum input "
                                                    f"{fmt(src.lo, VOLT)} (dropout {fmt(vdo, VOLT)})",
                                  f.span, f.path, sc)
            elif f.outs[0].vlim is not None:
                src = s.net(f.ins[0].net)
                lim = f.outs[0].vlim.source
                cap = s.vcap(f, r.vin, r.iout)
                if cap < vs.nom - 1e-12:
                    diags.error("output-limited",
                                f"{f.path} cannot reach its {fmt(vs.nom, VOLT)} setpoint: at vin {fmt(r.vin, VOLT)} "
                                f"the output is limited to {fmt(cap, VOLT)} (vlim {lim})", f.outs[0].span, f.path, sc)
                else:
                    cap_lo = s.vcap(f, src.lo, r.iout)
                    if cap_lo < vs.nom - 1e-12:
                        diags.warning("output-limit-margin",
                                      f"{f.path} falls out of regulation at minimum input {fmt(src.lo, VOLT)}: "
                                      f"output limited to {fmt(cap_lo, VOLT)} (vlim {lim})",
                                      f.outs[0].span, f.path, sc)
            elif f.subkind == "buck" and r.vin <= vs.nom:
                diags.error("converter-kind", f"{f.path} is a buck but vin {fmt(r.vin, VOLT)} <= vout "
                                              f"{fmt(vs.nom, VOLT)}", f.span, f.path, sc)
            elif f.subkind == "boost" and r.vin >= vs.nom:
                diags.error("converter-kind", f"{f.path} is a boost but vin {fmt(r.vin, VOLT)} >= vout "
                                              f"{fmt(vs.nom, VOLT)}", f.span, f.path, sc)
            for note in s.eff_notes.get(f.path, []):
                sev = "warning" if "above" in note else "info"
                diags.add(sev, "eff-extrapolated", f"{f.path} efficiency: {note}", f.eff.span, f.path, sc)
