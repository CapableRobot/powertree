"""Small KDL v2 parser that keeps source positions.

Existing Python parsers either lack KDL v2 support (kdl-py) or do not expose
line/column information (ckdl), which is needed for useful diagnostics.
This parser implements the KDL v2 grammar: nodes, arguments, properties,
children, type annotations, identifier/quoted/raw/multi-line strings,
all number forms, #keywords, comments, slashdash and line continuations.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Span:
    file: str
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


class KdlError(Exception):
    def __init__(self, message: str, span: Span):
        super().__init__(f"{span}: {message}")
        self.message = message
        self.span = span


@dataclass
class Value:
    value: Any  # str | int | float | bool | None
    type: str | None
    span: Span


@dataclass
class Node:
    name: str
    type: str | None
    span: Span
    args: list[Value] = field(default_factory=list)
    props: dict[str, Value] = field(default_factory=dict)
    prop_spans: dict[str, Span] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    duplicate_props: list[tuple[str, Span]] = field(default_factory=list)

    def child(self, name: str) -> "Node | None":
        for c in self.children:
            if c.name == name:
                return c
        return None

    def children_named(self, name: str) -> list["Node"]:
        return [c for c in self.children if c.name == name]


_WS = set("\t \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
          "\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff")
_NL_CHARS = set("\r\n\u0085\u000b\u000c\u2028\u2029")
_NON_IDENT = set('\\/(){};[]"#=')
_KEYWORDS = {"#true": True, "#false": False, "#null": None,
             "#inf": math.inf, "#-inf": -math.inf, "#nan": math.nan}
_BARE_FORBIDDEN = {"true", "false", "null", "inf", "-inf", "nan"}

_NUM_RE = re.compile(
    r"[+-]?(?:0x[0-9a-fA-F][0-9a-fA-F_]*|0o[0-7][0-7_]*|0b[01][01_]*|"
    r"[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?)")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"',
            "b": "\b", "f": "\f", "s": " "}


def _disallowed(ch: str) -> bool:
    o = ord(ch)
    if ch in _WS or ch in _NL_CHARS:
        return False
    return (o < 0x20 or o == 0x7F or 0xD800 <= o <= 0xDFFF
            or 0x200E <= o <= 0x200F or 0x202A <= o <= 0x202E
            or 0x2066 <= o <= 0x2069)


class _Parser:
    def __init__(self, text: str, filename: str):
        self.s = text
        self.n = len(text)
        self.file = filename
        self.i = 1 if text.startswith("\ufeff") else 0
        self._line_starts = [0]
        j = 0
        while j < self.n:
            c = text[j]
            if c == "\r" and j + 1 < self.n and text[j + 1] == "\n":
                j += 2
                self._line_starts.append(j)
                continue
            if c in _NL_CHARS:
                self._line_starts.append(j + 1)
            j += 1
        for k, c in enumerate(text):
            if _disallowed(c) or (c == "\ufeff" and k > 0):
                raise KdlError(f"disallowed character U+{ord(c):04X}", self.span(k))

    # -- position helpers -------------------------------------------------
    def span(self, pos: int | None = None) -> Span:
        pos = self.i if pos is None else pos
        line = bisect.bisect_right(self._line_starts, pos) - 1
        return Span(self.file, line + 1, pos - self._line_starts[line] + 1)

    def err(self, msg: str, pos: int | None = None) -> KdlError:
        return KdlError(msg, self.span(pos))

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < self.n else ""

    def startswith(self, t: str) -> bool:
        return self.s.startswith(t, self.i)

    def eof(self) -> bool:
        return self.i >= self.n

    # -- whitespace / comments --------------------------------------------
    def newline(self) -> bool:
        if self.startswith("\r\n"):
            self.i += 2
            return True
        if self.peek() and self.peek() in _NL_CHARS:
            self.i += 1
            return True
        return False

    def ws(self) -> bool:
        start = self.i
        while True:
            if self.peek() and self.peek() in _WS:
                self.i += 1
            elif self.startswith("/*"):
                self.block_comment()
            else:
                break
        return self.i > start

    def block_comment(self) -> None:
        start = self.i
        self.i += 2
        depth = 1
        while depth:
            if self.eof():
                raise self.err("unterminated /* comment", start)
            if self.startswith("/*"):
                depth += 1
                self.i += 2
            elif self.startswith("*/"):
                depth -= 1
                self.i += 2
            else:
                self.i += 1

    def line_comment(self) -> None:
        while not self.eof() and self.peek() not in _NL_CHARS:
            self.i += 1
        self.newline()

    def escline(self) -> bool:
        if self.peek() != "\\":
            return False
        start = self.i
        self.i += 1
        self.ws()
        if self.startswith("//"):
            self.line_comment()
        elif not self.newline() and not self.eof():
            raise self.err("expected newline after line continuation '\\'", start)
        return True

    def node_space(self) -> bool:
        start = self.i
        while self.ws() or self.escline():
            pass
        return self.i > start

    def line_space(self) -> None:
        while True:
            if self.ws() or self.newline():
                continue
            if self.startswith("//"):
                self.line_comment()
                continue
            break

    # -- document structure -------------------------------------------------
    def document(self) -> list[Node]:
        nodes = self.nodes()
        if not self.eof():
            raise self.err("unexpected '}'")
        return nodes

    def nodes(self) -> list[Node]:
        out: list[Node] = []
        while True:
            self.line_space()
            if self.eof() or self.peek() == "}":
                return out
            if self.startswith("/-"):
                self.i += 2
                self.line_space()
                self.node()
                continue
            out.append(self.node())

    def node(self) -> Node:
        start = self.i
        ntype = self.type_annotation()
        if ntype is not None:
            self.node_space()
        if self.eof() or self.peek() in "{};":
            raise self.err("expected node name")
        name = self.string()
        node = Node(name=name, type=ntype, span=self.span(start))
        children_seen = False
        while True:
            had_space = self.node_space()
            c = self.peek()
            if self.eof():
                return node
            if self.newline():
                return node
            if c == ";":
                self.i += 1
                return node
            if c == "}":
                return node
            if self.startswith("//"):
                self.line_comment()
                return node
            if self.startswith("/-"):
                self.i += 2
                self.node_space()
                if self.peek() == "{":
                    self.children_block()
                    children_seen = True
                elif children_seen:
                    raise self.err("only children blocks may be slashdashed after children")
                else:
                    self.entry(Node("_", None, node.span))
                continue
            if c == "{":
                if children_seen and node.children:
                    raise self.err("node has more than one children block")
                node.children = self.children_block()
                children_seen = True
                continue
            if children_seen:
                raise self.err("arguments and properties must come before the children block")
            if not had_space:
                raise self.err("expected whitespace between node entries")
            self.entry(node)

    def children_block(self) -> list[Node]:
        start = self.i
        self.i += 1
        kids = self.nodes()
        if self.peek() != "}":
            raise self.err("unterminated children block '{'", start)
        self.i += 1
        return kids

    def entry(self, node: Node) -> None:
        start = self.i
        vtype = self.type_annotation()
        if vtype is not None:
            self.node_space()
            node.args.append(self.value(vtype, start))
            return
        c = self.peek()
        if c == '"' or (c == "#" and self._is_raw_start()) or self._is_ident_start():
            key_or_val = self.string()
            save = self.i
            self.node_space()
            if self.peek() == "=":
                self.i += 1
                self.node_space()
                vstart = self.i
                vt = self.type_annotation()
                if vt is not None:
                    self.node_space()
                val = self.value(vt, vstart)
                if key_or_val in node.props:
                    node.duplicate_props.append((key_or_val, self.span(start)))
                node.props[key_or_val] = val
                node.prop_spans[key_or_val] = self.span(start)
                return
            self.i = save
            node.args.append(Value(key_or_val, None, self.span(start)))
            return
        node.args.append(self.value(None, start))

    def type_annotation(self) -> str | None:
        if self.peek() != "(":
            return None
        start = self.i
        self.i += 1
        self.node_space()
        t = self.string()
        self.node_space()
        if self.peek() != ")":
            raise self.err("unterminated type annotation", start)
        self.i += 1
        return t

    # -- values -------------------------------------------------------------
    def value(self, vtype: str | None, start: int) -> Value:
        c = self.peek()
        sp = self.span(start)
        if c == "#" and not self._is_raw_start():
            m = re.match(r"#-?[a-z]+", self.s[self.i:])
            word = m.group(0) if m else "#"
            if word not in _KEYWORDS:
                raise self.err(f"unknown keyword '{word}'")
            self.i += len(word)
            self._check_value_end(start)
            return Value(_KEYWORDS[word], vtype, sp)
        if c.isdigit() or (c in "+-." and self.peek(1).isdigit()) or (c in "+-" and self.peek(1) == "." ):
            return Value(self.number(start), vtype, sp)
        if c == '"' or c == "#" or self._is_ident_start():
            return Value(self.string(), vtype, sp)
        raise self.err(f"unexpected character {c!r}")

    def _check_value_end(self, start: int) -> None:
        c = self.peek()
        if c and not (c in _WS or c in _NL_CHARS or c in ";}{)=\\" or self.startswith("/")):
            j = self.i
            while j < self.n and not (self.s[j] in _WS or self.s[j] in _NL_CHARS or self.s[j] in ";{}"):
                j += 1
            tok = self.s[start:j]
            hint = ""
            if re.match(r"^[+-]?[0-9.]", tok):
                hint = f'; values with units must be quoted, e.g. "{tok}"'
            raise self.err(f"invalid value '{tok}'{hint}", start)

    def number(self, start: int) -> int | float:
        m = _NUM_RE.match(self.s, self.i)
        if not m:
            self._check_value_end(start)
            raise self.err("invalid number", start)
        tok = m.group(0)
        self.i = m.end()
        self._check_value_end(start)
        t = tok.replace("_", "")
        sign = -1 if t.startswith("-") else 1
        body = t.lstrip("+-")
        if body.startswith("0x"):
            return sign * int(body[2:], 16)
        if body.startswith("0o"):
            return sign * int(body[2:], 8)
        if body.startswith("0b"):
            return sign * int(body[2:], 2)
        if any(ch in body for ch in ".eE"):
            return float(t)
        return int(t)

    def _is_raw_start(self) -> bool:
        j = self.i
        while j < self.n and self.s[j] == "#":
            j += 1
        return j > self.i and j < self.n and self.s[j] == '"'

    def _is_ident_start(self) -> bool:
        c = self.peek()
        if not c or c in _WS or c in _NL_CHARS or c in _NON_IDENT:
            return False
        if c.isdigit():
            return False
        if c in "+-":
            n1 = self.peek(1)
            if n1.isdigit() or (n1 == "." and self.peek(2).isdigit()):
                return False
        if c == "." and self.peek(1).isdigit():
            return False
        return True

    def string(self) -> str:
        c = self.peek()
        if c == '"':
            return self.quoted()
        if c == "#" and self._is_raw_start():
            return self.raw()
        if not self._is_ident_start():
            raise self.err("expected a string")
        start = self.i
        while not self.eof():
            ch = self.peek()
            if ch in _WS or ch in _NL_CHARS or ch in _NON_IDENT:
                break
            self.i += 1
        word = self.s[start:self.i]
        if word in _BARE_FORBIDDEN:
            raise self.err(f"bare '{word}' is not allowed; use #{word} or \"{word}\"", start)
        return word

    def quoted(self) -> str:
        start = self.i
        if self.startswith('"""'):
            self.i += 3
            if not self.newline():
                raise self.err('multi-line string must start with a newline after """', start)
            body_start = self.i
            end = self.s.find('"""', self.i)
            while end != -1 and self._escaped(end):
                end = self.s.find('"""', end + 1)
            if end == -1:
                raise self.err("unterminated multi-line string", start)
            raw = self.s[body_start:end]
            self.i = end + 3
            return self._unescape(self._dedent(raw, start), start)
        self.i += 1
        out: list[str] = []
        while True:
            if self.eof():
                raise self.err("unterminated string", start)
            ch = self.peek()
            if ch in _NL_CHARS:
                raise self.err('unterminated string (use """ for multi-line strings)', start)
            if ch == '"':
                self.i += 1
                return "".join(out)
            if ch == "\\":
                out.append(self._escape(start))
                continue
            out.append(ch)
            self.i += 1

    def _escaped(self, pos: int) -> bool:
        k = pos - 1
        count = 0
        while k >= 0 and self.s[k] == "\\":
            count += 1
            k -= 1
        return count % 2 == 1

    def _escape(self, start: int) -> str:
        self.i += 1
        e = self.peek()
        if e in _ESCAPES:
            self.i += 1
            return _ESCAPES[e]
        if e == "u":
            m = re.match(r"u\{([0-9a-fA-F]{1,6})\}", self.s[self.i:])
            if not m:
                raise self.err("invalid unicode escape")
            self.i += len(m.group(0))
            return chr(int(m.group(1), 16))
        if e and (e in _WS or e in _NL_CHARS):
            while not self.eof() and (self.peek() in _WS or self.peek() in _NL_CHARS):
                self.i += 1
            return ""
        raise self.err(f"invalid escape '\\{e}'")

    def _unescape(self, text: str, start: int) -> str:
        sub = _Parser.__new__(_Parser)
        sub.s, sub.n, sub.i, sub.file = text, len(text), 0, self.file
        sub._line_starts = [0]
        out: list[str] = []
        while sub.i < sub.n:
            if sub.s[sub.i] == "\\":
                out.append(sub._escape(start))
            else:
                out.append(sub.s[sub.i])
                sub.i += 1
        return "".join(out)

    def _dedent(self, raw: str, start: int) -> str:
        lines = re.split(r"\r\n|[\r\n\u0085\u000b\u000c\u2028\u2029]", raw)
        last = lines[-1]
        if any(ch not in _WS for ch in last):
            raise self.err('closing """ must be on its own line', start)
        prefix = last
        body = lines[:-1]
        out = []
        for ln in body:
            if all(ch in _WS for ch in ln):
                out.append("")
            elif ln.startswith(prefix):
                out.append(ln[len(prefix):])
            else:
                raise self.err("multi-line string line is less indented than closing \"\"\"", start)
        return "\n".join(out)

    def raw(self) -> str:
        start = self.i
        hashes = 0
        while self.peek() == "#":
            hashes += 1
            self.i += 1
        closer = '"' + "#" * hashes
        if self.startswith('"""'):
            self.i += 3
            if not self.newline():
                raise self.err('multi-line raw string must start with a newline', start)
            end = self.s.find('"""' + "#" * hashes, self.i)
            if end == -1:
                raise self.err("unterminated raw string", start)
            raw = self.s[self.i:end]
            self.i = end + 3 + hashes
            return self._dedent(raw, start)
        self.i += 1
        end = self.s.find(closer, self.i)
        if end == -1:
            raise self.err("unterminated raw string", start)
        text = self.s[self.i:end]
        if any(ch in _NL_CHARS for ch in text):
            raise self.err("unterminated raw string", start)
        self.i = end + len(closer)
        return text


def parse(text: str, filename: str = "<string>") -> list[Node]:
    """Parse KDL v2 text into a list of top-level nodes. Raises KdlError."""
    return _Parser(text, filename).document()
