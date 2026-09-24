"""In-memory model of a power design after loading and resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expr import Expr
from .kdl import Span
from .units import VSpec


@dataclass
class Port:
    direction: str                 # "in" | "out"
    func: "Function"
    index: int = 0
    net: str | None = None
    from_ref: str | None = None
    span: Span | None = None
    # electrical properties (already converted)
    v: VSpec | None = None         # out: setpoint; consumer in: required voltage
    accept: tuple[float, float] | None = None   # in: accepted input range
    adjust: tuple[float, float] | None = None   # converter out: setpoint range
    imax: float | None = None
    share: bool = False
    priority: int | None = None

    @property
    def path(self) -> str:
        base = f"{self.func.path}.{self.direction}"
        if self.direction == "in" and len(self.func.ins) > 1:
            return f"{base}[{self.net or self.index}]"
        return base


@dataclass
class EffTable:
    vin: float | None
    vout: float | None
    points: list[tuple[float, float]]    # (iout, eff), sorted by iout
    span: Span | None = None


@dataclass
class Efficiency:
    constant: float | None = None
    expr: Expr | None = None
    tables: list[EffTable] = field(default_factory=list)
    span: Span | None = None


@dataclass
class Load:
    model: str                     # "i" | "p" | "r"
    nom: float
    min: float
    max: float
    offboard: bool = False
    span: Span | None = None


@dataclass
class Function:
    kind: str                      # provider|converter|switch|series|oring|consumer
    name: str
    chip: "Chip"
    span: Span
    desc: str | None = None
    on: bool = True
    subkind: str | None = None     # converter kind=
    enable: str | None = None
    ins: list[Port] = field(default_factory=list)
    outs: list[Port] = field(default_factory=list)
    iq: float = 0.0
    eff: Efficiency | None = None
    dropout: float | None = None
    dropout_at: float | None = None
    r: float = 0.0                 # rdson / series r / source r / oring r
    vf: float = 0.0
    load: Load | None = None

    @property
    def path(self) -> str:
        return f"{self.chip.ref}.{self.name}"

    @property
    def is_linear(self) -> bool:
        return self.kind == "converter" and self.subkind == "linear"


@dataclass
class Chip:
    ref: str
    span: Span
    part: str | None = None
    desc: str | None = None
    functions: dict[str, Function] = field(default_factory=dict)


@dataclass
class Net:
    name: str
    span: Span | None = None
    desc: str | None = None
    anonymous: bool = False
    drivers: list[Port] = field(default_factory=list)
    sinks: list[Port] = field(default_factory=list)


@dataclass
class ScenarioDef:
    name: str
    span: Span
    bases: list[tuple[str, Span]] = field(default_factory=list)
    loads: str | None = None
    sets: list[tuple[str, dict[str, Any], dict[str, Span], Span]] = field(default_factory=list)
    desc: str | None = None


@dataclass
class Waiver:
    code: str
    target: str
    reason: str
    span: Span
    used: bool = False


DEFAULT_RULES = {"converter-load": 0.8, "provider-load": 0.8, "switch-load": 0.8,
                 "input-headroom": 0.0}


@dataclass
class Design:
    name: str
    file: str
    nets: dict[str, Net] = field(default_factory=dict)
    chips: dict[str, Chip] = field(default_factory=dict)
    scenarios: dict[str, ScenarioDef] = field(default_factory=dict)
    rules: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RULES))
    waivers: list[Waiver] = field(default_factory=list)
    topo_order: list[Function] = field(default_factory=list)

    def functions(self):
        for c in self.chips.values():
            yield from c.functions.values()
