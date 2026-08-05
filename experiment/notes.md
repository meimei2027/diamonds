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

## T1 measurement (t1_test.py)

- **`RTB2004.run()`'s `start_s` -> `TIMebase:POSition` conversion needs
  `sample_rate_hz` queried in the SAME acquisition mode the real acquisition
  will use, not before.** `run()` added a `scale_s` parameter (coarser than
  the driver's long-standing default `1e-7`, for `t1_test.py`'s wide
  diagnostic capture) -- the sample-rate query used to happen right after
  `TIMebase:SCALe`, before `setup_segmented_mode()` switches into
  `ACQuire:MEMory MANual` + fixed `ACQuire:POINts`. `ACQuire:SRATe?` depends
  on that mode, not just scale -- for the old fixed `scale_s=1e-7` the early
  query coincidentally matched (both regimes apparently give 2.5 GSa/s
  there), but for `scale_s=2e-6` it silently returned the *normal*-mode
  rate (still 2.5 GSa/s, unaffected by the scale change), not the real
  manual-mode rate. Confirmed for real: a diagnostic capture's authoritative
  `t0_s` (from live `CHANnel<n>:DATA:XORigin?`) came out to `-1.9e-5`
  instead of the requested `-5e-6`, and the true rate (inferred from where a
  real signal feature landed) was consistent with ~20x lower than the
  stored 2.5 GSa/s -- i.e. `ACQuire:MEMory MANual` mode really does use a
  different (lower) rate for the same `TIMebase:SCALe`. **Fixed**: `run()`
  now calls `setup_segmented_mode()` *before* querying `ACQuire:SRATe?`, so
  the query reflects the mode actually used. Re-running the same diagnostic
  after the fix gave `sample_rate_hz=312000000.0` and `t0_s=-4.9728e-6`
  (closely matching the requested `-5e-6`) -- self-consistent and
  trustworthy.
- **The AWG sequence's two `readout` segments (dark-time signal +
  calibration) both use the `"highAtStart"` marker, so the scope's EXT TRIG
  can't tell which one it's capturing.** Fine for `run_sweep()` (both
  readouts land in the same captured segment's trace, told apart by
  position within it afterward), but NOT fine for a single-readout timing
  diagnostic. Confirmed for real: a `run_delay_diagnostic()` capture using
  the normal 5-part sequence showed a perfectly uniform 43.2us inter-trigger
  spacing across all 99 gaps in the timetable -- but the two readouts within
  one loop should be ~10.3us and ~11.3us apart (alternating), so a uniform
  43.2us (≈ 2 full loop periods) doesn't match "always the same readout"
  (would give a uniform ~21.6us) or "evenly alternating between both"
  either. Root cause of that specific pattern was not chased down further --
  instead, **fixed by removing the ambiguity entirely**:
  `upload_diagnostic_sequence()` (used only by `run_delay_diagnostic()`) is
  a simplified polarize -> dark -> readout sequence with no calibration
  stage, so every captured segment is unambiguously that one pulse.
- **`SCOPE_START_S` was set too late, missing the actual readout pulse.**
  The real diagnostic measurement (after both fixes above) found the
  readout pulse rises at ~0.39us after the trigger and peaks around ~0.6us
  (with a brief AC-coupling-looking undershoot around ~1.9us before
  settling back to baseline by ~8us) -- the old default `SCOPE_START_S =
  1e-6` opened `run_sweep()`'s 4us capture window *after* the peak had
  already passed, missing the rise entirely. **Fixed**: changed to
  `SCOPE_START_S = -0.5e-6`, giving a window of roughly [-0.5, 3.5]us that
  includes a bit of pre-trigger baseline, the full rise/peak, and most of
  the decay. Re-check with `t1_test.py diagnostic` / `t1_test.ipynb` if the
  optical alignment or AOM changes -- the delay is a property of that
  physical chain, not something derivable from settings alone.
