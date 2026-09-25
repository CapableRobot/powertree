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
    net: str | None = None         # flattened net name
    net_local: str | None = None   # net name as written in its file
    from_ref: str | None = None
    span: Span | None = None
    # electrical properties (already converted)
    v: VSpec | None = None         # out: setpoint; consumer in: required voltage
    accept: tuple[float, float] | None = None   # in: accepted input range
    adjust: tuple[float, float] | None = None   # converter out: setpoint range
    vlim: Expr | None = None       # converter out: highest reachable output (vin, iout)
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
    ref: str                       # flattened: "IO:2.D1:3"
    span: Span
    part: str | None = None
    desc: str | None = None
    functions: dict[str, Function] = field(default_factory=dict)
    board: str = ""                # path of the enclosing board instance, "" at top level
    desc_template: str | None = None   # desc before {n} substitution


@dataclass
class Net:
    name: str
    span: Span | None = None
    desc: str | None = None
    anonymous: bool = False
    port: str | None = None        # unbound top-level port name (board analysed standalone)
    template: str | None = None    # "IO{n}_24V" for counted nets
    prefix: str = ""               # board prefix of the design that declared it
    index: int | None = None
    drivers: list[Port] = field(default_factory=list)
    sinks: list[Port] = field(default_factory=list)


@dataclass
class SetOp:
    span: Span
    func_vals: dict[str, dict[str, Any]] = field(default_factory=dict)   # function path -> values
    boards: list[tuple[str, str]] = field(default_factory=list)          # (board path, scenario)


@dataclass
class ScenarioDef:
    name: str
    span: Span
    owner: str = ""                # board path owning this scenario, "" = top level
    bases: list[tuple[str, Span]] = field(default_factory=list)
    loads: str | None = None
    raw_sets: list[tuple[str, dict[str, Any], dict[str, Span], Span]] = field(default_factory=list)
    ops: list[SetOp] = field(default_factory=list)
    desc: str | None = None


@dataclass
class Board:
    path: str                      # "IO:3" or "SYS.IO:3"
    design: str
    file: str
    span: Span
    desc: str | None = None
    ports: dict[str, str] = field(default_factory=dict)       # port name -> parent net (flattened)
    port_dirs: dict[str, str] = field(default_factory=dict)
    scenarios: dict[str, ScenarioDef] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return self.path + "."


@dataclass
class Library:
    name: str
    path: str
    origin: str                    # project file / --lib / environment
    files: list[str] = field(default_factory=list)
    sha: str | None = None
    git: str | None = None


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
    boards: dict[str, Board] = field(default_factory=dict)
    libraries: list[Library] = field(default_factory=list)
    topo_order: list[Function] = field(default_factory=list)

    def functions(self):
        for c in self.chips.values():
            yield from c.functions.values()

    def scenario_group(self, owner: str) -> dict[str, ScenarioDef]:
        return self.scenarios if owner == "" else self.boards[owner].scenarios
