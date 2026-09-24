"""Quantities with units: volts, amps, watts, ohms, and dimensionless.

Dimensions are tracked as (volt exponent, amp exponent):
V=(1,0), A=(0,1), W=(1,1), Ω=(1,-1), dimensionless=(0,0).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

Dim = tuple[int, int]
VOLT: Dim = (1, 0)
AMP: Dim = (0, 1)
WATT: Dim = (1, 1)
OHM: Dim = (1, -1)
NONE: Dim = (0, 0)

DIM_NAMES = {VOLT: "voltage", AMP: "current", WATT: "power",
             OHM: "resistance", NONE: "dimensionless number"}
DIM_SYMBOL = {VOLT: "V", AMP: "A", WATT: "W", OHM: "Ω", NONE: ""}

PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
            "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}
UNITS = {"V": VOLT, "A": AMP, "W": WATT, "Ω": OHM, "ohm": OHM,
         "ohms": OHM, "Ohm": OHM, "R": OHM}

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_PFX = r"(?:p|n|u|µ|μ|m|k|M|G)"
_UNIT = r"(?:V|A|W|Ω|ohms|ohm|Ohm|R)"
QUANTITY_RE = re.compile(rf"^\s*({_NUM})\s*({_PFX})?({_UNIT}|%)?\s*$")
# used by the expression language to find unit literals inside formulas
LITERAL_RE = re.compile(rf"(?<![\w.])({_NUM})\s?({_PFX})?({_UNIT})(?![\w])")


class UnitError(ValueError):
    pass


@dataclass(frozen=True)
class Q:
    value: float
    dim: Dim = NONE

    def _same(self, o: "Q", op: str) -> None:
        if self.dim != o.dim:
            raise UnitError(f"cannot {op} {DIM_NAMES.get(self.dim, self.dim)} "
                            f"and {DIM_NAMES.get(o.dim, o.dim)}")

    def __add__(self, o):
        o = _q(o)
        self._same(o, "add")
        return Q(self.value + o.value, self.dim)

    __radd__ = __add__

    def __sub__(self, o):
        o = _q(o)
        self._same(o, "subtract")
        return Q(self.value - o.value, self.dim)

    def __rsub__(self, o):
        return _q(o) - self

    def __mul__(self, o):
        o = _q(o)
        return Q(self.value * o.value, (self.dim[0] + o.dim[0], self.dim[1] + o.dim[1]))

    __rmul__ = __mul__

    def __truediv__(self, o):
        o = _q(o)
        return Q(self.value / o.value, (self.dim[0] - o.dim[0], self.dim[1] - o.dim[1]))

    def __rtruediv__(self, o):
        return _q(o) / self

    def __pow__(self, o):
        o = _q(o)
        if o.dim != NONE:
            raise UnitError("exponent must be dimensionless")
        if self.dim != NONE and o.value != int(o.value):
            raise UnitError("non-integer power of a quantity with units")
        e = o.value
        return Q(self.value ** e, (int(self.dim[0] * e), int(self.dim[1] * e)))

    def __neg__(self):
        return Q(-self.value, self.dim)

    def __pos__(self):
        return self

    def __str__(self) -> str:
        return fmt(self.value, self.dim)


def _q(x) -> Q:
    return x if isinstance(x, Q) else Q(float(x), NONE)


def parse_quantity(text: str, expect: Dim | None = None) -> Q:
    """Parse '3.3V', '250 mA', '45mΩ', '90%', '0.9'."""
    m = QUANTITY_RE.match(str(text))
    if not m:
        raise UnitError(f"cannot parse {text!r} as a quantity")
    num, pfx, unit = m.groups()
    value = float(num)
    if unit == "%":
        if pfx:
            raise UnitError(f"prefix not allowed on % in {text!r}")
        q = Q(value / 100.0, NONE)
    elif unit:
        q = Q(value * PREFIXES.get(pfx or "", 1.0), UNITS[unit])
    else:
        if pfx:
            raise UnitError(f"{text!r} has a prefix but no unit")
        q = Q(value, NONE)
    if expect is not None and q.dim != expect:
        raise UnitError(f"expected a {DIM_NAMES[expect]}, got {text!r}")
    return q


@dataclass(frozen=True)
class VSpec:
    """A voltage with nominal value and min/max bounds."""
    nom: float
    lo: float
    hi: float

    def __str__(self) -> str:
        if self.lo == self.nom == self.hi:
            return fmt(self.nom, VOLT)
        return f"{fmt(self.nom, VOLT)} [{fmt(self.lo, VOLT)}..{fmt(self.hi, VOLT)}]"


_TOL_RE = re.compile(r"^(.*?)\s*(?:±|\+/-|\+-)\s*(.+)$")


def parse_range(text: str, expect: Dim = VOLT) -> tuple[float, float]:
    """Parse 'a..b' into (lo, hi)."""
    if ".." not in text:
        raise UnitError(f"expected a range like '3V..5.5V', got {text!r}")
    a, b = text.split("..", 1)
    lo = parse_quantity(a, expect).value
    hi = parse_quantity(b, expect).value
    if lo > hi:
        raise UnitError(f"range {text!r} has min > max")
    return lo, hi


def parse_vspec(text: str) -> VSpec:
    """Parse '12V', '12V ±5%', '5V +-50mV', or '11.4V..12.6V'."""
    text = str(text).strip()
    if ".." in text:
        lo, hi = parse_range(text)
        return VSpec((lo + hi) / 2, lo, hi)
    m = _TOL_RE.match(text)
    if m:
        nom = parse_quantity(m.group(1), VOLT).value
        tol = parse_quantity(m.group(2))
        if tol.dim == NONE and "%" in m.group(2):
            d = abs(nom) * tol.value
        elif tol.dim == VOLT:
            d = tol.value
        else:
            raise UnitError(f"tolerance in {text!r} must be a percentage or a voltage")
        return VSpec(nom, nom - d, nom + d)
    nom = parse_quantity(text, VOLT).value
    return VSpec(nom, nom, nom)


_ENG = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
        (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


def fmt(value: float, dim: Dim = NONE, digits: int = 3) -> str:
    """Engineering formatting: fmt(0.0123, AMP) -> '12.3 mA'."""
    sym = DIM_SYMBOL.get(dim, "?")
    if value is None:
        return "—"
    if not math.isfinite(value):
        return f"{value} {sym}".strip()
    if value == 0:
        return f"0 {sym}".strip()
    a = abs(value)
    for scale, p in _ENG:
        if a >= scale * 0.9995:
            break
    else:
        scale, p = _ENG[-1]
    m = value / scale
    s = f"{m:.{digits}g}"
    return f"{s} {p}{sym}".strip()


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"
