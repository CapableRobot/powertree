"""Declarative description of the file structure, and the structure validator.

The same spec drives validation messages and `powertree schema` (a generated
reference of every node and property), so docs and checks cannot drift.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from . import units
from .diagnostics import Diagnostics
from .expr import Expr, ExprError
from .kdl import Node, Value
from .units import UnitError

# --- value types -----------------------------------------------------------
NAME, STR, BOOL, INT = "name", "string", "bool", "integer"
V, A, W, OHM = "voltage", "current", "power", "resistance"
RATIO = "ratio"          # 0.9 or "90%"
VSPEC = "voltage-spec"   # "5V", "5V ±2%", "4.75V..5.25V"
VRANGE = "voltage-range"  # "3V..5.5V"
EXPR = "expression"

_UNIT_OF = {V: units.VOLT, A: units.AMP, W: units.WATT, OHM: units.OHM}
_EXAMPLE = {V: '"3.3V"', A: '"250mA"', W: '"1.2W"', OHM: '"45mΩ"', RATIO: '0.9 or "90%"',
            VSPEC: '"5V ±2%"', VRANGE: '"3V..5.5V"', EXPR: '"0.9 - 0.02*iout/1A"'}


@dataclass
class Enum:
    choices: tuple[str, ...]

    def __str__(self):
        return " | ".join(self.choices)


@dataclass
class Spec:
    doc: str = ""
    args: tuple[int, int, object] = (0, 0, None)   # (min, max, type)
    arg_names: str = ""
    props: dict[str, object] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    children: dict[str, "Spec"] | None = None
    free_props: bool = False


def convert(v: Value, t) -> object:
    """Convert a KDL value to type t. Raises ValueError with a readable message."""
    x = v.value
    if isinstance(t, Enum):
        if not isinstance(x, str) or x not in t.choices:
            raise ValueError(f"expects one of: {t}; got {x!r}")
        return x
    if t == NAME or t == STR:
        if not isinstance(x, str):
            raise ValueError(f"expects a {'name' if t == NAME else 'string'}, got {x!r}")
        return x
    if t == BOOL:
        if not isinstance(x, bool):
            raise ValueError(f"expects #true or #false, got {x!r}")
        return x
    if t == INT:
        if isinstance(x, bool) or not isinstance(x, int):
            raise ValueError(f"expects an integer, got {x!r}")
        return x
    if t in _UNIT_OF:
        dim = _UNIT_OF[t]
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            if v.type:   # KDL type annotation form: (mA)250
                try:
                    return units.parse_quantity(f"{x}{v.type}", dim).value
                except UnitError:
                    raise ValueError(f"expects a {t}; annotation ({v.type}) is not a {t} unit")
            raise ValueError(f"expects a {t} with units, e.g. {_EXAMPLE[t]}; got bare number {x}")
        if not isinstance(x, str):
            raise ValueError(f"expects a {t}, e.g. {_EXAMPLE[t]}")
        try:
            return units.parse_quantity(x, dim).value
        except UnitError:
            raise ValueError(f"expects a {t}, got {x!r}")
    if t == RATIO:
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            r = float(x)
        elif isinstance(x, str):
            try:
                q = units.parse_quantity(x)
            except UnitError:
                raise ValueError(f"expects a ratio like {_EXAMPLE[RATIO]}, got {x!r}")
            if q.dim != units.NONE:
                raise ValueError(f"expects a ratio like {_EXAMPLE[RATIO]}, got {x!r}")
            r = q.value
        else:
            raise ValueError(f"expects a ratio like {_EXAMPLE[RATIO]}")
        if r < 0:
            raise ValueError("expects a non-negative ratio")
        return r
    if t == VSPEC:
        if not isinstance(x, str):
            raise ValueError(f"expects a voltage like {_EXAMPLE[VSPEC]}")
        try:
            return units.parse_vspec(x)
        except UnitError as e:
            raise ValueError(f"expects a voltage like {_EXAMPLE[VSPEC]}: {e}")
    if t == VRANGE:
        if not isinstance(x, str):
            raise ValueError(f"expects a voltage range like {_EXAMPLE[VRANGE]}")
        try:
            return units.parse_range(x)
        except UnitError as e:
            raise ValueError(f"expects a voltage range like {_EXAMPLE[VRANGE]}: {e}")
    if t == EXPR:
        if not isinstance(x, str):
            raise ValueError("expects an expression string")
        try:
            e = Expr(x)
            e.validate_efficiency()
            return e
        except ExprError as e:
            raise ValueError(str(e))
    raise AssertionError(f"unknown type {t}")  # pragma: no cover


# --- the spec ----------------------------------------------------------------
_link = {"net": NAME, "from": NAME}
_func_props = {"desc": STR, "on": BOOL}
_name_arg = dict(args=(0, 1, NAME), arg_names="[name]")

IN_ACCEPT = Spec("Input port with accepted voltage range.",
                 props={**_link, "range": VRANGE, "vmin": V, "vmax": V})
IN_CONSUMER = Spec("Consumer input; v= gives required voltage with tolerance.",
                   props={**_link, "v": VSPEC, "range": VRANGE, "vmin": V, "vmax": V})
IN_PLAIN = Spec("Input port.", props=dict(_link))
IN_ORING = Spec("OR-ing input; lower priority number wins, else highest voltage.",
                props={**_link, "priority": INT})
OUT_SOURCE = Spec("Regulated output.",
                  props={"net": NAME, "v": VSPEC, "vmin": V, "vmax": V, "imax": A, "share": BOOL})
OUT_CONVERTER = Spec("Converter output; range= is the adjustable setpoint range.",
                     props={"net": NAME, "v": VSPEC, "vmin": V, "vmax": V, "range": VRANGE,
                            "imax": A, "share": BOOL})
OUT_PASS = Spec("Pass-through output.", props={"net": NAME, "imax": A, "share": BOOL})

EFF = Spec("Efficiency: constant (eff 0.9), expression (eff expr=...), or datasheet "
           "table (eff vin= vout= { point iout= eff= }).",
           args=(0, 1, RATIO), arg_names="[ratio]",
           props={"expr": EXPR, "vin": V, "vout": V},
           children={"point": Spec("One efficiency curve point.",
                                    props={"iout": A, "eff": RATIO}, required=("iout", "eff"))})

LOAD = Spec("Load model: i= (constant current), p= (constant power) or r= (resistive). "
            "*min/*max give scenario bounds.",
            props={"i": A, "imin": A, "imax": A, "p": W, "pmin": W, "pmax": W,
                   "r": OHM, "offboard": BOOL})

FUNCTIONS = {
    "provider": Spec("Power source (adapter, battery, upstream board).", **_name_arg,
                     props=dict(_func_props),
                     children={"out": OUT_SOURCE,
                               "r": Spec("Internal source resistance.", args=(1, 1, OHM), arg_names="<ohms>")}),
    "converter": Spec("Regulator or converter.", **_name_arg,
                      props={**_func_props, "kind": Enum(("buck", "boost", "buck-boost", "flyback",
                                                          "isolated", "charge-pump", "charger",
                                                          "linear"))},
                      children={"in": IN_ACCEPT, "out": OUT_CONVERTER,
                                "iq": Spec("Quiescent current.", args=(1, 1, A), arg_names="<amps>"),
                                "eff": EFF,
                                "dropout": Spec("Linear regulator dropout voltage, optionally at a current "
                                                "(scaled linearly with load).",
                                                args=(1, 1, V), arg_names="<volts>", props={"at": A})}),
    "switch": Spec("Load switch / eFuse / FET.", **_name_arg,
                   props={**_func_props, "enable": NAME},
                   children={"in": IN_ACCEPT, "out": OUT_PASS,
                             "rdson": Spec("On resistance.", args=(1, 1, OHM), arg_names="<ohms>")}),
    "series": Spec("Passive series element: fuse, ferrite, sense resistor, connector, cable.", **_name_arg,
                   props=dict(_func_props),
                   children={"in": IN_PLAIN, "out": OUT_PASS,
                             "r": Spec("Series resistance.", args=(1, 1, OHM), arg_names="<ohms>")}),
    "oring": Spec("Diode or ideal-diode OR of several inputs.", **_name_arg,
                  props=dict(_func_props),
                  children={"in": IN_ORING, "out": OUT_PASS,
                            "vf": Spec("Forward voltage drop.", args=(1, 1, V), arg_names="<volts>"),
                            "r": Spec("On resistance.", args=(1, 1, OHM), arg_names="<ohms>")}),
    "consumer": Spec("A chip power domain drawing power.", **_name_arg,
                     props=dict(_func_props),
                     children={"in": IN_CONSUMER, "load": LOAD}),
}

RULES = {
    "converter-load": Spec("Warn when converter output exceeds this fraction of imax.",
                           props={"max": RATIO}, required=("max",)),
    "provider-load": Spec("Warn when provider output exceeds this fraction of imax.",
                          props={"max": RATIO}, required=("max",)),
    "switch-load": Spec("Warn when switch/series/oring current exceeds this fraction of imax.",
                        props={"max": RATIO}, required=("max",)),
    "input-headroom": Spec("Warn when supplied voltage range is within this fraction of an "
                           "input's accepted limits.", props={"min": RATIO}, required=("min",)),
}

TOP = {
    "design": Spec("Design name (one per design file).", args=(1, 1, NAME), arg_names="<name>",
                   props={"desc": STR}),
    "use": Spec("Load part definitions from another file (path relative to this file).",
                args=(1, 1, STR), arg_names="<path>"),
    "net": Spec("Named bus. With count=, the name must contain {n}: net \"IO{n}_24V\" count=8.",
                args=(1, 1, NAME), arg_names="<NAME>", props={"desc": STR, "count": INT}),
    "chip": Spec("A physical component instance; groups all of its power functions. "
                 "count= makes instances REF:1..REF:count; {n} in net=/from=/desc= is the index.",
                 args=(1, 1, NAME), arg_names="<REF>", props={"part": NAME, "desc": STR, "count": INT},
                 children=FUNCTIONS),
    "port": Spec("Board interface: exposes an internal net to the design that instantiates this one.",
                 args=(1, 1, NAME), arg_names="<PORT>",
                 props={"net": NAME, "dir": Enum(("in", "out", "bidir")), "desc": STR},
                 required=("net",)),
    "board": Spec("Instance of another design (a board, module or subsystem). count= works as for chip.",
                  args=(1, 1, NAME), arg_names="<REF>",
                  props={"design": NAME, "count": INT, "desc": STR}, required=("design",),
                  children={"port": Spec("Bind the board's port to a net of this design.",
                                         args=(1, 1, NAME), arg_names="<PORT>", props={"net": NAME},
                                         required=("net",))}),
    "part": Spec("Reusable component definition, instantiated by chip part=.",
                 args=(1, 1, NAME), arg_names="<PART>", props={"desc": STR}, children=FUNCTIONS),
    "scenario": Spec("Operating scenario; base= inherits (comma-separated, applied in order).",
                     args=(1, 1, NAME), arg_names="<name>", props={"base": STR, "desc": STR},
                     children={"loads": Spec("Load level for all consumers.",
                                             args=(1, 1, Enum(("nom", "min", "max"))),
                                             arg_names="nom|min|max"),
                               "set": Spec("Override properties in this scenario. Target: function, "
                                           "chip, or board; selectors REF:3, REF:*, REF:1..4.",
                                           args=(1, 1, NAME), arg_names="<target>",
                                           free_props=True)}),
    "rules": Spec("Design-rule thresholds.", children=RULES),
    "waive": Spec("Accept a finding: waive <code> <target> reason=... (target may use selectors).",
                  args=(2, 2, NAME), arg_names="<code> <target>", props={"reason": STR},
                  required=("reason",)),
}


# --- validator ---------------------------------------------------------------
def _suggest(word: str, options) -> str:
    m = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" (did you mean '{m[0]}'?)" if m else ""


def validate(nodes: list[Node], diags: Diagnostics, specs: dict[str, Spec] = TOP,
             context: str = "top level") -> None:
    for node in nodes:
        spec = specs.get(node.name)
        if spec is None:
            diags.error("unknown-node",
                        f"unknown node '{node.name}' in {context}{_suggest(node.name, specs)}; "
                        f"expected one of: {', '.join(specs)}", node.span)
            continue
        where = f"'{node.name}'"
        lo, hi, atype = spec.args
        if not (lo <= len(node.args) <= hi):
            want = f"{lo}" if lo == hi else f"{lo} to {hi}"
            usage = f" (usage: {node.name} {spec.arg_names})" if spec.arg_names else ""
            diags.error("argument-count", f"{where} takes {want} argument(s), got {len(node.args)}{usage}",
                        node.span)
        else:
            for a in node.args:
                try:
                    convert(a, atype)
                except ValueError as e:
                    diags.error("bad-value", f"{where} argument {e}", a.span)
        for key, span in node.duplicate_props:
            diags.warning("duplicate-property", f"property '{key}' given twice on {where}; last wins", span)
        if not spec.free_props:
            for key, val in node.props.items():
                t = spec.props.get(key)
                if t is None:
                    allowed = ", ".join(spec.props) or "none"
                    diags.error("unknown-property",
                                f"unknown property '{key}' on {where}{_suggest(key, spec.props)}; "
                                f"allowed: {allowed}", node.prop_spans[key])
                    continue
                try:
                    convert(val, t)
                except ValueError as e:
                    diags.error("bad-value", f"'{key}' {e}", node.prop_spans[key])
        for req in spec.required:
            if req not in node.props:
                diags.error("missing-property", f"{where} requires property '{req}'", node.span)
        if node.children:
            if spec.children is None:
                diags.error("unexpected-children", f"{where} does not take a children block",
                            node.children[0].span)
            else:
                validate(node.children, diags, spec.children, f"'{node.name}'")


def schema_reference() -> str:
    """Human-readable reference generated from the spec."""
    lines: list[str] = []

    def walk(specs: dict[str, Spec], depth: int) -> None:
        for name, s in specs.items():
            pad = "  " * depth
            head = f"{pad}{name} {s.arg_names}".rstrip()
            lines.append(f"{head}    // {s.doc}" if s.doc else head)
            for k, t in s.props.items():
                req = " (required)" if k in s.required else ""
                lines.append(f"{pad}    {k}= {t}{req}")
            if s.free_props:
                lines.append(f"{pad}    <property>= <value>   (see SPEC.md: scenario set)")
            if s.children:
                walk(s.children, depth + 1)

    walk(TOP, 0)
    return "\n".join(lines)


PROJECT = {
    "library": Spec("Named library root: use \"<name>:file.kdl\", part=<name>:PART.",
                    args=(1, 1, NAME), arg_names="<name>", props={"path": STR}, required=("path",)),
}

TOP["standalone"] = Spec(
    "Nodes used only when this file is the top-level design (e.g. a bench supply for a board's "
    "input port); ignored when the design is instantiated as a board.",
    children={k: TOP[k] for k in ("net", "chip", "board", "scenario", "rules", "waive")})

