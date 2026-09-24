"""Command line: powertree check|report|dot|schema."""
from __future__ import annotations

import argparse
import os
import sys

from .analysis import analyze
from .report import findings_text, scenario_text, to_dot, to_json
from .render import RenderError, render
from .schema import schema_reference


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="powertree", description="PCB power tree analysis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate and run all scenarios; print findings only")
    c.add_argument("file")
    c.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")

    r = sub.add_parser("report", help="solve scenarios and print results")
    r.add_argument("file")
    r.add_argument("-s", "--scenario", action="append", help="scenario(s) to run (default: all)")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    r.add_argument("--summary", action="store_true",
                   help="functions except consumers, plus the Board, Group and Power tables")

    g = sub.add_parser("dot", help="write a Graphviz graph for one scenario")
    g.add_argument("file")
    g.add_argument("-s", "--scenario", help="scenario (default: first)")
    g.add_argument("-o", "--output", help="output file; format from extension: .dot .svg .png .pdf ... "
                                          "(default: DOT on stdout)")
    g.add_argument("-T", "--format", dest="fmt", help="output format if it differs from the extension")
    g.add_argument("--dpi", type=int, default=200, help="resolution for raster formats like PNG (default 200)")
    g.add_argument("--dot", help="path to Graphviz dot(.exe) (default: $POWERTREE_DOT, then PATH)")

    for sp in (c, r, g):
        sp.add_argument("--lib", action="append", metavar="NAME=PATH",
                        help="library root (overrides project file and $POWERTREE_LIBS)")

    sub.add_parser("schema", help="print the reference of all nodes and properties")

    a = ap.parse_args(argv)
    if a.cmd == "schema":
        print(schema_reference())
        return 0

    scen = a.scenario if a.cmd == "report" else ([a.scenario] if getattr(a, "scenario", None) else None)
    an = analyze(a.file, scen, a.lib)

    if a.cmd == "report" and a.json:
        print(to_json(an))
        return 0 if an.ok else 1

    if a.cmd == "check" and an.design is not None:
        from .report import libraries_text
        for line in libraries_text(an):
            print(line)
    if a.cmd == "report" and an.design is not None:
        for res in an.results.values():
            print(scenario_text(an, res, summary=a.summary))
    if a.cmd == "dot" and an.results:
        name = a.scenario or next(iter(an.results))
        dot = to_dot(an, name)
        if not a.output:
            print(dot)
        else:
            ext = (a.fmt or os.path.splitext(a.output)[1].lstrip(".")).lower()
            if ext in ("", "dot", "gv"):
                with open(a.output, "w", encoding="utf-8") as fh:
                    fh.write(dot)
            else:
                try:
                    render(dot, a.output, ext, a.dpi, a.dot)
                except RenderError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 2
            print(f"wrote {a.output}", file=sys.stderr)

    print(findings_text(an.diags), file=sys.stderr if a.cmd == "dot" else sys.stdout)
    if not an.ok:
        return 1
    if a.cmd == "check" and a.strict and any(f.severity == "warning" and not f.waived for f in an.diags.items):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