- **Re-uploading `DATA:SEQ` under an already-used sequence name does not
  actually replace it.** `run_sweep()` originally called `upload_sequence()`
  with the same hardcoded name `"test"` for every dark-time point in the
  sweep loop. Confirmed for real: a 100-segment test sweep across dark times
  1/2/3/4us produced FOUR files whose timetables all showed the identical
  `10.3us / 11.3us` inter-trigger gap pattern -- i.e. all four actually
  captured a 1us dark time (the first point's value), regardless of the
  filename. The `DATA:SEQ "test",...` calls for dark=2/3/4us silently had no
  effect; only the very first upload under a given name actually takes.
  **Fixed**: `upload_sequence()`/`upload_diagnostic_sequence()` now take a
  `sequence_name` parameter, and `run_sweep()` uses a unique name per point
  (`f"test_{dark_time_us}us"`) instead of reusing `"test"` -- avoids the
  collision entirely rather than trying to find/use a "delete existing
  sequence" command. If you see suspiciously identical timetable gap
  patterns across different-dark-time files again, re-check this first.
- **The "calibration" (fresh-polarization reference) stage was removed --
  it wasn't measuring what it was meant to.** The original sequence was
  polarize -> dark -> readout (signal) -> polarize -> readout
  (calibration), meant to give a "fully bright" reference to normalize the
  dark-time signal against. Confirmed for real (`1us_calib` sweep point,
  segments separated by the gap-alternation pattern into signal vs.
  calibration groups): the calibration trace showed NO detectable
  fluorescence pulse anywhere in the capture window, while the signal trace
  right next to it showed a clear one. Root cause: the calibration readout
  fires ~10.3us after the signal readout's trigger, by which point the
  laser has already been continuously on (through the rest of the signal
  readout and the whole second polarize segment) -- so it's measuring
  saturated steady-state fluorescence after the initial bright transient
  has already decayed, not a comparable "fresh bright" reading. (Separately,
  the initial signal-trace analysis also had a sign-convention bug: this
  PMT's fluorescence is NEGATIVE-going, so the true onset is a negative-
  going crossing, not `abs(deviation)` -- the latter picked up a positive-
  going transient instead, most likely the AOM still ramping up to full
  diffraction efficiency rather than real fluorescence.) **Fixed**:
  `upload_sequence()` is now just polarize -> dark -> readout, used
  identically by both `run_sweep()` and `run_delay_diagnostic()` (which
  used to have its own near-identical `upload_diagnostic_sequence()`, now
  removed as redundant). Every captured segment is now unambiguously the
  one readout -- no more gap-pattern classification needed for new sweep
  files (still needed for old ones captured before this fix, e.g. the
  existing `1us`/`2us`/`3us`/`4us`/`6us` files in `D:/t1`, which used the
  old 5-part sequence). Since half of every old sweep point's segments were
  "wasted" on the now-removed calibration reads, `SEGMENTS` dropped from
  5000 to 2500 to keep the same real per-point sample count.
- **A constant AOM/PMT turn-ON delay does NOT by itself limit how short a
  meaningful dark time can be -- that was a wrong inference.** Reasoning
  from the Keysight 33600A's datasheet: the generator's own hardware floor
  for an arb segment is 32 samples at 1 GSa/s = 32ns, far below anything the
  sweep currently uses (`DARK_UNIT_S=1us` = 1000 samples, a design choice,
  not a hardware limit). But a *fixed* turn-on delay (i.e. how long after
  the readout trigger the fluorescence appears) only says where to look for
  the signal -- it says nothing about how short the preceding dark period
  can meaningfully be. The actual constraint is a different quantity: how
  fast the AOM stops diffracting light once told to turn OFF. If that
  turn-off transient takes some finite time, a nominal dark period shorter
  than it wouldn't actually be dark, regardless of readout timing. Added
  `run_falloff_diagnostic()` (`python t1_test.py falloff`) to measure this
  directly -- polarize (bright) -> dark, triggered on the polarize->dark
  TRANSITION itself (`upload_falloff_diagnostic_sequence()`: the first 1us
  of the off period gets its own "once" segment carrying the "highAtStart"
  marker, mirroring how "readout" carries the marker in the normal
  sequence, so the trigger fires exactly once per loop right at turn-off),
  with a wide (~32us) capture window (`FALLOFF_DIAGNOSTIC_START_S=-2e-6`,
  same `DIAGNOSTIC_SCALE_S` as the turn-on diagnostic) and a long enough
  off period (`FALLOFF_DIAGNOSTIC_OFF_US=80`) that the sequence doesn't
  loop back to "polarize" before the decay is fully captured. Saves
  `data/diagnostic_falloff.npy`. Not yet run against real hardware.
- **`KS33600A.write()` used to only print SCPI errors when `debug=True`, and
  never raised** -- so a real AWG-side failure (e.g. `DATA:SEQ` hitting the
  instrument's own limit on the number of distinct named sequences it can
  hold) let a script keep running with the AWG silently stuck outputting
  whatever sequence it last successfully loaded, while a sweep kept saving
  files under the *intended* label regardless of what was actually being
  generated. Confirmed for real: a `contrast_check` run hit "specified arb
  not loaded in waveform memory, arb: too many sequences defined" partway
  through, and a downloaded timetable for a supposedly-later file showed an
  inter-trigger period matching an EARLIER, already-completed point --
  multiple files in that run were silently corrupted this way, not just the
  one that errored. **Fixed**: `write()` now always checks `SYST:ERR?` and
  raises `RuntimeError` on any error, so this failure mode stops the run
  immediately instead of silently corrupting data. The sequence-limit root
  cause (each sweep point uploads a uniquely-named `DATA:SEQ`, needed per
  the collision bug above, and nothing ever deletes an old one) is
  mitigated in `run_sweep()` via `RESEQUENCE_INTERVAL=20` -- every 20
  points, the AWG is reset (`*RST` + `DATA:VOL:CLE`) and the arb waveforms
  re-uploaded from scratch, clearing the sequence table before it fills up.
  The exact limit wasn't bisected -- one 100-point sweep hit it around its
  80th point, another (same script, different session) as early as its
  32nd -- don't assume a fixed number; re-validate any long-sweep dataset
  against its own timetables (the dark-time-vs-filename check earlier in
  this section) before trusting it.
- **A from-scratch T1 re-measurement (`D:/t1_new`, 218 points) gave a
  dramatically different fit than the original `D:/t1`: tau=378+/-19us vs.
  tau=18.7+/-0.4us, ~20x longer, with ~9x smaller amplitude.** Checked
  whether the SR445A output-impedance mismatch (see "General" below) could
  explain this: it can't touch tau at all (a pure amplitude-scaling factor
  multiplies `A` and `C` in `A*exp(-t/tau)+C` but can't appear in the
  exponent -- no scaling error makes a decay look faster or slower), and it
  can explain at most half the amplitude change (~2x from the impedance
  fix, vs. ~9x actually observed) -- so most of both differences reflect a
  real change in what's being measured, not an instrumentation artifact.
  Plausibly consistent with the existing theory (above) that the original
  ~18.7us figure was measuring fast shelving/charge-state recovery rather
  than real spin-lattice T1 -- 378us is far more in line with expected real
  T1 at room temperature.
