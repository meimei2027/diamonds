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

## NanoVNA-F V3 vector network analyzer

- **`recall {slot}` (e.g. `recall 0`) reliably crashed the USB-CDC connection
  on our unit** (firmware `0.6.0`, build Jun 18 2025): the device echoes the
  command, then the port goes completely unresponsive -- Windows reports
  "a device attached to the system is not functioning" on any further
  read/write or even just reconfiguring the port's timeout. This happened
  twice in a row under the same conditions. **A full power cycle (not just
  USB replug) was required to recover both times** -- unplugging/replugging
  the USB cable alone was not enough to bring the port back.
- Root cause untested -- plausible that `recall` triggers a full
  synth/mixer/display reinit that briefly (or permanently, on this
  firmware) drops the USB peripheral. Not root-caused yet; treat any
  `recall`/state-restore command as **crash-risk** until proven otherwise
  on this unit, and don't call it from an unattended script without a
  human present to power-cycle if it hangs.
- The bare `cal` command (no arguments) also does not return to the `ch> `
  prompt in any reasonable time -- it appears to expect a further
  interactive step rather than just printing usage and returning like most
  other commands do here. Always pass a subcommand:
  `cal [load|open|short|thru|done|reset|on|off]` (note: `load` here is the
  *load standard* of an OSL cal, not "load a saved calibration" -- there is
  no such subcommand on `cal`).
- **Calibration and other instrument state save/recall is a separate
  command from `cal`**: `save {id}` / `recall {id}` persist/restore a full
  state slot (frequency plan + cal coefficients + display settings), not
  just the calibration coefficients alone.
- `nanovna.py`'s `exec_command()` (borrowed from NanoVNA-Saver) assumes
  every command eventually re-emits a `ch> ` prompt line. Both gotchas
  above break that assumption -- a command that never returns to the
  prompt will spin through all its retries and then raise `IOError`, but by
  then the underlying pyserial `readline()` calls may have already been
  blocking far longer than `max_retries * wait` would suggest, since a wedged
  USB-CDC device can make Windows serial reads hang past their nominal
  timeout.

## RTB2004 oscilloscope

- **`ACQuire:SRATe?`'s relationship to the real per-sample spacing depends on
  acquisition mode/point count/channel count -- don't assume a fixed
  correction factor.** An earlier `make_t()` divided the reported rate by 4;
  that was apparently right for whatever settings it was written against,
  but is wrong under `rtb2004.py`'s actual settings (`ACQuire:MEMory MANual`
  + `ACQuire:POINts` + segmented mode). Verified directly against a known
  80 MHz reference signal (`rtb2004_timebase_check.ipynb`): using the
  reported rate as-is gives the correct frequency; dividing by 4 does not.
- **`CHANnel<n>:DATA?` (with `DATA:POINts MAX`) returns a wider buffer than
  what's shown on the display -- don't derive the time axis from
  `TIMebase:POSition`/`TIMebase:RANGe`, they describe the display, not the
  returned array.** With `set_timebase()`'s `TIMebase:POSition 3e-6` and
  `ACQuire:POINts 10000`, `TIMebase:RANGe?` reports `1.2e-6` -- that's the
  *displayed graticule*, i.e. what you'd see on the physical screen or
  measure with the front-panel cursors, and `POSition - RANGe/2 = 2.4e-6`
  correctly gives *that* window's start. But `ACQuire:MEMory MANual` with
  `POINts=10000` acquires a much deeper buffer than the 3000 samples (at
  2.5 GSa/s) the display needs for its 1.2 us window -- the other 7000
  samples (3500 before + 3500 after, symmetric because `TIMebase:REFerence`
  is 50%) are captured too, so you can pan/zoom the display after a single
  trigger without re-acquiring. `CHANnel<n>:DATA?` with `DATA:POINts MAX`
  returns that whole 10000-sample buffer, not just the displayed 3000 --
  confirmed by directly counting: exactly 3500 samples in the returned
  array fall before the display's nominal start and 3500 after its end.
  So the buffer's start (`CHANnel<n>:DATA:XORigin?` = `1.0e-6`) is real and
  correct for the array you actually get back in Python -- it's just
  answering a different question than "where does the display start."
  Always get the time axis from `CHANnel<n>:DATA:XORigin?` /
  `CHANnel<n>:DATA:XINCrement?` right after reading out a waveform
  (`RTB2004.get_time_origin()`) and feed that into `make_t(..., t0_s=...)`
  -- never from the timebase/display settings, which describe a different
  (narrower) window than what you're processing.
- **The exact relationship, if you need to predict it without querying**
  (verified against 5 independent `(points, position, scale)` configurations,
  exact match every time, including cases where the buffer starts before
  the trigger and `XOrigin` comes out negative):

      XOrigin = TIMebase:POSition - num_points / (2 * sample_rate)

  i.e. `TIMebase:POSition` is the time (relative to the trigger) of the
  *center of the full acquired buffer* (`num_points` samples at
  `ACQuire:SRATe?`), not of the display window -- `TIMebase:RANGe` cancels
  out of the derivation entirely because the display is just a
  `TIMebase:REFerence`-centered sub-window of that same buffer. Still prefer
  querying `CHANnel<n>:DATA:XORigin?` directly (`get_time_origin()`) over
  recomputing this -- one less place to get out of sync if settings change.
- **`ACQuire:POINts` has a floor of 10000 in `ACQuire:MEMory MANual` mode**:
  requesting fewer points (e.g. `ACQuire:POINts 1000`) silently gets clamped
  up to 10000 (confirmed via `ACQuire:POINts?` readback); requesting more
  (e.g. 20000, 50000) is honored exactly. Always check `ACQuire:POINts?`
  after setting it if the exact record length matters.

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
