"""Restricted, unit-aware expression language used for efficiency formulas.

Example: "0.94 - 0.03 * (iout / 750mA)"
Variables: vin, vout (volts), iout (amps). Reserved for later: t, temp.
Functions: min, max, abs, sqrt, exp, log, log10, clamp(x, lo, hi).
Unit literals (e.g. 750mA, 3.3V, 45mΩ) carry dimensions; mismatched
additions raise errors. Nothing else from Python is reachable.
"""
from __future__ import annotations

import ast
import math

from .units import AMP, LITERAL_RE, NONE, PREFIXES, UNITS, VOLT, Q, UnitError

VARIABLES = {"vin": VOLT, "vout": VOLT, "iout": AMP}
LIMIT_VARIABLES = {"vin": VOLT, "iout": AMP}   # output-limit expressions cannot use vout
RESERVED = {"t", "temp"}


class ExprError(ValueError):
    pass


def _clamp(x: Q, lo: Q, hi: Q) -> Q:
    return max(lo, min(hi, x, key=lambda q: q.value), key=lambda q: q.value)


def _dimless(fn):
    def wrap(x: Q) -> Q:
        if x.dim != NONE:
            raise UnitError(f"{fn.__name__}() needs a dimensionless argument")
        return Q(fn(x.value), NONE)
    wrap.__name__ = fn.__name__
    return wrap


FUNCTIONS = {
    "min": lambda *a: min(a, key=lambda q: _same_dims(a) or q.value),
    "max": lambda *a: max(a, key=lambda q: _same_dims(a) or q.value),
    "abs": lambda x: Q(abs(x.value), x.dim),
    "sqrt": lambda x: x ** Q(0.5) if x.dim == NONE else _sqrt_dim(x),
    "exp": _dimless(math.exp),
    "log": _dimless(math.log),
    "log10": _dimless(math.log10),
    "clamp": lambda x, lo, hi: (_same_dims((x, lo, hi)) or _clamp(x, lo, hi)),
}


def _same_dims(args) -> None:
    dims = {a.dim for a in args}
    if len(dims) > 1:
        raise UnitError("arguments have different units (a bare number is dimensionless; "
                        "give it a unit, e.g. 0.8V)")
    return None


def _sqrt_dim(x: Q) -> Q:
    if x.dim[0] % 2 or x.dim[1] % 2:
        raise UnitError("sqrt of a quantity with odd unit powers")
    return Q(math.sqrt(x.value), (x.dim[0] // 2, x.dim[1] // 2))


class Expr:
    def __init__(self, source: str, variables: dict | None = None):
        self.source = source
        self.variables = VARIABLES if variables is None else variables
        self._literals: dict[str, Q] = {}

        def repl(m):
            num, pfx, unit = m.groups()
            name = f"__q{len(self._literals)}"
            self._literals[name] = Q(float(num) * PREFIXES.get(pfx or "", 1.0), UNITS[unit])
            return name

        text = LITERAL_RE.sub(repl, source)
        try:
            self._tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as e:
            raise ExprError(f"syntax error in expression {source!r}: {e.msg}") from None
        self._check(self._tree.body)

    def _check(self, node: ast.AST) -> None:
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                raise ExprError(f"operator not allowed in {self.source!r}")
            self._check(node.left)
            self._check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.UAdd, ast.USub)):
                raise ExprError(f"operator not allowed in {self.source!r}")
            self._check(node.operand)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise ExprError(f"only numbers are allowed in {self.source!r}")
        elif isinstance(node, ast.Name):
            n = node.id
            if n in RESERVED:
                raise ExprError(f"'{n}' is reserved for future use")
            if n not in self.variables and n not in self._literals:
                raise ExprError(f"unknown name '{n}' in {self.source!r} "
                                f"(variables here: {', '.join(self.variables)})")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExprError(f"unknown function in {self.source!r} "
                                f"(allowed: {', '.join(FUNCTIONS)})")
            if node.keywords:
                raise ExprError("keyword arguments are not allowed")
            for a in node.args:
                self._check(a)
        else:
            raise ExprError(f"construct not allowed in expression {self.source!r}")

    def eval(self, **values: Q) -> Q:
        env = dict(self._literals)
        env.update(values)
        try:
            return self._eval(self._tree.body, env)
        except ZeroDivisionError:
            raise ExprError(f"division by zero in {self.source!r}") from None
        except UnitError as e:
            raise ExprError(f"unit error in {self.source!r}: {e}") from None
        except (ValueError, OverflowError) as e:
            raise ExprError(f"math error in {self.source!r}: {e}") from None

    def _eval(self, node, env) -> Q:
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, env), self._eval(node.right, env)
            op = node.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Div):
                return a / b
            return a ** b
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, env)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Constant):
            return Q(float(node.value), NONE)
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Call):
            args = [self._eval(a, env) for a in node.args]
            return FUNCTIONS[node.func.id](*args)
        raise ExprError("internal: unexpected node")  # pragma: no cover

    def validate_voltage(self) -> None:
        """For output-limit expressions: variables vin, iout; result must be a voltage."""
        r = self.eval(vin=Q(12.0, VOLT), iout=Q(0.5, AMP))
        if r.dim != VOLT:
            raise ExprError(f"expression {self.source!r} must give a voltage")

    def validate_efficiency(self) -> None:
        """Evaluate at a sample point to catch unit errors at load time."""
        r = self.eval(vin=Q(12.0, VOLT), vout=Q(3.3, VOLT), iout=Q(0.5, AMP))
        if r.dim != NONE:
            raise ExprError(f"efficiency expression {self.source!r} must be dimensionless")