- **The `1us` dark-time point can show NO real fluorescence signal at all,
  dominated instead by a ~1us-wide flat positive artifact** -- confirmed
  reproducible with a 10x-larger retake (N=1000 segments, identical shape).
  Checked across the rest of the `D:/t1_new` sweep: 217/218 points show the
  expected significant negative-going dip; the artifact's duration does NOT
  scale with labeled dark time (~1-1.2us at both 1us and 2us dark time,
  then drops sharply by 3us) -- consistent with the pre-existing,
  already-characterized AOM ramp-up transient (above) simply dominating the
  entire visible window at the shortest dark time, not a new problem.
  Already excluded from the exponential fit by the existing
  `MIN_FIT_DARK_TIME_US=3` convention -- no code change needed, just don't
  be alarmed by it in isolation; check the WHOLE sweep's dip-significance
  and artifact-width-vs-dark-time (like this) before suspecting a bigger
  problem from one point.

## CW-ODMR contrast_check (cw_odmr.py)

- **`acquire_segments()`'s scope VISA timeout was a hardcoded 100000ms,
  independent of segment count or trigger rate** -- fine for the
  1000-segment/10Hz defaults used elsewhere, but a 2000-segment
  `contrast_check` run at the same 10Hz (needing >=200s of triggering
  before the scope's blocking `*OPC?` query inside `wait_for_acquisition()`
  can return) hit `pyvisa.errors.VisaIOError: VI_ERROR_TMO` well before the
  acquisition could finish. **Fixed** in two places that need to agree
  (fixing only one just converts one timeout into another):
  `acquire_segments()` now computes a VISA timeout that scales with
  `segments / trigger_freq_hz` (with margin), and `RTB2004.run()` gained an
  `acquisition_timeout_s` parameter so its own Python-side polling-loop
  ceiling (`wait_for_acquisition()`, previously a hardcoded 60s regardless
  of segment count) matches the VISA-level one.
