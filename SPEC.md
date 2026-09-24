# powertree file format — v0.2

A power design is a KDL v2 document. `powertree schema` prints the full list of nodes and properties generated from the validator, so it is always current; this document explains the meaning.

## Values

All electrical values are quoted strings with units: `"3.3V"`, `"250mA"`, `"45mΩ"` (also `ohm`, `R`), `"1.2W"`. SI prefixes `p n u µ m k M G` are accepted. A KDL type annotation is equivalent: `imax=(mA)250`. Bare numbers are rejected for quantities, so a missing unit is always an error.

| Form | Example | Meaning |
|---|---|---|
| voltage spec | `"5V"`, `"5V ±2%"`, `"5V +-50mV"`, `"4.75V..5.25V"` | nominal and min/max (range form: nominal = midpoint) |
| voltage range | `"3V..5.5V"` | accepted input range, adjustable setpoint range |
| ratio | `0.9`, `"90%"` | efficiency, rule thresholds |
| boolean | `#true`, `#false` | KDL v2 keywords |

## Top-level nodes

```kdl
design <name> [desc=]          // exactly one per design file
use "<path>" | "<lib>:<path>"  // load parts and designs; path relative to this file or a library root
net <NAME> [desc=] [count=]    // named bus; every net= must refer to a declared net
port <PORT> net=<NET> [dir=in|out|bidir]    // this design's interface when used as a board
part <PART> { functions... }   // reusable definition (any file)
chip <REF> [part=] [desc=] [count=] { functions... }
board <REF> design=<name> [count=] [desc=] { port <PORT> net=<NET> ... }
scenario <name> [base="a, b"] [desc=] { loads nom|min|max; set <target> prop=value... }
rules { converter-load max=; provider-load max=; switch-load max=; input-headroom min= }
waive <code> <target> reason="..."
```

References (`CHIP`, `BOARD`) may not contain `.`, `:`, `{` or `}`; those characters are reserved for paths and templates.

## Chips and functions

A chip groups all power functions of one physical component. A PMIC is one chip with several `converter` functions and possibly `consumer` functions for its own supply pins. Function names are optional when a chip has one function of that kind; otherwise name them (`consumer vdd`, `consumer vdda`). Functions are addressed as `CHIP.function`, or just `CHIP` if the chip has a single function.

| Function | Ports | Parameters |
|---|---|---|
| `provider` | `out` (v, imax, share) | `r` internal resistance |
| `converter kind=...` | `in` (range), `out` (v, range, imax, share) | `iq`, `eff`, `dropout` (linear) |
| `switch [enable=SIG]` | `in` (range), `out` (imax) | `rdson` |
| `series` | `in`, `out` (imax) | `r` — fuse, ferrite, sense resistor, connector, cable |
| `oring` | `in` × N (priority), `out` (imax) | `vf`, `r` |
| `consumer` | `in` (v, range) | `load` |

Converter kinds: `buck boost buck-boost flyback isolated charge-pump charger linear`. Kind `linear` uses Iin = Iout + Iq and ignores efficiency. Every other kind requires `eff`. The solver sanity-checks `buck` (vin > vout) and `boost` (vin < vout).

**Implicit ports.** A function with exactly one input or output gets that port implicitly when it is not written. So `series { in net=VIN; r "10mΩ" }` still has an output, which another input can link to with `from=RSENSE.series.out` without naming the net between them. OR-ing inputs are never implicit. An implicit port that nothing links to is reported by the topology rules (`unconnected-input` error, `unconnected-output` warning).

`on=#false` on any function turns it off. This is usually set per scenario.

### Accepted input voltage

`range="a..b"` on an input, optionally overridden by `vmin=`/`vmax=`. On a consumer, `v="3.3V ±5%"` also defines the accepted range. A `v=` with no tolerance and no `range=` is documentation only and is not checked.

### Output limit (`vlim`)

`vlim=` on a converter output gives the highest voltage the converter can produce, as an expression of `vin` and `iout`. Use it for datasheet limits such as "Output Voltage: 0.8V to 0.85 × VIN", a maximum duty cycle, or a pass-element drop:

