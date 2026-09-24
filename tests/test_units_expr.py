import pytest

from powertree.expr import Expr, ExprError
from powertree.units import AMP, OHM, VOLT, WATT, Q, UnitError, fmt, parse_quantity, parse_vspec


@pytest.mark.parametrize("text,value,dim", [
    ("3.3V", 3.3, VOLT), ("250 mA", 0.25, AMP), ("45mΩ", 0.045, OHM), ("10R", 10, OHM),
    ("1.2kohm", 1200, OHM), ("15uA", 15e-6, AMP), ("90%", 0.9, (0, 0)), ("0.5", 0.5, (0, 0)),
    ("2W", 2, WATT), ("1e-3A", 1e-3, AMP),
])
def test_parse_quantity(text, value, dim):
    q = parse_quantity(text)
    assert q.value == pytest.approx(value) and q.dim == dim


@pytest.mark.parametrize("bad", ["3.3", "5m", "abc", "1k%"])
def test_parse_quantity_errors(bad):
    with pytest.raises(UnitError):
        parse_quantity(bad, VOLT)


def test_vspec_forms():
    s = parse_vspec("12V ±5%")
    assert (s.nom, s.lo, s.hi) == pytest.approx((12, 11.4, 12.6))
    s = parse_vspec("5V +-50mV")
    assert (s.lo, s.hi) == pytest.approx((4.95, 5.05))
    s = parse_vspec("6V..8.4V")
    assert (s.nom, s.lo, s.hi) == pytest.approx((7.2, 6, 8.4))
    with pytest.raises(UnitError):
        parse_vspec("5V ±1A")


def test_fmt():
    assert fmt(0.0123, AMP) == "12.3 mA"
    assert fmt(3.3, VOLT) == "3.3 V"
    assert fmt(0, WATT) == "0 W"
    assert fmt(1500, OHM) == "1.5 kΩ"


def test_expr_eval_units():
    e = Expr("0.94 - 0.03 * (iout / 750mA)")
    r = e.eval(vin=Q(5, VOLT), vout=Q(3.3, VOLT), iout=Q(0.375, AMP))
    assert r.value == pytest.approx(0.925) and r.dim == (0, 0)
    assert Expr("clamp(1 - vout/vin, 0, 1)").eval(vin=Q(5, VOLT), vout=Q(3, VOLT), iout=Q(1, AMP)).value \
        == pytest.approx(0.4)


@pytest.mark.parametrize("src", ["__import__('os')", "vin.real", "[1]", "x + 1", "t * 2", "1 if 1 else 2"])
def test_expr_rejects(src):
    with pytest.raises(ExprError):
        Expr(src)


def test_expr_unit_mismatch():
    with pytest.raises(ExprError):
        Expr("0.9 - iout").validate_efficiency()
    with pytest.raises(ExprError):
        Expr("iout / 1V").validate_efficiency()