- **A per-block interlock check that reads reflected power BEFORE turning
  RF on isn't actually checking anything.** `cmd_contrast_check()`'s first
  version read reflected power at the top of each loop iteration, right
  after the previous block's `rf_off()` -- always saw the noise floor (no
  forward power to reflect) and could never catch a real fault. **Fixed**:
  the check now happens after `rf_on()` (plus an `rf_settle_s` delay,
  default 5.0s, added after every RF on/off transition since the
  generator/amplifier chain doesn't respond instantaneously), so the
  reading reflects real drive conditions.
- **Found a statistically strong (5-7 sigma), reproducible difference
  between RF-on and RF-off segments at 2.87 GHz -- but with the WRONG SIGN
  for real ODMR, and it isn't spin physics.** Given this PMT's established
  sign convention (more negative = more fluorescence), a real ODMR
  resonance (population pumped bright ms=0 -> dark ms=+-1) should make
  RF-on LESS negative than RF-off; instead RF-on measured MORE negative in
  two independent runs (`contrast1`: -0.274mV, 5.5 sigma; `contrast2`,
  using an alternating-block design specifically to rule out drift:
  -0.396mV, 7.3 sigma -- same sign, if anything larger). Two follow-up
  tests ruled out the two most likely explanations:
    1. Laser off (no photocurrent) -> effect vanishes (-0.057mV, 1.1 sigma,
       not significant) -- rules out simple electrical pickup that doesn't
       care about the optical signal.
    2. Same test at 2.368 GHz (500+ MHz from any plausible NV resonance)
       -> effect is STILL strong (+0.351mV, 6.6 sigma) and comparable in
       size to the "on-resonance" runs, but with the OPPOSITE sign -- rules
       out genuine NV spin resonance, which should be sharply peaked near
       2.87 GHz and essentially absent 500 MHz away.
  Conclusion: most consistent with a frequency-dependent artifact that
  requires real photocurrent to manifest (e.g. RF leakage/standing-wave
  effects coupling into the PMT's gain or amplifier chain in a way that
  depends on drive frequency), NOT real ODMR contrast. **Any future ODMR
  contrast measurement on this setup needs this ruled out first**
  (laser-off + far-off-resonance controls, same as above) before trusting a
  sign or magnitude. See `contrast_check_result.ipynb`.

## Mini-Circuits ZYSWA-2-50DR+ RF switch (pulsed ODMR gating)

- **Absorptive, not reflective -- RF always goes somewhere, there's no true
  "off" state.** Truth table: control LOW -> RF IN routed to RF1; control
  HIGH -> RF IN routed to RF2. Whichever port isn't selected still needs a
  proper 50 ohm termination (a dummy load on RF1 if RF2 is the "sample"
  path), or the unselected fraction of every cycle reflects back into the
  switch/generator instead of being absorbed.
- **Needs a separate +-5V DC power supply for its internal driver -- NOT
  derivable from the AWG.** The TTL control signal (0-0.7V LOW / 2.1-5V
  HIGH) and the +-5V driver power are electrically distinct; an AWG channel
  is a signal source, not rated/protected to sustain the driver's operating
  current as a power rail. Ground the supply's COM/return to the switch's
  own ground (already tied to the AWG's ground via the control cable's
  shield) -- a star topology through the switch, not a second independent
  wire directly between the supply and the AWG (which would close a loop).
- **Confirmed working via `rf_switch_test.ipynb`**: chopping a 2.5 GHz
  carrier with a square wave on AWG CH2 (10% and 50% duty cycle, 1 kHz and
  1 MHz) produces the expected AM sideband comb on the spectrum analyzer,
  verified two independent ways -- visually (50% duty: sharp discrete peaks
  at odd harmonics only, exact nulls at even harmonics, matching the
  classic square-wave Fourier series) and quantitatively (FFT of the trace
  itself shows a standout peak at the expected periodicity, 14-21x the
  background level, robust to detrending).

## SR830 lock-in amplifier

- **Must send `OUTX 1` on connect, or query responses go out RS232 instead
  of GPIB and `query()` hangs forever.** Not optional -- `sr830.py`'s
  `__init__()` does this automatically.
- **Use X (after nulling Y), not R, for weak signals where sign matters.**
  R = sqrt(X^2+Y^2) is always >=0 and has a well-known positive bias for
  small signals (rectifying symmetric noise), AND it discards sign entirely
  -- both are a problem when (as in the contrast_check investigation above)
  the sign of a small effect is the key diagnostic. X (with phase nulled
  against a known strong signal at the same reference first) keeps sign
  and has the best per-channel SNR.
- **Phase calibration uses the SAME reference as the real measurement, just
  a much stronger signal to null against** -- e.g. feed the reference
  signal directly into the input temporarily, or drive a deliberately
  large, real modulation at the reference rate, null Y, then switch back to
  the real (weak) measurement without touching the phase setting. Don't
  expect `auto_phase()` to work reliably on a signal too weak for it to get
  a stable reading.
- **`cw_odmr_lock_in.py`'s resonance sweep must run with the ZYSWA switch
  held static, not chopping.** Originally `cmd_run()` called `setup_chop()`
  (CH2 square wave) before `resonance_sweep()`, so the analyzer read
  reflected power while the switch was already flipping between RF1/RF2 on
  every chop cycle -- garbage/AM-sideband signals riding on top of the
  resonance dip, not a clean trace (this is what was seen live on the
  spectrum analyzer during the coarse sweep). Fixed by adding
  `set_switch_static()` (CH2 held at a fixed DC level, HIGH = RF2/sample
  path per the ZYSWA truth table) for the resonance sweep, only calling
  `setup_chop()` afterward once f0 is found and the lock-in sweep is about
  to start.
- **Second pickup-artifact signature found with the lock-in readout,
  converging with the `contrast_check` investigation above**: sweeping
  `cmd_run()`'s lock-in signal shows a MUCH bigger reading at the
  resonator's own resonant frequency (found via `resonance_sweep()`'s dip
  in reflected power) than at 2.87 GHz (the actual NV zero-field
  transition), when the two don't coincide -- backwards from real ODMR,
  which should peak at the NV transition (real spin-microwave coupling) and
  vanish elsewhere, not track the resonator's own structural resonance. A
  resonant structure concentrates/enhances the local RF field AT its own
  resonance by design (that's what "resonant" means) even though *more*
  power is reflected back (not absorbed) away from it -- so a pickup
  artifact driven by ambient RF field strength near the resonator, not real
  NV coupling, would naturally peak exactly where this data peaks.
  Follow-up **carrier-off test** (`cw_odmr_lock_in.py single ... carrier_off=true`,
  keeps CH2 chopping the switch and the SR830 demodulating normally, but
  holds the generator's RF output off throughout via explicit `rf_off()`,
  never `rf_on()`) showed the lock-in signal drops way down with the
  carrier off -- confirms the pickup needs real RF power present, ruling
  out simple digital crosstalk from the AWG chop control line itself as the
  dominant source, and leaving resonator-near-field pickup into the PMT
  cable/electronics as the leading explanation.

  **Full localization chain** (each test isolates one stage of the PMT
  signal path):
  1. RF power amplifier driving a dummy load instead of the resonator ->
     no background. Requires the resonator specifically to be driven with
     real RF, not just RF present anywhere upstream in the chain.
  2. Optical path to the PMT blocked entirely (no light at all) -> background
     still present. Rules out anything involving real photons/fluorescence --
     the effect cannot be optical.
  3. PMT itself powered off (no HV, no gain) -> background still present.
     Rules out the PMT tube/photocathode/dynode chain -- an unpowered PMT
     can't produce a meaningful output regardless of light.
  4. PMT's SR445A preamp powered off -> NO background. Localizes the pickup
     specifically to the preamp stage -- its own circuitry needs to be
     actively biased/amplifying for the effect to appear (consistent with
     RF coupling onto an active, biased circuit and getting rectified/
     amplified into a baseband artifact; a passive/unpowered stage has no
     mechanism to do that).
  5. Preamp is plastic-housed -> no RF shielding at all (plastic is
     transparent to RF; only a continuous conductive enclosure blocks it).
     Moving the preamp farther from the resonator dropped the reading from
     6 uV to 2 uV -- direct near-field-coupling-with-distance signature,
     confirming the mechanism is RF penetrating the unshielded preamp
     housing from the resonator's near-field, not a ground loop (ruled out
     by the plastic, non-conductive chassis not bonding to the optical
     table) and not any single interconnecting cable.

  **Fix**: replaced the SR445A preamp entirely with a simple resistor
  transimpedance (PMT anode current -> resistor -> voltage directly into
  the SR830), for this CW-lock-in signal path specifically. Rationale: the
  preamp's x5 voltage gain over its 50 ohm input impedance is only an
  effective ~250 ohm transimpedance -- a deliberately chosen, much larger
  discrete resistor gets both better underlying SNR (Johnson-noise-limited
  SNR scales as sqrt(R), so a ~100x-600x larger R gives a substantial
  improvement, limited by the RC rolloff from cable/PMT stray capacitance
  needing to stay well above the ~1 kHz chop rate) AND removes the pickup
  mechanism entirely, since a purely passive resistor has no active/biased
  nonlinear element to rectify coupled RF into a baseband signal the way
  the powered preamp evidently did. Confirmed on the bench: background is
  gone with the resistor in place. NOTE: the SR445A is still needed for
  fast pulsed/T1-style measurements elsewhere in this project (DC-300 MHz
  bandwidth) -- this swap is specific to the slow (~1 kHz chop) CW lock-in
  path, not a wholesale replacement.

