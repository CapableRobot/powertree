import textwrap

from powertree.analysis import analyze


def run(tmp_path, src, scenarios=None, files=None):
    for name, text in (files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    p = tmp_path / "design.kdl"
    p.write_text("design t\n" + textwrap.dedent(src), encoding="utf-8")
    return analyze(str(p), scenarios)


def codes(a, severity=None, include_waived=False):
    return sorted(f.code for f in a.diags.items
                  if (severity is None or f.severity == severity) and (include_waived or not f.waived))
