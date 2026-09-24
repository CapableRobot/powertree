# powertree file format — v0.1

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
use "<path>"                   // load part definitions; path relative to this file
net <NAME> [desc=]             // named bus; every net= must refer to a declared net
part <PART> { functions... }   // reusable definition (any file)
chip <REF> [part=] [desc=] { functions... }
scenario <name> [base="a, b"] [desc=] { loads nom|min|max; set <target> prop=value... }
rules { converter-load max=; provider-load max=; switch-load max=; input-headroom min= }
waive <code> <target> reason="..."
```

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

`on=#false` on any function turns it off. This is usually set per scenario.

### Accepted input voltage

`range="a..b"` on an input, optionally overridden by `vmin=`/`vmax=`. On a consumer, `v="3.3V ±5%"` also defines the accepted range. A `v=` with no tolerance and no `range=` is documentation only and is not checked.

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

If no scenario is defined, a single implicit `nominal` scenario runs.

## Rules and waivers

| Rule | Default | Finding |
|---|---|---|
| `converter-load max=` | 80% | warning when Iout/imax exceeds it; above 100% is always an `overload` error |
| `provider-load max=` | 80% | same, for providers |
| `switch-load max=` | 80% | same, for switch, series and oring |
| `input-headroom min=` | 0 (off) | warning when the supplied range is within this fraction of an input's accepted limits |

`waive <code> <target> reason=` accepts matching findings. The target may be a chip, a function or a net; a chip target covers all its functions. Waived findings are still listed, marked as waived. A waiver that matches nothing produces an `unused-waiver` warning.

## Pipeline and finding codes

Each layer runs only if the previous layers produced no errors, and reports all errors in its own layer.

1. **Syntax:** `syntax`
2. **Structure:** `unknown-node`, `unknown-property`, `bad-value`, `argument-count`, `missing-property`, `unexpected-children`, `duplicate-property`
3. **Model and references:**
   - Loading: `file-not-found`, `duplicate-part`, `kind-mismatch`
   - Function definitions: `port-count`, `missing-voltage`, `missing-efficiency`, `missing-load`, `load-model`, `efficiency`, `setpoint-range`
   - Duplicates: `duplicate-net`, `duplicate-chip`, `duplicate-function`, `duplicate-scenario`
   - References: `undefined-net`, `bad-reference`, `conflicting-link`, `undefined-scenario`, `scenario-cycle`
4. **Topology:** `unconnected-input`, `undriven-net`, `multiple-drivers`, `cycle`, and warnings `unused-net`, `unloaded-net`, `unconnected-output`
5. **Electrical, per scenario:**
   - Loading: `overload`, `converter-load`, `provider-load`, `switch-load`
   - Input voltage: `input-range`, `input-headroom`
   - Regulation: `dropout`, `dropout-margin`, `converter-kind`, `eff-extrapolated`, `efficiency`
   - Solver: `collapse`, `no-convergence`
   - State: `unpowered` (info)

## Solver

The solver computes a steady-state DC solution per scenario:

1. **Forward pass** in topological order. This computes each net's nominal voltage and its min/max range:
   - Providers use their spec minus I·r.
   - Switching converters use their setpoint.
   - Linear regulators use min(setpoint, V_in − dropout(I)), with dropout scaled linearly from `at=`.
   - Switches and series elements subtract I·R.
   - OR-ing picks the lowest `priority` among powered inputs, otherwise the highest voltage, then subtracts vf + I·r.
2. **Backward pass** in reverse order. This computes currents from loads up to sources. A net with several `share=#true` drivers splits current equally. Otherwise, the highest-voltage driver supplies the net.
3. **Iterate** both passes until voltages and currents stop changing. This handles constant-power loads behind resistive drops.

Currents are nominal. Voltage ranges are propagated using those currents.

**Heat per chip** is the sum of its functions' heat:
- consumer: input power, unless `offboard`
- converter, switch, series, oring: P_in − P_out
- provider: I²·r

## Not yet implemented (planned)

- `count=` and `{n}` net templates
- hierarchical `board` instances and ports
- named library roots (`corp:regulators.kdl`)
- KiCad netlist mode
- worst-case current solve
- battery state-of-charge and runtime
- time-based behavior
