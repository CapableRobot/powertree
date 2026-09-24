"""Scenario composition: bases applied in order, then the scenario's own settings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diagnostics import Diagnostics
from .model import Design

DEFAULT_SCENARIO = "nominal"
_LOAD_KEYS = ("i", "p", "r")


@dataclass
class Effective:
    name: str
    loads: str = "nom"
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def scenario_names(d: Design) -> list[str]:
    return list(d.scenarios) or [DEFAULT_SCENARIO]


def effective(d: Design, name: str, diags: Diagnostics) -> Effective | None:
    if not d.scenarios and name == DEFAULT_SCENARIO:
        return Effective(name)
    if name not in d.scenarios:
        diags.error("undefined-scenario", f"no scenario '{name}'; defined: {', '.join(d.scenarios)}")
        return None
    out = Effective(name)
    ok = _apply(d, name, out, [], diags)
    return out if ok else None


def _apply(d: Design, name: str, out: Effective, stack: list[str], diags: Diagnostics) -> bool:
    s = d.scenarios[name]
    if name in stack:
        chain = " -> ".join(stack[stack.index(name):] + [name])
        diags.error("scenario-cycle", f"scenario inheritance loops: {chain}", s.span)
        return False
    stack = stack + [name]
    for base, _span in s.bases:
        if not _apply(d, base, out, stack, diags):
            return False
    if s.loads:
        out.loads = s.loads
    for path, vals, _spans, _span in s.sets:
        cur = out.overrides.setdefault(path, {})
        if any(k in vals for k in _LOAD_KEYS):
            for k in _LOAD_KEYS:
                cur.pop(k, None)
        cur.update(vals)
    return True
