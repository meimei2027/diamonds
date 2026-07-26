# Instrument gotchas

Quick reference for things that aren't obvious from the driver code alone.
See `CLAUDE.md` for the full investigation history behind each of these.

## HP8673H signal generator

- **Pre-SCPI, mnemonic ASCII commands** (e.g. `FR3000000000HZ`), not SCPI
  strings. Full command table is Table 3-7 in the HP 8673C/D manual (same
  codes across the whole 8673A/B/C/D/E/G/H family).
- **Frequency resolution is 1 kHz** between 2-4 GHz on our unit (matches the
  manual's spec table). Anything finer gets silently floored.
- **Sweep-start settling**: the first point of any sweep involves a large
  jump from wherever the generator was previously sitting. AUTO PEAK
  re-leveling triggers automatically on frequency changes >50 MHz and takes
  much longer to settle than normal step-to-step timing. Without an extra
  delay after that initial jump, the first few points of a sweep read
  anomalously low -- easy to mistake for a real resonance dip, especially
  since it appears at whatever frequency the sweep happens to start at.
  `frequency_sweep()` handles this via `initial_settle_s` (default 1.0 s,
  applied once before the per-step loop begins) -- don't set it to 0 unless
  you really know what you're doing.
- **GPIB bus number isn't stable.** Has shown up as both `GPIB0::19::INSTR`
  and `GPIB1::19::INSTR` depending on adapter enumeration order. Always
  `pyvisa.ResourceManager().list_resources()` before assuming an address.

## E4403B spectrum analyzer

- **Trace averaging** is `AVER:STATE ON` + `AVER:COUNT n`, not
  `DISP:WIND:TRAC1:MODE AVER` (undefined header on this firmware).
  `TRAC1:MODE` only accepts `WRITE|MAXHOLD|MINHOLD|VIEW|BLANK`.
- **`TRAC1:MODE BLANK` poisons subsequent reads**: `TRACE:DATA?` returns a
  flat sentinel (`400.0` everywhere) until you set the mode back to
  `WRITE`. Looks like a driver bug, is actually leftover instrument state.
- **Amplitude calibration can genuinely drift out of spec.** We hit a real
  fault where readings were 20-37 dB too high, worse at some frequencies
  than others. All of the instrument's own settings (attenuation, preamp,
  units, offset) checked out normal -- it was a real internal cal fault, not
  a settings mistake. **Fix: front panel Align Now -> RF (not Align Now ->
  All), with a cable connected to the input.** After a real cal fault, don't
  trust *any* absolute dBm reading until this has been done and reverified
  with a known source.
- **Even when calibration is healthy, expect a small residual offset** from
  ordinary cable loss (we consistently see ~-4.4 to -4.5 dB direct
  generator-to-analyzer). This is normal, not a fault -- re-measure if the
  cable changes.
- **Noise floor is RBW/attenuation dependent, and auto-RBW isn't always what
  you expect.** Measured ~-64.3 dBm median at 1 MHz RBW / 10 dB attenuation,
  but the analyzer's auto-coupling picked 3 MHz RBW for the same 500 MHz
  span on one occasion, giving a very different-looking floor. Set `BAND`
  explicitly if you need a result comparable to a previous measurement.
- **"Query UNTERMINATED" / frozen-looking display**: caused by an
  exploratory SCPI query that isn't valid on this firmware timing out on
  the controller side without ever reading the response, leaving the
  instrument's output queue in a pending state. Fix: drain any stale
  response (`inst.read()` in a loop until it raises), then `inst.clear()`
  (GPIB device clear) followed by `*CLS`.

## General

- **Two GPIB-USB adapters share a USB hub**, causing intermittent
  `VI_ERROR_CONN_LOST`. `visa_retry.py`'s `call_with_reconnect()` retries
  automatically (up to 3x) and is already wired into both driver classes'
  `write`/`query`/`get_trace` -- but a hard crash with this error mid-sweep
  is usually just this, and a plain retry of the same sweep works.
- **Always verify a new physical setup before trusting a measurement.**
  Sweep the generator's power over a wide range at a fixed frequency and
  confirm the analyzer's reading actually tracks it, then confirm it drops
  further with RF off. A disconnected/floating analyzer input on this
  E4403B reads a flat, power-independent, RF-state-independent level
  (~-45 to -48 dBm across 2-3 GHz) that looks deceptively like a real, if
  boring, signal.
- **Step size for finding an unknown resonance should track its FWHM, not
  be a fixed default.** Rule of thumb: step <= FWHM/5 to reliably identify
  a dip and get a rough shape; step <= FWHM/2 only guarantees detection,
  not an accurate depth. `resonance_sweep.py`'s default coarse step
  (6.7 MHz) assumes Q ~75 -- retune it if you have reason to expect a much
  higher-Q (narrower) resonance.
- **A coupler's directivity sets a hard floor on reflection measurements.**
  Our ZABDC20-322H-S+ has ~13 dB directivity in this band -- if whatever
  you're measuring reflects less than that relative to a matched load, you
  won't be able to tell it apart from the coupler's own leakage.