- **Separately, ALL `cw_odmr_lock_in.py` lock-in data taken before AWG CH1
  was explicitly reconfigured is invalid.** `KS33600A.__init__()`
  unconditionally sends `*RST` on every connection (needed elsewhere, e.g.
  to guarantee a known clean state) -- `*RST` resets BOTH channels to their
  power-on default (output off), including CH1. `cw_odmr_lock_in.py` only
  ever configured CH2 (the chop), never touched CH1 at all -- so any prior
  CH1 setup (e.g. a continuous carrier needed for the measurement, set up
  by a separate script like `run_alignment.py`) got silently wiped out the
  moment ANY `cw_odmr_lock_in.py` command connected to the AWG, without any
  error or warning. Confirmed the impact is real: after adding
  `setup_ch1_carrier()` (CH1: continuous, unmodulated 80 MHz / 632 mVpp
  sine, called right after connecting to the AWG in `cmd_run`/`cmd_single`/
  `cmd_sweep_average`, before CH2 is touched) so CH1 stays correctly
  configured throughout, **a real ODMR dip was observed for the first time**
  -- everything recorded before this fix (`lockin1`-`lockin9`,
  `backgroundfree1`/`2`, `average1`) was taken with CH1 silently off and
  should be treated as invalid, regardless of what it appeared to show.
  `scan1`-`scan7` were taken after this fix was in place (`setup_ch1_carrier()`
  already added to `cmd_run`/`cmd_single`/`cmd_sweep_average`) and are valid
  -- these are the first runs to show the real dip.