```kdl
out net=V12 v="12V ±2%" vlim="0.85*vin"
out net=V5  v="5V"      vlim="vin - 0.2Ω*iout"
```

The output is the lower of the setpoint and the limit, both for the nominal value and for the min/max range. It is usually a part property, so it belongs in the library definition.

Two findings report the limit:
- `output-limited` (error): the converter cannot reach its setpoint at the nominal input.
- `output-limit-margin` (warning): it regulates at nominal input but not at the minimum input.

The result must be a voltage, so every constant needs a unit: `max(0.8V, 0.85*vin)`, not `max(0.8, 0.85*vin)`.

### Efficiency

```kdl
eff 0.9                                    // constant
eff "92%"
eff expr="0.94 - 0.03 * (iout / 750mA)"    // variables vin, vout, iout
eff vin="12V" vout="5V" {                  // datasheet curves; several allowed
    point iout="10mA" eff=0.62
    point iout="1A"   eff=0.91
}
```

Expressions are restricted and unit-aware. They allow `+ - * / **`, unit literals, and the functions `min max abs sqrt exp log log10 clamp`. The result must be dimensionless. `t` and `temp` are reserved for future time and temperature support.

Tables are interpolated linearly in log(iout), and linearly in vin between curves. The curve set with the nearest `vout` is used. Outside the data the value is clamped and a finding is reported: `info` below the lowest current, `warning` above the highest current or more than 10% outside the vin/vout covered.

Input power is P_in = P_out / η + V_in · I_q.

### Loads

```kdl
load i="35mA" imin="15uA" imax="90mA"     // constant current
load p="50mW" pmin="1mW" pmax="80mW"      // constant power (current rises as voltage falls)
load r="100Ω"                             // resistive
load i="100mA" offboard=#true             // power leaves the board: not counted as heat
```

A missing min or max falls back to the nominal value.

### Linking

`in net=NAME` connects a port to a named net.

`in from=CHIP[.function[.out]]` links directly to an output:
- If that output is on a named net, the input joins that net.
- Otherwise an anonymous net `~CHIP.function.out` is created.
- Giving both `net=` and `from=` is allowed only if they agree.

### Parts

`chip U1 part=TPS54331 { converter buck { in net=A; out net=B v="5V" } }` merges the instance onto the part definition with the same function name:
- Instance properties override library properties.
- `in`/`out` ports merge by position.
- Any other child (`eff`, `iq`, …) given on the instance replaces the library's.

If `part=` names no loaded part, it is metadata only, and the chip body must be complete. Defining the same part name twice is an error.

## Counts and templates

`count=N` on a `chip`, `board` or `net` creates N instances. Chips and boards are named `REF:1` … `REF:N`. Inside a counted node, `{n}` in `net=`, `from=` and `desc=` is replaced by the instance number. A counted net needs `{n}` in its name. Values containing `{n}` must be quoted, because braces are not allowed in bare KDL strings:

```kdl
net "IO{n}_24V" count=8
chip F count=8 desc="Slot {n} fuse" { series { in net=BUS; out net="IO{n}_24V"; r "40mΩ" } }
chip D count=12 { consumer led { in net=V5; load i="5mA" } }     // D:1 .. D:12, all on V5
```

## Hierarchy: ports and boards

Any design file can be instantiated as a board. It declares its interface with `port` nodes, each naming one of its own nets:

```kdl
// io-card.kdl
design io-card
port VIN net=VIN_24V dir=in
net VIN_24V
...
scenario run
scenario idle { loads min; set K:* on=#false }
```

The parent `use`s the file and instantiates it, binding every port to one of its own nets. An unbound port is an error.

```kdl
use "io-card.kdl"
board IO count=8 design=io-card { port VIN net="IO{n}_24V" }
```

**How a board is flattened into the parent:**
- Chips are prefixed with the instance path, so U3 on the second card becomes `IO:2.U3` and its function becomes `IO:2.U3.buck`.
- Internal nets are prefixed too (`IO:2.V5`), except port nets, which become the parent's net.
- Boards nest to any depth. A design that instantiates itself, directly or indirectly, is an error.
- The board's `waive` nodes come along, prefixed with the instance path.
- The board's `rules` are ignored; the top-level design's rules apply.
- The board's scenarios are available to the parent through `set <board> scenario=<name>`.

