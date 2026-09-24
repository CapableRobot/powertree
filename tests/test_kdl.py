import math
import os

import pytest

from powertree.kdl import KdlError, parse

HERE = os.path.dirname(__file__)
EXAMPLES = os.path.join(HERE, "..", "examples")


def test_basic_structure_and_positions():
    nodes = parse('a 1 key="v" {\n  b #true; c\n}\nd', "f.kdl")
    a = nodes[0]
    assert a.name == "a" and a.args[0].value == 1 and a.props["key"].value == "v"
    assert [c.name for c in a.children] == ["b", "c"]
    assert a.children[0].args[0].value is True
    assert (a.span.line, a.span.col) == (1, 1)
    assert (a.children[1].span.line, a.children[1].span.col) == (2, 12)
    assert (a.prop_spans["key"].line, a.prop_spans["key"].col) == (1, 5)
    assert nodes[1].name == "d"


def test_strings():
    n = parse(r'''n "a\tb\u{41}" #"raw \n"# ident-str x.y.z "\
      joined"''')[0]
    assert [v.value for v in n.args] == ["a\tbA", "raw \\n", "ident-str", "x.y.z", "joined"]


def test_multiline_string_dedent():
    n = parse('n """\n    line1\n      line2\n    """')[0]
    assert n.args[0].value == "line1\n  line2"


def test_numbers_and_keywords():
    n = parse("n 1_000 -2.5e3 0x1F 0o17 0b101 #null #inf #-inf #nan #false")[0]
    v = [a.value for a in n.args]
    assert v[:5] == [1000, -2500.0, 31, 15, 5]
    assert v[5] is None and v[6] == math.inf and v[7] == -math.inf and math.isnan(v[8])
    assert v[9] is False


def test_type_annotations():
    n = parse("(t)n (mA)250 k=(V)3.3")[0]
    assert n.type == "t" and n.args[0].type == "mA" and n.props["k"].type == "V"


def test_comments_slashdash_escline():
    nodes = parse("""
    // line comment
    a /* inline */ 1 /-2 3 /-k=4 \\  // continued
        k=5
    /-b {
        c
    }
    d /-{ x } { y }
    """)
    assert [n.name for n in nodes] == ["a", "d"]
    assert [v.value for v in nodes[0].args] == [1, 3]
    assert nodes[0].props["k"].value == 5
    assert [c.name for c in nodes[1].children] == ["y"]


def test_unit_value_hint():
    with pytest.raises(KdlError) as e:
        parse("n v=3.3V", "f.kdl")
    assert "quoted" in e.value.message and e.value.span.col == 5


@pytest.mark.parametrize("src,msg", [
    ('n "abc', "unterminated string"),
    ("n {", "unterminated children"),
    ("n true", "bare 'true'"),
    ("n #maybe", "unknown keyword"),
    ("a\n}", "unexpected '}'"),
    ('n"x"', "whitespace"),
])
def test_errors(src, msg):
    with pytest.raises(KdlError) as e:
        parse(src)
    assert msg in e.value.message


def test_duplicate_props_last_wins():
    n = parse("n k=1 k=2")[0]
    assert n.props["k"].value == 2 and n.duplicate_props


@pytest.mark.parametrize("name", ["sensor-board.kdl", "ups.kdl", "lib/regulators.kdl"])
def test_matches_ckdl_reference_parser(name):
    ckdl = pytest.importorskip("ckdl")
    text = open(os.path.join(EXAMPLES, name), encoding="utf-8").read()

    def mine(nodes):
        return [(n.name, [a.value for a in n.args], {k: v.value for k, v in n.props.items()},
                 mine(n.children)) for n in nodes]

    def ref(nodes):
        def val(v):
            return v.value if hasattr(v, "value") else v
        return [(n.name, [val(a) for a in n.args], {k: val(v) for k, v in n.properties.items()},
                 ref(n.children)) for n in nodes]

    assert mine(parse(text)) == ref(ckdl.parse(text, version=2).nodes)
