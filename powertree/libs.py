"""Named library roots: project file, environment and command line."""
from __future__ import annotations

import hashlib
import os
import subprocess

from . import schema
from .diagnostics import Diagnostics
from .kdl import KdlError, parse
from .model import Library

PROJECT_FILE = "powertree-project.kdl"
ENV_VAR = "POWERTREE_LIBS"        # "name=path" entries separated by os.pathsep


def find_project_file(start: str) -> str | None:
    d = os.path.dirname(os.path.abspath(start))
    while True:
        cand = os.path.join(d, PROJECT_FILE)
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _parse_pairs(items, origin, diags) -> dict[str, Library]:
    out = {}
    for item in items:
        if "=" not in item:
            diags.error("library", f"{origin}: expected name=path, got {item!r}")
            continue
        name, path = item.split("=", 1)
        out[name.strip()] = Library(name.strip(), os.path.abspath(os.path.expanduser(path.strip())), origin)
    return out


def library_roots(design_file: str, cli: list[str] | None, diags: Diagnostics) -> dict[str, Library]:
    """Project file < environment < command line (later wins)."""
    roots: dict[str, Library] = {}
    proj = find_project_file(design_file)
    if proj:
        try:
            nodes = parse(open(proj, encoding="utf-8").read(), proj)
        except KdlError as e:
            diags.error("syntax", e.message, e.span)
            nodes = []
        schema.validate(nodes, diags, schema.PROJECT, "project file")
        for n in nodes:
            if n.name == "library" and n.args and "path" in n.props:
                p = os.path.normpath(os.path.join(os.path.dirname(proj), str(n.props["path"].value)))
                roots[str(n.args[0].value)] = Library(str(n.args[0].value), p, f"project file {proj}")
    env = os.environ.get(ENV_VAR)
    if env:
        roots.update(_parse_pairs([e for e in env.split(os.pathsep) if e], f"${ENV_VAR}", diags))
    roots.update(_parse_pairs(cli or [], "--lib", diags))
    for lib in roots.values():
        if not os.path.isdir(lib.path):
            diags.error("library", f"library '{lib.name}' path {lib.path} ({lib.origin}) is not a directory")
    return roots


def stamp(lib: Library) -> None:
    """Record a content hash of the files used, and the git commit if any."""
    h = hashlib.sha256()
    for f in sorted(set(lib.files)):
        h.update(os.path.relpath(f, lib.path).encode())
        with open(f, "rb") as fh:
            h.update(fh.read())
    lib.sha = h.hexdigest()[:12]
    try:
        rev = subprocess.run(["git", "-C", lib.path, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if rev.returncode == 0:
            dirty = subprocess.run(["git", "-C", lib.path, "status", "--porcelain", "--", "."],
                                   capture_output=True, text=True, timeout=5)
            lib.git = rev.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