### Analysing a board on its own

With no source on its input ports, a board analysed on its own reports the port nets as undriven. A `standalone` block fixes this. Its contents are used only when the file is the top-level design, and are dropped entirely when the file is instantiated as a board, so they never add a second driver to the parent's net:

```kdl
standalone {
    chip TP1 desc="Bench supply on VIN" { provider { out net=VIN_24V v="24V ±3%" imax="1A" } }
    scenario low-line { set TP1 v="21.6V ±3%" }
}
```

A `standalone` block may contain `net`, `chip`, `board`, `scenario`, `rules` and `waive`. Scenarios that refer to standalone chips belong inside it; otherwise they would be unresolved references when the file is used as a board. The report lists what the block added (info `standalone`).

`dir=` is informational in this version.

## Paths and selectors

A path is dot-separated: `BOARD.CHIP.function`, for example `SHELF.IO:3.U10.vdd`. A segment of a path can select several instances:

| Selector | Matches |
|---|---|
| `REF:3` | instance 3 |
| `REF:*` | all instances |
| `REF:2..5` | instances 2 to 5 |

Selectors work in `set` and `waive` targets, e.g. `set IO:*.D:* on=#false` or `waive overload IO:1..4.U3 reason=...`.

## Library roots

Named roots let design files refer to shared libraries without machine-specific paths. They are set in `powertree-project.kdl`, found by searching upward from the design file:

```kdl
library corp path="../corp-power-lib"      // relative to the project file
```

Each later source overrides earlier ones:
1. the project file
2. `$POWERTREE_LIBS` (`name=path` entries separated by `:` on Linux/macOS, `;` on Windows)
3. `--lib name=path` on the command line

**Using a root:**
- `use "corp:regulators.kdl"` loads a file from the root.
- Relative `use` paths inside a library file stay in that library.
- Parts and designs are namespaced by library. `part=corp:TPS54331` is always exact.
- An unqualified `part=TPS54331` must be unique across all loaded files; otherwise it is an `ambiguous-part` error. The same rule applies to `design=`.

**Traceability:** every report records each root used, with its path, its origin, a SHA-256 content hash of the files actually loaded, and the git commit (marked `+dirty` if there are uncommitted changes).

## Scenarios

```kdl
scenario active
scenario peak base=active { loads max }
scenario night base="active, low-power" {
    set U4.sw on=#false
    set U10.vdd i="15uA"
    set J1 v="10.8V"
}
```

**Resolution order:** defaults → each base in listed order, recursively → the scenario's own `loads` and `set` lines. Later settings win.

**Consumer load models:** setting one of `i`/`p`/`r` replaces any load model set earlier.

**What `set` can change:**
- any function: `on`
- consumer: `i`, `p`, `r` (overrides the load level for that consumer)
- provider: `v`, `imax`
- converter: `v`
- chip with several functions: `on` only, applied to all its functions
- board: `scenario=<name>`, which applies the board's own scenario scoped to that board, and `on`, which turns off everything on the board

**Load levels are scoped.** A board scenario's `loads` applies only inside that board. Otherwise `loads` lines apply in order, and the last one covering a function wins. So in `scenario s { loads max; set IO:2 scenario=idle }`, board IO:2 runs at its idle level and everything else at max.

If no scenario is defined, a single implicit `nominal` scenario runs.

**Composing on the command line.** `-s a+b` runs one scenario made of `a` then `b`, as if you had written `scenario a+b base="a, b"`. Repeating `-s` runs separate scenarios: `-s peak -s low-line` produces two reports. A scenario actually named `a+b` takes precedence over composition.

## Rules and waivers

