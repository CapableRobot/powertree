"""Scenario composition.

Order: defaults -> bases (in listed order, recursively) -> the scenario's own
`loads` and `set` lines, in file order. `set BOARD scenario=X` applies that
board's scenario X at that point, scoped to the board. Later settings win.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnostics import Diagnostics
from .model import Design, ScenarioDef

DEFAULT_SCENARIO = "nominal"
_LOAD_KEYS = ("i", "p", "r")


@dataclass
class Effective:
    name: str
    loads: str = "nom"                                   # top-level load level (for display)
    loads_rules: list[tuple[str, str]] = field(default_factory=list)   # (path prefix, level), in order
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    board_scenarios: dict[str, str] = field(default_factory=dict)      # board path -> scenario applied

    def load_level(self, path: str) -> str:
        level = "nom"
        for prefix, lv in self.loads_rules:
            if path.startswith(prefix):
                level = lv
        return level


def scenario_names(d: Design) -> list[str]:
    return list(d.scenarios) or [DEFAULT_SCENARIO]


def effective(d: Design, name: str, diags: Diagnostics) -> Effective | None:
    if not d.scenarios and name == DEFAULT_SCENARIO:
        return Effective(name)
    if name not in d.scenarios:
        diags.error("undefined-scenario", f"no scenario '{name}'; defined: {', '.join(d.scenarios)}")
        return None
    out = Effective(name)
    return out if _apply(d, d.scenarios[name], out, (), diags) else None


def _apply(d: Design, s: ScenarioDef, out: Effective, stack: tuple, diags: Diagnostics) -> bool:
    key = (s.owner, s.name)
    if key in stack:
        diags.error("scenario-cycle", f"scenario inheritance loops at '{s.name}'", s.span)
        return False
    stack = stack + (key,)
    group = d.scenario_group(s.owner)
    for base, _span in s.bases:
        if not _apply(d, group[base], out, stack, diags):
            return False
    prefix = d.boards[s.owner].prefix if s.owner else ""
    if s.loads:
        out.loads_rules.append((prefix, s.loads))
        if not s.owner:
            out.loads = s.loads
    for op in s.ops:
        for bpath, sname in op.boards:
            out.board_scenarios[bpath] = sname
            if not _apply(d, d.boards[bpath].scenarios[sname], out, stack, diags):
                return False
        for path, vals in op.func_vals.items():
            cur = out.overrides.setdefault(path, {})
            if any(k in vals for k in _LOAD_KEYS):
                for k in _LOAD_KEYS:
                    cur.pop(k, None)
            cur.update(vals)
    return True
