# microwave_control

GPIB/HP-IB drivers for the microwave sweep setup: HP 8673H synthesized signal
generator sweeping into an HP/Agilent E4403B spectrum analyzer, connected
through a coupler. Goal: sweep the generator's CW frequency and read back the
resulting spectrum on the analyzer.

## Hardware

- **HP 8673H** synthesized signal generator (0.01-26.5 GHz family member;
  8673A/B/C/D/E/G/H all share the same digital/HP-IB architecture, just
  different band coverage).
- **HP/Agilent E4403B** ESA-E series spectrum analyzer (SCPI).
- Two separate GPIB-USB adapters, currently on a shared USB hub. **GPIB bus
  numbers are not stable** -- when adapters are re-plugged or the hub is
  power-cycled, the generator has shown up as both `GPIB0::19::INSTR` and
  `GPIB1::19::INSTR`. Always confirm with:
  ```python
  import pyvisa
  pyvisa.ResourceManager().list_resources()
  ```
  before assuming an address. The E4403B's address (`18`) has stayed fixed on
  `GPIB0` so far.
- **Always keep generator output power low** (we've used -20 dBm) whenever
  its output is connected into the analyzer input through the coupler --
  even with a load/coupler in place, high output power risks reflections and
  overloading the analyzer's front end.

## Files

- `hp8673h.py` -- `HP8673H` driver class.
- `e4403b.py` -- `E4403B` driver class.
- `frequency_sweep.py` -- sweep helpers that combine the two:
  - `frequency_sweep(gen, sa, start_hz, stop_hz, step_hz, power_dbm=-20)`:
    steps the generator's CW frequency, reads a single marker amplitude off
    the analyzer at each point. Good for fine-grained sweeps (kHz-MHz steps)
    where a single number per point is enough.
  - `frequency_sweep_full_trace(gen, sa, start_hz, stop_hz, step_hz, power_dbm=-20, rbw_hz=None)`:
    steps the generator, and at each step retunes the analyzer to center on
    that frequency with span = step_hz (so adjacent windows tile with no
    gaps/overlap) and captures the analyzer's *full* trace. Use for coarser
    steps where you want to see the tone's skirt, spurs, and surrounding
    noise floor, not just the peak value.
  - `concatenate_traces(traces)`: flattens/sorts a list of
    `(freqs_hz, power_dbm)` window traces from `frequency_sweep_full_trace`
    into one continuous array for plotting. This does NOT average or merge
    overlapping data -- each window is an independent, sequential
    measurement; concatenation just lays them side by side in frequency
    order.
- `visa_retry.py` -- `call_with_reconnect(obj, fn)`: wraps a pyvisa call, and
  on `VI_ERROR_CONN_LOST` specifically (seen intermittently, likely from the
  two GPIB-USB adapters sharing a USB hub) reopens `obj.inst` and retries up
  to 3 times. Both driver classes' `write`/`query`/`get_trace` go through
  this already -- no need to wrap calls yourself.

## HP8673H: HP-IB command set

Pre-SCPI instrument -- commands are short ASCII mnemonics (e.g. `FR3000000000HZ`
sets CW frequency to 3 GHz), not SCPI strings. Full command table is Table 3-7
in the HP 8673C/D Operating and Service Manual (same codes apply across the
whole 8673 family). Key ones the driver uses:

| Code | Meaning |
|---|---|
| `IP` | Instrument preset |
| `FR<n>HZ` | Set CW frequency |
| `FI<n>HZ` | Set FREQ INCR step size |
| `PL<n>DB` | Set output power level |
| `RF1` / `RF0` | RF output on/off |
| `K1` / `K0` | AUTO PEAK on/off |
| `FA<n>HZ` / `FB<n>HZ` | Sweep START / STOP frequency |
| `<n>SS` | Number of sweep steps |
| `<n>DWMS` | Dwell time (ms) per sweep step |
| `W2` | Start repetitive AUTO sweep |
| `W6` | Arm + begin SINGLE sweep |
| `W0` | Sweep off |
| `OA` | Output Active Parameter (address to talk, then read back current setting) |

Readback via `read_active_parameter()` returns a raw learn-string like
`CF03000000000HZ` (code + zero-padded value + unit), not a bare number --
parse accordingly.

**Empirically measured**: true frequency resolution between 2-4 GHz on this
specific unit is a clean 1 kHz (matches the manual's spec table for the
0.05-6.6 GHz band) -- anything requested below 1 kHz gets floored to the
nearest 1 kHz.

## E4403B: SCPI gotchas found by testing

- Trace averaging is `AVER:STATE ON` + `AVER:COUNT n`, **not**
  `DISP:WIND:TRAC1:MODE AVER` (that command doesn't exist on this firmware --
  returns `-113 Undefined header`). `TRAC1:MODE` only accepts
  `WRITE|MAXHOLD|MINHOLD|VIEW|BLANK`.
- If you ever set `TRAC1:MODE BLANK`, subsequent `TRACE:DATA?` reads return a
  flat sentinel value (`400.0` at every point) until you set the mode back to
  `WRITE`. This looks like a driver bug but is actually a leftover instrument
  state -- always `TRAC1:MODE WRITE` before reading a trace if unsure.
- Binary trace transfer needs `FORM REAL,32` + `FORM:BORD SWAP` set once
  after connecting (done automatically in `E4403B.__init__`).

**Measured noise floor** (generator RF off, so only the analyzer's own
noise): **-64.2 dBm median** (range -67.8 to -60.4 dBm) at the settings used
during that test -- 1 MHz RBW, 10 dB input attenuation, no averaging. This is
a real reading at those specific settings, not a datasheet spec -- narrower
RBW and/or lower attenuation will push it lower (better). Re-measure if you
change either setting.

## Data and notebooks

- All sweep results live in `data/*.csv` (two columns: `frequency_hz,power_dbm`).
  Every notebook loads/saves there, not the top-level directory.
- `frequency_sweep_demo.ipynb` -- first fine sweep, 2.5-3.0 GHz, 1 MHz steps.
- `full_trace_sweep_demo.ipynb` -- noise floor measurement (RF off) + coarse
  100 MHz-step sweep capturing full analyzer traces per step.
- `resonance_sweep_demo.ipynb` -- coarse (2-3 GHz, 1 MHz) + fine (±5 MHz,
  20 kHz) sweep that found the confirmed resonance at 2.0443 GHz. This was
  taken with the analyzer on the coupler's REVERSE (reflected) port.
- `resonance_2.3ghz_demo.ipynb` / `resonance_2.3ghz_demo_repeat.ipynb` --
  checked a coarse dip that appeared near 2.3 GHz; two independent fine
  sweeps of the same region showed zero correlation with each other
  (dip location moved, values uncorrelated) -- confirmed to be noise, not a
  real feature. Worth remembering as a cautionary example: always check
  repeatability before trusting a single sweep's dip/peak.
- `plot_real_resonance.ipynb` -- re-plots the confirmed 2.0443 GHz resonance
  from already-saved data, no hardware involved.
- `isolator_comparison_demo.ipynb` -- forward-port sweeps (2-3 GHz, 1 MHz)
  with and without an isolator before the coupler, overlaid against the
  original baseline. Found roughly 1.4 dB mean difference with vs. without
  the isolator -- small, within run-to-run variation seen elsewhere.

**Caution**: several of these notebooks have cells that open real hardware
connections and run multi-minute sweeps. Do NOT `jupyter nbconvert --execute`
a notebook like `isolator_comparison_demo.ipynb` just to refresh a plot from
already-saved data -- it re-runs every cell top to bottom, including the live
sweep cells. To regenerate a plot/analysis cell from existing CSVs without
touching the instruments, either run just that cell interactively, or patch
the `.ipynb` JSON directly (set `cell["outputs"]` by matching cell content,
not by the `cell-N` label the Read tool displays -- notebooks written by hand
via the Write tool don't necessarily have real `id` fields, so `cell-N` is
just a display placeholder, not something you can match on in the JSON).

To run notebooks with the project's dependencies: they use a Jupyter kernel
named `diamonds` (registered via `python -m ipykernel install --user --name
diamonds` from within the `diamonds` conda env). From the command line:
```
jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.kernel_name=diamonds <notebook>.ipynb
```

## Typical usage

```python
from hp8673h import HP8673H
from e4403b import E4403B
from frequency_sweep import frequency_sweep, frequency_sweep_full_trace, concatenate_traces

gen = HP8673H("GPIB1::19::INSTR")
sa = E4403B("GPIB0::18::INSTR")

# fine sweep, single marker reading per point
freqs_hz, power_dbm = frequency_sweep(gen, sa, 2.5e9, 3.0e9, 1e6, power_dbm=-20)

# coarse sweep, full trace per step
traces = frequency_sweep_full_trace(gen, sa, 2.5e9, 3.0e9, 100e6, power_dbm=-20)
freqs_hz, power_dbm = concatenate_traces(traces)

gen.close()
sa.close()
```
