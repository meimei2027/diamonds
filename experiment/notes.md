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
- **`frequency_sweep()` with `settle_s=0` (the old default) produces a false,
  >13 dB deep "dip" of >13 dB at specific frequencies (we hit it at ~2.10 GHz
  and ~2.30 GHz) that looks exactly like a real resonance or a generator
  hardware defect -- it's neither.** First suspected this was a genuine
  defect in the generator itself: reproduced it with the generator connected
  directly into the E4403B, nothing else in the chain (ruling out the
  resonator and the amplifier). But holding the generator at those exact
  frequencies directly (bypassing the sweep's per-step logic) showed no dip
  at all, at any settle time up to 2s -- so it isn't a permanent property of
  the generator's steady-state output either. Root cause: `frequency_sweep()`
  does `if settle_s: time.sleep(settle_s)` -- with `settle_s=0` that's falsy
  and the sleep is skipped entirely, so the analyzer's `INIT:IMM` fires
  essentially immediately after the frequency-change command is sent, racing
  the GPIB bus/generator before it's finished processing that command.
  Confirmed by reproducing the exact sweep grid with `settle_s=0` (dip
  reappears every time, sometimes even deeper) vs `settle_s=0.02`-`0.3`
  (completely gone, at every value tested). **Fixed**: `frequency_sweep()`'s
  default `settle_s` is now `0.05` (was `0.0`) -- never pass `settle_s=0`
  intentionally. `coarse_sweep()` (and therefore `resonance_sweep()`'s coarse
  stage) never passed `settle_s` explicitly, so it silently inherited the old
  buggy default -- this is almost certainly what produced an earlier spurious
  result of `f0 ~= 2.09 GHz, Q ~= 5227` that had nothing to do with the real
  resonator. `fine_sweep()`'s existing `settle_s=0.15` was always safe.
- **Interlock operating point, as configured in `hp8673h.py`'s CLI defaults:**
  `--freq-hz 2.68725e9` (measured f0, amplifier in the path, -40 dBm drive --
  reproduced across 3 independent sweeps within ~1 MHz), `--power-dbm 0.0`
  (drives the amplifier to its measured max output, ~20 dBm -- see the
  amplifier gain/max-output notes above), `--threshold-dbm -10.0`.
  Reasoning for the threshold: at 0 dBm drive / ~20 dBm amplifier output,
  assuming ~15 dB return loss for a "detuned but still connected" resonator,
  reflected power (coupled at the established 20 dB coupling factor) is
  expected in the -15 to -25 dBm range during normal operation; -10 dBm
  gives 5-10 dB of margin above that before tripping. For a full-disconnect
  fault (near-total reflection), the coupled reading would be closer to
  0 dBm -- comfortably above the -10 dBm threshold, so it still catches that
  case with margin.
  **Open safety question, not resolved:** in that same full-disconnect fault,
  after the isolator's tested ~7 dB isolation (tested against an open),
  ~13 dBm would reach the HP8347A amplifier's output stage. The HP8347A
  datasheet (Keysight literature 5091-0370E) does not publish a maximum
  safe reflected/reverse power rating for the output port -- "Output SWR"
  in that datasheet describes the amplifier's own output port match (S22),
  and "Reverse isolation >60 dB" describes output-to-input leakage, neither
  of which is a load-mismatch damage rating. The full service manual
  (Keysight part 08347-90023) that might document this is gated behind a
  login and wasn't accessible. Decided to proceed with the -10 dBm
  threshold anyway without empirically fault-testing at reduced power
  first -- if the amplifier is later found to be damaged or behaving
  oddly, revisit this open question first.

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
  **Reconfirmed, with the generator connected directly to the analyzer
  (known power in, no cable/amplifier/coupler in between):** the analyzer
  itself reported `SYST:ERR?` = `652, "Connect Calibration Output to Input"`
  on several unrelated commands (`FORM REAL,32`, `FREQ:CENT`, `FREQ:SPAN`)
  -- it's asking for its own internal alignment. Measured error is NOT a
  constant offset -- it grows with input power, i.e. this isn't only a
  calibration fault but likely front-end compression/overload stacked on
  top of it:

      set (dBm)   measured (dBm)   delta (dB)
        -40.0         -19.92          +20.08
        -30.0          -9.82          +20.18
        -20.0           0.11          +20.11
        -10.0          11.09          +21.09
         -5.0          26.55          +31.55
          0.0          36.51          +36.51

  Roughly constant ~+20 dB from -40 to -10 dBm, then jumps to +31.5 and
  +36.5 dB at -5 and 0 dBm.
  **This fault arose recently -- confirmed the earlier amplifier
  gain/max-output and resonance-sweep measurements were NOT affected by
  it** (taken before this developed). What it does explain: the 4th
  interlock test's immediate trip (`-17.07 dBm` displayed on the very
  first reading, no baseline period, no physical disturbance) was most
  likely a **false trip caused by this fault**, not real resonance drift as
  first guessed -- a true baseline around -40 dBm (consistent with the
  -42 to -50 dBm baselines seen in the earlier, uncompromised interlock
  tests) plus this fault's +20 dB offset would display right around -20
  dBm, over the -30 dBm threshold with no real cause. Re-verify with a
  fresh interlock test once the analyzer is realigned, rather than trusting
  that 4th test's "resonance drifted" explanation.
  **Resolved.** First `Align Now -> RF` attempt (with `AMPTD REF OUT`
  correctly cabled to `INPUT 50` Ohm) was run immediately after power-on
  and did NOT fix it -- `652` errors and the ~+20 dB fault persisted
  essentially unchanged. **The missing step was the ~5 minute warm-up**
  (the E4403B's own spec: meets spec "5 minutes after the analyzer is
  turned on, and after ALIGN NOW [RF] has been run" -- aligning cold
  doesn't count). After warming up and re-running the align: zero `652`
  errors, and the offset came back as a rock-solid constant -4.4 to -4.5 dB
  across the entire -40 to 0 dBm range tested -- exactly the healthy
  "residual cable loss" figure below, confirming the analyzer is fully
  healthy again. If this recurs, warm up for 5 min *before* aligning, not
  just before measuring.
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
- **Confirmed this is a genuine firmware hang, not a clean USB reset**: after
  sending `recall 0`, polled Windows' USB enumeration (`vid=0x0483,
  pid=0x5740`) every 0.2s for 25s -- the device stayed listed/enumerated the
  entire time (never disappeared and re-enumerated), yet every attempt to
  actually communicate with it (even just opening a fresh serial handle)
  failed with "a device attached to the system is not functioning." That
  combination -- descriptor still registered, but the USB transfer layer
  dead -- points to the MCU's firmware locking up (e.g. in an interrupt
  handler or the main loop) rather than doing any kind of USB reset/reboot.
  Waiting longer or reconnecting differently does not help; this has now
  crashed 3 times, always needing the same physical power cycle. Likely
  needs a firmware fix on the NanoVNA side, not a driver-side workaround.
  Treat any `recall`/state-restore command as **crash-risk** until this
  firmware is updated/fixed, and don't call it from an unattended script
  without a human present to power-cycle if it hangs.
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
- **Tried `reset` (full device reboot) as a `recall`-avoiding way to load a
  saved calibration** -- some docs for other NanoVNA firmware forks say
  `save 0` auto-restores at power-on, so a clean reboot should apply
  whatever's in slot 0 without ever touching the buggy `recall` path.
  **Verdict: not reliable enough to use.** Across 3 attempts: 1 self-recovered
  cleanly within ~25s; 2 got stuck with the port still listed in Windows'
  device list (`vid=0x0483, pid=0x5740`) but never actually openable --
  one of those needed only a USB replug, the other needed a full power
  cycle even though the device's own screen looked normal/responsive the
  whole time. That's a 2-in-3 rate of needing physical intervention --
  meaningfully less catastrophic-looking than `recall`'s 100% hard hang,
  but not meaningfully safer to actually rely on. **Conclusion: there is no
  known reliable way to (re)load a saved calibration on this unit without
  physical intervention.** For now, treat calibration as something you set
  once per session from the front panel (or accept running uncalibrated,
  as `nanovna.py` currently does) rather than something a script loads
  automatically -- don't add a `reset`-based `load_calibration()` path
  without addressing this reliability problem first.
- **The real fix is almost certainly to stop trying to use the device's
  `cal`/`save`/`recall` state machine at all, and calibrate on the host
  instead -- this is what NanoVNA-Saver itself does.** Checked its source
  (`Calibration.py`, `Windows/CalibrationSettings.py`): it measures OPEN/
  SHORT/LOAD standards via ordinary sweeps (the same `sweep`/`data 0`
  commands we already use -- no special device-side "cal open" state is
  involved), computes the standard vector error-correction terms
  (directivity/port-match/tracking, the classic 3-term one-port OSL model)
  entirely in Python, applies that correction to data after reading it, and
  saves/loads the calibration as a plain file on the PC's disk -- never the
  device's internal flash slots. Their own code comment on the manual
  cal-standard buttons says outright: "The buttons do not sweep for you nor
  do they interact with the NanoVNA calibration... If you are trying to do
  a calibration of the NanoVNA, do so on the device itself instead" -- i.e.
  even NanoVNA-Saver's authors treat the device's own `cal`/`recall`
  mechanism as something for the front panel, not for scripting.
  **Not implemented yet** -- `nanovna.py` still runs uncalibrated. When we
  do this, it should be a from-scratch one-port OSL calibration routine in
  `nanovna.py` (sweep with OPEN/SHORT/LOAD connected in turn, compute the
  3-term correction, apply to subsequent sweeps), entirely independent of
  the crash-prone `recall`/`reset`/`cal` device commands above.
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
- **The scope has gone unresponsive (`pyvisa.errors.VisaIOError: VI_ERROR_TMO`
  on `*OPC?` inside `wait_for_acquisition()`) right around an interlock trip,
  3 out of 3 times** when running `cw_odmr.py run` with the interlock active
  in a background thread. Not root-caused -- plausible causes include the
  documented shared-USB-hub disruption (see "General" below) triggered by
  the generator's RF switching off at that exact moment, or some VISA-level
  contention between the interlock thread closing its own E4403B session and
  the main thread's concurrent scope query, but neither is confirmed.
  **Immediately retrying with a fresh connection does NOT help** -- tested
  this directly: a brand new `RTB2004` object, reconfigured from scratch,
  hit the identical `VI_ERROR_TMO` again within seconds of the first
  failure. But a plain aliveness check (`*IDN?`) run manually a couple of
  minutes later always succeeded instantly, both times -- so the scope
  isn't wedged the way the NanoVNA's `recall` crash wedges it (no power
  cycle/replug needed), it just needs real wall-clock time to recover, not
  just a new VISA session. `cw_odmr.py`'s scope-acquisition step now waits
  45s after the failure, then reconnects fresh and tries
  `RTB2004.save_available_segments()` to salvage whatever segments the
  scope's history buffer actually captured before the connection died --
  that buffer is independent of the dead connection, so reading it out on a
  new one is safe. Only if salvage comes back empty does it fall back to
  retrying the full acquisition from scratch (up to 3 attempts total,
  discarding partial progress each time it does). The 45s figure is a
  guess, not something bisected -- revisit if it's still failing after a
  full round of retries with waits. Note: a salvaged partial save has fewer
  segments than requested, saved as-is with no special marker in the
  filename -- check `.npy` shape / the printed "salvaged N of M" line if
  the exact count matters downstream.
- **First working salvage run (`interlock_test5`) saved a wrong
  `sample_rate_hz` in its metadata (`109000000.0` instead of the true
  `2500000000.0`), even though the actual waveform data was completely
  correct.** Root cause: `save_available_segments()` originally computed
  `sample_rate_hz` from a fresh `ACQuire:SRATe?` query -- but that reflects
  the scope's *current live* setting, and `RTB2004.__init__()` always sends
  `*RST` on connect (this is a brand-new connection, reconnected after the
  original one died), which resets that live setting to some default. It
  does NOT reset `CHANnel<n>:DATA:XINCrement?`/`XORigin?`, which stay
  frozen to whatever the buffered waveform was actually captured at (this
  is the same distinction noted above for `get_time_origin()`). Confirmed
  by recomputing the saved data's FFT peak with the true 2.5 GSa/s instead
  of the metadata's bogus 109 MSa/s: went from a nonsense 3.488 MHz to a
  clean 80.0000 MHz on every segment -- the data was fine, only the
  recorded rate was wrong. **Fixed**: `save_available_segments()` now
  computes `sample_rate_hz` from `get_time_origin()`'s `dt_s` (queried
  after `save_segments()` has read real data), never from `ACQuire:SRATe?`.
  `interlock_test5`'s existing metadata file was hand-corrected to
  `2500000000.0` after verifying against the raw data. If you find another
  salvaged file with a suspicious `sample_rate_hz`, cross-check it the same
  way (a known-frequency signal's FFT peak, or just compare against
  whatever rate every non-salvaged run in the same session used) before
  trusting it.
- **Same `VI_ERROR_TMO` also happens with no interlock trip at all, during
  history readout (`save_segments()`'s per-segment `read_segment()` loop),
  not during acquisition** -- confirmed on a `first.npy` run at a 10 Hz
  trigger (1000 segments = ~100s to actually trigger): the salvage step
  recovered "1000 of 1000 requested segments", meaning all 1000 had already
  finished triggering and were sitting in the scope's history buffer before
  the connection died reading them out. So this isn't only the
  interlock-adjacent failure documented above -- it's a more general
  "scope goes unresponsive reading out a long segmented history" failure,
  with at least two different phases it can strike in (mid-acquisition vs.
  mid-readout). Since the readout-phase case means the scope is fully idle
  with everything already captured (not still busy mid-acquisition), it
  doesn't need the 45s wall-clock recovery wait that the mid-acquisition
  case needed -- `acquire_segments()` now tracks whether
  `RTB2004.run()`'s `on_acquired` callback fired before the crash and, if
  so, salvages immediately with no wait. Also: RF drive is no longer left on
  during any history readout window (normal post-acquisition readout, or
  the wait-then-salvage sequence after an error) -- `RTB2004.run()` calls
  `on_acquired()` right after triggering finishes and before readout
  starts, and `cw_odmr.py`'s `cmd_run()` uses that (via `acquire_segments()`'s
  `on_rf` callback) to turn RF off for the readout window and only back on
  when a fresh acquisition attempt actually starts. Root cause of the
  timeout itself is still not identified -- this only shortens the
  recovery time and removes the unnecessary RF exposure, it doesn't prevent
  the readout hang from happening.

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
