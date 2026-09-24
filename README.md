# powertree

Plain-text PCB power tree description (KDL v2), with validation, a steady-state DC solver, per-chip heat, design-rule checks, scenarios, and Graphviz output. Pure Python 3.10+, no runtime dependencies.

```sh
pip install -e .                     # or: python -m powertree ...
powertree check  examples/sensor-board.kdl          # findings only; exit 1 on errors (--strict: warnings too)
powertree report examples/sensor-board.kdl -s peak  # nets, functions, chip heat, totals, findings
powertree report examples/ups.kdl --json            # machine-readable
powertree dot    examples/ups.kdl -s on-battery -o ups.svg   # needs Graphviz for svg/png/pdf
powertree check  examples/rack/rack.kdl --lib corp=examples/lib   # boards, counts, library roots
powertree schema                                    # every node/property, generated from the validator
```

- `SPEC.md` — the format reference and solver semantics
- `examples/`
  - `sensor-board.kdl` — 12V → 5V → LDO/buck, part library
  - `ups.kdl` — adapter + battery, OR-ing, charger, harness, waiver
  - `rack/` — project file with a library root, a PSU feeding 4 counted `io-card` boards, board scenarios
- `tests/` — `pytest` (91 tests, including a parity check against the `ckdl` reference parser when it is installed)

## Code layout

| Module | Layer |
|---|---|
| `kdl.py` | KDL v2 parser with line/column positions |
| `schema.py` | declarative structure spec + validator (layer 2) |
| `loader.py` | `use` files, part merge, counts, board flattening, reference resolution (layer 3) |
| `libs.py`, `paths.py` | library roots and stamps; hierarchical paths and selectors |
| `topology.py` | ERC-style graph rules, topological order (layer 4) |
| `scenario.py` | scenario inheritance |
| `solver.py` | DC solve and electrical checks (layer 5) |
| `units.py`, `expr.py` | quantities and the safe efficiency-expression language |
| `analysis.py`, `report.py`, `cli.py` | pipeline runner, waivers, text/JSON/DOT output |