- **The physical resonance can drift out of a `sweep-average` power study's
  frequency window WITHOUT tripping the interlock, if scan power is low.**
  The interlock only watches reflected power against a fixed threshold --
  at low drive power, reflected power stays comfortably under that
  threshold whether or not the resonator is actually well-matched at the
  frequencies being scanned, so a real physical drift (resonator detuning
  between sessions) goes completely undetected by the existing safety
  check. Confirmed for real: found the resonance had physically drifted
  away from where a `powerstudy*` run assumed it was, discovered only by
  comparing run-to-run scale differences (see the X-vs-R phase-calibration
  discussion above) and manually checking, not by anything alerting during
  the run. **`powerstudy9`'s data is affected by this** -- its
  power-comparison numbers should be treated with caution until re-checked
  against a resonance position record. **Fixed going forward**:
  `cmd_sweep_average()` gained `check_resonance_before_sweep=true`
  (default on) -- before each power level's repeats start, sweeps
  reflected power across that exact frequency range and records/reports
  where the dip actually is (saved as `_resonance_check_freqs_hz.npy`/
  `_resonance_check_reflected_dbm.npy`, plus a printed warning if the dip
  is far from the center of the range) -- diagnostic only, does not gate
  or abort anything itself. Any `powerstudy*` run from before this was
  added has no such record and can't be retroactively checked this way.

