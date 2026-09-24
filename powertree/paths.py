"""Hierarchical paths and instance selectors.

Paths are dot-separated: BOARD.CHIP.function, e.g. "IO:3.U10.vdd".
Counted instances are REF:k. A selector segment may be REF:*, REF:k or REF:a..b.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"(?<!\.)\.(?!\.)")   # a single dot; ".." belongs to a range selector
_SEL = re.compile(r"^(?P<base>[^:]+):(?P<sel>\*|\d+\.\.\d+|\d+)$")


def split(path: str) -> list[str]:
    return _SPLIT.split(path)


def is_selector(pattern: str) -> bool:
    return any(_SEL.match(s) and (":*" in s or ".." in s) for s in split(pattern))


def seg_match(p: str, s: str) -> bool:
    if p == s:
        return True
    m = _SEL.match(p)
    if not m or not s.startswith(m["base"] + ":"):
        return False
    idx = s[len(m["base"]) + 1:]
    if not idx.isdigit():
        return False
    sel = m["sel"]
    if sel == "*":
        return True
    if ".." in sel:
        a, b = map(int, sel.split(".."))
        return a <= int(idx) <= b
    return int(idx) == int(sel)


def match(pattern: str, path: str) -> bool:
    ps, ss = split(pattern), path.split(".")
    return len(ps) == len(ss) and all(seg_match(a, b) for a, b in zip(ps, ss))


def match_prefix(pattern: str, path: str) -> bool:
    """True if pattern matches path or any ancestor of path."""
    ps, ss = split(pattern), path.split(".")
    return len(ps) <= len(ss) and all(seg_match(a, b) for a, b in zip(ps, ss))


def group_key(path: str) -> str:
    """'IO:3.D1:12' -> 'IO:*.D1:*'"""
    return ".".join(re.sub(r":\d+$", ":*", s) for s in path.split("."))