| Rule | Default | Finding |
|---|---|---|
| `converter-load max=` | 80% | warning when Iout/imax exceeds it; above 100% is always an `overload` error |
| `provider-load max=` | 80% | same, for providers |
| `switch-load max=` | 80% | same, for switch, series and oring |
| `input-headroom min=` | 0 (off) | warning when the supplied range is within this fraction of an input's accepted limits |

`waive <code> <target> reason=` accepts matching findings. The target may be a board, chip, function or net, or a selector; it covers everything beneath it. Waived findings are still listed, marked as waived. A waiver that matches nothing produces an `unused-waiver` warning.

## Pipeline and finding codes

Each layer runs only if the previous layers produced no errors, and reports all errors in its own layer.

1. **Syntax:** `syntax`
2. **Structure:** `unknown-node`, `unknown-property`, `bad-value`, `argument-count`, `missing-property`, `unexpected-children`, `duplicate-property`
3. **Model and references:**
   - Loading: `file-not-found`, `library`, `duplicate-part`, `duplicate-design`, `ambiguous-part`, `ambiguous-design`, `unknown-design`, `kind-mismatch`, and the warning `unknown-part` for a near-miss part name
   - Hierarchy: `bad-name`, `template`, `unknown-port`, `unbound-port`, `duplicate-port`, `duplicate-board`, `board-cycle`, and info `ignored-rules`, `standalone`
   - Function definitions: `port-count`, `missing-voltage`, `missing-efficiency`, `missing-load`, `load-model`, `efficiency`, `setpoint-range`
   - Duplicates: `duplicate-net`, `duplicate-chip`, `duplicate-function`, `duplicate-scenario`
   - References: `undefined-net`, `bad-reference`, `conflicting-link`, `undefined-scenario`, `scenario-cycle`
4. **Topology:** `unconnected-input`, `undriven-net`, `multiple-drivers`, `cycle`, and warnings `unused-net`, `unloaded-net`, `unconnected-output`
5. **Electrical, per scenario:**
   - Loading: `overload`, `converter-load`, `provider-load`, `switch-load`
   - Input voltage: `input-range`, `input-headroom`
   - Regulation: `dropout`, `dropout-margin`, `output-limited`, `output-limit-margin`, `converter-kind`, `eff-extrapolated`, `efficiency`, `vlim`
   - Solver: `collapse`, `no-convergence`
   - State: `unpowered` (info)

## Solver

The solver computes a steady-state DC solution per scenario:

1. **Forward pass** in topological order. This computes each net's nominal voltage and its min/max range:
   - Providers use their spec minus I·r.
   - Switching converters use their setpoint, capped by `vlim` when given.
   - Linear regulators use min(setpoint, V_in − dropout(I)), with dropout scaled linearly from `at=`.
   - Switches and series elements subtract I·R.
   - OR-ing picks the lowest `priority` among powered inputs, otherwise the highest voltage, then subtracts vf + I·r.
2. **Backward pass** in reverse order. This computes currents from loads up to sources. A net with several `share=#true` drivers splits current equally. Otherwise, the highest-voltage driver supplies the net.
3. **Iterate** both passes until voltages and currents stop changing. This handles constant-power loads behind resistive drops.

Currents are nominal. Voltage ranges are propagated using those currents.

## Report power summary

| Row | Definition |
|---|---|
| Source output | Σ provider output power: V_out · I_out, after the provider's internal resistance |
| Converter / Switch / Series / OR-ing loss | Σ (P_in − P_out) over functions of that kind |
| On-board consumers | Σ P_in of consumers without `offboard` |
| External consumers | Σ P_in of consumers with `offboard=#true` |
| Source internal loss | Σ I²·r inside providers; heat, but not part of source output |
| Heat on board | all losses + on-board consumers + source internal loss |
| System efficiency | (on-board + external consumers) / source output |

The rows from Converter loss through External consumers sum to Source output.

**Heat per chip** is the sum of its functions' heat:
- consumer: input power, unless `offboard`
- converter, switch, series, oring: P_in − P_out
- provider: I²·r

## Not yet implemented (planned)

- KiCad netlist mode
- `offboard` for series elements, so cable losses are not counted as board heat
- worst-case current solve
- battery state-of-charge and runtime
- time-based behavior
