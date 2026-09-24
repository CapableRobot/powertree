"""Runs the whole pipeline: load -> topology -> every scenario -> waivers."""
from __future__ import annotations

from dataclasses import dataclass, field

from .diagnostics import Diagnostics
from .loader import load
from .model import Design
from .scenario import effective, scenario_names
from .solver import Result, solve


@dataclass
class Analysis:
    design: Design | None
    diags: Diagnostics
    results: dict[str, Result] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.design is not None and not self.diags.has_errors()


def analyze(path: str, scenarios: list[str] | None = None) -> Analysis:
    d, diags = load(path)
    a = Analysis(d, diags)
    if d is None:
        return a
    names = scenarios or scenario_names(d)
    for n in names:
        eff = effective(d, n, diags)
        if eff is None:
            continue
        r = solve(d, eff)
        diags.extend(r.diags)
        a.results[n] = r
    _apply_waivers(d, diags, check_unused=scenarios is None)
    return a


def _apply_waivers(d: Design, diags: Diagnostics, check_unused: bool) -> None:
    for f in diags.items:
        if f.target is None or f.waived:
            continue
        for w in d.waivers:
            if w.code == f.code and (f.target == w.target or f.target.startswith(w.target + ".")):
                f.waived = w.reason
                w.used = True
                break
    if check_unused:
        for w in d.waivers:
            if not w.used:
                diags.warning("unused-waiver",
                              f"waiver '{w.code} {w.target}' matched nothing; remove it if the issue is fixed",
                              w.span)
