"""Findings (errors, warnings, info) collected by every pipeline layer."""
from __future__ import annotations

from dataclasses import dataclass, field

from .kdl import Span

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    severity: str          # error | warning | info
    code: str              # stable id, e.g. "overload", "unknown-property"
    message: str
    span: Span | None = None
    target: str | None = None      # function/net path the finding is about
    scenario: str | None = None
    waived: str | None = None      # waiver reason if waived

    def __str__(self) -> str:
        loc = f"{self.span}: " if self.span else ""
        scen = f"[{self.scenario}] " if self.scenario else ""
        sev = f"{self.severity} (waived)" if self.waived else self.severity
        tail = f"  -- waived: {self.waived}" if self.waived else ""
        return f"{loc}{sev}: {scen}{self.message} [{self.code}]{tail}"

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "file": self.span.file if self.span else None,
                "line": self.span.line if self.span else None,
                "col": self.span.col if self.span else None,
                "target": self.target, "scenario": self.scenario, "waived": self.waived}


@dataclass
class Diagnostics:
    items: list[Finding] = field(default_factory=list)

    def add(self, severity, code, message, span=None, target=None, scenario=None) -> Finding:
        f = Finding(severity, code, message, span, target, scenario)
        self.items.append(f)
        return f

    def error(self, code, message, span=None, target=None, scenario=None):
        return self.add("error", code, message, span, target, scenario)

    def warning(self, code, message, span=None, target=None, scenario=None):
        return self.add("warning", code, message, span, target, scenario)

    def info(self, code, message, span=None, target=None, scenario=None):
        return self.add("info", code, message, span, target, scenario)

    def extend(self, other: "Diagnostics") -> None:
        self.items.extend(other.items)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.items if f.severity == "error" and not f.waived]

    def has_errors(self) -> bool:
        return bool(self.errors)

    def sorted(self) -> list[Finding]:
        def key(f: Finding):
            s = f.span
            return (bool(f.waived), SEVERITY_ORDER[f.severity],
                    s.file if s else "", s.line if s else 0, s.col if s else 0,
                    f.scenario or "", f.message)
        return sorted(self.items, key=key)