## Pulsed ODMR (pulsed_odmr.py) -- NOT YET TESTED

- Uses `[SOURce[1|2]:]PHASe:SYNChronize` (confirmed via Keysight's own
  Trueform SCPI documentation) to align CH1 (laser sequence) and CH2 (MW
  gate, via the ZYSWA switch) after both are independently started --
  necessary since `OUTPUT1 ON`/`OUTPUT2 ON` are two separate SCPI writes
  with no inherent timing guarantee between them. **Open item**: not
  verified this actually holds the two channels in sync over a full
  multi-minute scan on this specific firmware -- check by scoping CH1's
  marker/sync output against CH2's gate simultaneously at the start and end
  of a long run.
- Confining the MW pulse to the dark period (never overlapping
  polarize/readout) sidesteps the RF-pickup artifact found in
  `contrast_check` (above), since that artifact needed RF and photocurrent
  simultaneously present -- pulsed mode never has both at once by
  construction. Validate this directly (e.g. a far-off-resonance pulsed
  comparison, same idea as the CW contrast_check controls) before trusting
  a pulsed ODMR result, in case sync drift (previous bullet) lets the pulse
  creep toward the readout window over a long scan.
- Dephasing (T2*) during the gap between the MW pulse and readout does NOT
  matter for a population-based (single-pulse) measurement like this --
  only T1 (population relaxation) does, since no coherence/interference
  information is being read out. Keep the gap short relative to whatever
  the relevant relaxation timescale is (the T1 sweep above suggests
  order-100s of us, not the ~18.7us originally assumed) rather than
  worrying about T2*.

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
- **The SR445A preamp's output impedance is 50 ohms, and its rated gain
  assumes that output is terminated into 50 ohms** -- the amplifier's own
  50 ohm output impedance and an external 50 ohm load form a voltage
  divider that halves the swing, and that halving is already baked into
  the datasheet's gain spec. Feed it into a high-impedance input instead
  (the RTB2004 scope's default 1 Mohm channel impedance, or the SR830
  lock-in's fixed 10 Mohm || 2pF input -- not user-switchable via any SCPI
  command, unlike the scope) and you get the *undivided* output: roughly
  2x the amplifier's spec'd gain. Confirmed for real: fed a 652 mVpp 1kHz
  sine into the SR830 (0.2305 Vrms expected from a naive Vpp/(2*sqrt(2))
  conversion), measured X = 0.4432V after nulling Y -- a ~1.92x ratio,
  consistent with the missing termination. **Fixed** by adding a 50 ohm
  pass-through terminator at the SR830's input. Also matters beyond
  amplitude: the SR445A's fast rise/fall time (1.3ns typ.) means an
  unterminated output can reflect/ring on fast edges too, not just scale
  wrong -- proper 50 ohm termination fixes both at once. Caution if ever
  splitting the SR445A's output to feed both the scope AND the SR830
  simultaneously: terminating BOTH independently puts two 50 ohm loads in
  parallel (~25 ohms combined), under-matching the amplifier further --
  use a proper 50 ohm splitter (50 ohms at each output port) rather than a
  plain T with two separate terminators.
