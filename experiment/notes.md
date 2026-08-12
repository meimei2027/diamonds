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

## cw_odmr_lock_in.py operation notes

- **Hardware**: AWG CH2 is tee'd to both the ZYSWA switch's control pin
  (gates the microwave) and the SR830's external reference input, so the
  lock-in locks onto the exact signal driving the switch (TTL rising-edge
  mode). 50% duty by default -- puts the most energy in the fundamental
  harmonic of any duty cycle (same Fourier reasoning as
  `rf_switch_test.ipynb`'s sideband work). Laser stays on continuously
  throughout; only the microwave is chopped.
- **Readout is continuous demodulation, not one-reading-per-chop-cycle**:
  the SR830 demodulates the PMT signal against the CH2 reference
  continuously and low-pass filters with a settable time constant (e.g. a
  100ms time constant at 1kHz chop averages ~100 cycles). Per frequency
  point: set frequency -> wait several time constants for the filter to
  settle -> read one X/Y -> move on. The chop cycle itself never surfaces
  as a discrete readout.
- **Phase is NOT auto-calibrated by this script** -- do it manually once
  per session (feed a large real modulation at the chop frequency, null Y
  by adjusting phase, or use `SR830.auto_phase()` if the signal is already
  strong enough to converge reliably), then pass the result as `phase_deg`.
- Before trusting any ODMR result from this script, run the same two
  controls that caught the chop-synchronized pickup artifacts above (see
  "CW-ODMR contrast_check" and "SR830 lock-in amplifier" sections): laser
  off (a real artifact-of-pickup should vanish) and far off-resonance (real
  ODMR contrast should vanish/shrink there; the artifacts above did not).
  If those controls aren't clean, `pulsed_odmr.py` is the safer path -- it
  confines the MW pulse to the dark period, so RF and photocurrent are
  never present at the same time.
- `cmd_single`'s `carrier_off=true` isolates whether an observed signal
  needs real RF power, or is just crosstalk from the AWG CH2 control line
  itself: CH2 keeps chopping the switch and the SR830 keeps demodulating
  normally, but the generator's RF output is held off throughout. Signal
  vanishing means it needed real RF power (e.g. resonator near-field
  pickup); persisting means it's coming from the control line itself.
- `cmd_sweep_average()` averages X and Y elementwise across repeats, THEN
  computes R from the averaged X/Y -- never averages R directly, since
  R = sqrt(X^2+Y^2) has a positive noise-rectification bias (see SR830
  section above) that averaging after the fact doesn't remove. A partial
  repeat (interlock trip or Ctrl+C mid-repeat) is saved to disk but
  excluded from the average -- a short/NaN-padded array averaged against
  full ones would bias the result, not just reduce its noise.
- `cmd_sweep_average()`'s real-time overload handling
  (`auto_rescale_on_overload`, default on): checks the SR830's hardware
  overload status after every point (not just eyeballing values
  afterward), since real signal size can vary a lot across a sweep (e.g.
  much bigger right at a resonance dip than elsewhere). On overload, steps
  sensitivity ONE range coarser at a time (never straight to
  `auto_gain()`, which could overshoot into an overly-insensitive range
  based on one anomalous point) and rescans from a few points back
  (`rescan_backoff_points`), not from the start of the repeat -- points
  before that already read fine at the old, more sensitive range. Gives up
  after `max_rescale_attempts` and treats the repeat as PARTIAL (saved,
  excluded from the average) rather than looping forever.
- **`coil_voltage_margin`'s old default (1.2, i.e. 20% headroom) wasn't
  enough to actually reach the commanded coil current on a long scan.**
  Confirmed for the `static_new*` runs: commanding `coil_current_a=2.0`
  with the old default only achieved ~1.92 A actual coil current (per
  `SPD1305X.read_current()`), because the SPD1305X hit its voltage
  compliance limit before reaching the requested current -- the coil's
  *actual* (warm) resistance during the scan was higher than
  `spd1305x.py`'s cold-calibration value predicts (working backward from
  the 1.92 A shortfall: ~0.60 ohm actual vs. ~0.48 ohm cold-calibrated,
  ~26% higher), consistent with ordinary resistive self-heating over a
  long run -- and a 20% voltage margin over the *cold* prediction wasn't
  enough to cover that. **Any `static_new*` scan's actual coil current
  (and therefore field -- see the NV-field-estimation discussion below)
  may be somewhat below its nominal `coil_current_a` setting for this
  reason; check `SPD1305X.read_current()` if the exact current matters for
  a specific run, don't just trust the commanded setpoint.** **Fixed**:
  `cw_odmr_lock_in.py`'s `coil_voltage_margin` default raised from `1.2`
  to `1.5` (50% headroom) in `cmd_run()`/`cmd_single()`/
  `cmd_sweep_average()`, comfortably above the ~1.25 that would have just
  barely reached 2.0 A given the coil's apparent warm resistance, leaving
  room for further heating on an even longer scan.
- **Estimating the applied field from the observed NV splitting for the
  `static_new*` runs**: using `nv_center.ipynb`'s magic-angle spin-1
  Hamiltonian model (field along [100] on this (100)-cut diamond, at
  54.74 deg to every NV axis -- not the simple on-axis `D +- gamma*B`),
  the two double-Lorentzian fits in `cw_odmr_lock_in_result.ipynb`
  (splittings 54.12 MHz for the `power_up` subset, 54.36 MHz for the
  non-`power_up` subset) both invert to essentially the same field, ~16.7-
  16.8 G. This is well below the ~28.4 G `spd1168x.py`'s calibration table
  measures directly at `I=2.0 A` "right at the coil" (and still well below
  the ~27.3 G predicted for the actual ~1.92 A current, per the point
  above) -- **the diamond was positioned further from the coil than the
  calibration point** for these runs, not merely a current shortfall.
  Treating the coil as a simple single-turn loop (on-axis field
  `B(z) = B0 / (1 + (z/R)^2)^(3/2)`, `B0` = the calibrated field at the
  coil center) gives `z/R ~= 0.65` -- i.e. the diamond sat about 65% of
  one coil-radius away from the coil's center plane. The coil's actual
  physical parameters (not documented anywhere else in this repo -- see
  `spd1305x.py`/`spd1168x.py`/`drawings/`, none of which record the real
  coil geometry) are **R = 31.4 mm, N = 68 turns**, giving an absolute
  distance of **z ~= 20.3-20.4 mm (~2.0-2.1 cm)** from the coil's center
  plane to the diamond. Cross-check: the theoretical on-axis field for
  this R/N at I=2.0A (`B0 = mu0*N*I/(2R)`) works out to ~27.2 G, matching
  the measured calibration point (28.4 G) to within ~4% -- close enough to
  confirm the single-loop-at-radius-R approximation is reasonable for this
  coil, not wildly off.

## Pulsed ODMR (pulsed_odmr.py) -- NOT YET TESTED

**Rewritten from scratch** (the file had been deleted at some point --
this whole section predates that and was written against an earlier,
no-longer-present version). New version: frequency-sweep/resonance-
finding structure copied from `cw_odmr_lock_in.py`'s `cmd_run()`, pulse
sequence copied from `rabi.py`'s `setup_awg_sequences()` (same
onceWaitTrig-anchor combined-arb design, `anchor_free_reps`, `extra_settle_s` fixes
from this session's whole background-artifact investigation) with
`tau_mw_us` FIXED (default 5.0) instead of swept -- only `freq_hz` varies
point to point, the mirror image of `rabi.py`'s own sweep. Confirmed still
consistent with the design constraints recorded below: does NOT use
`PHASe:SYNChronize` (the onceWaitTrig anchor mechanism is the validated
CH1/CH2 sync fix instead, already built into `setup_awg_sequences()`), and
reuses `setup_awg_sequences()` unchanged so it also inherits the `VOLTage`
-not-`FUNC:ARB:PTP` amplitude fix. Since the pulse sequence never changes
across frequency points (only the generator's frequency does), `setup_awg_
sequences()` runs ONCE before the frequency loop, not per point -- avoids
the whole reconfigure-per-point artifact class by construction. The
periodic interlock check still races with the AWG's own anchor timing the
same way it did in `rabi.py` (see that investigation above), so the same
ABOR-plus-`extra_settle_s` fix is included unchanged. STILL NOT TESTED ON
REAL HARDWARE -- validate with the same controls mentioned below (laser
off, far off-resonance) before trusting any result from it.

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

## Rabi oscillation (rabi.py) -- NOT YET TESTED

Design: instead of `cw_odmr_lock_in.py`'s continuous fast chop, the lock-in
reference comes from a slow block structure on CH1 -- `n_reps` loop
iterations with the MW pulse present ("on" block), then `n_reps` without
("off" block), with the "on"/"off" blocks marked `highAtStart`/`lowAtStart`
in the `DATA:SEQ` sequence table so CH1's Sync/marker BNC output becomes
the reference square wave. See
`tests/rabi_awg_marker_test.ipynb` for the AWG-only bench test this is
being verified against before building the real sweep into `rabi.py`.

- **Earlier entry in this section claimed an all-`"repeat"`-segment
  `DATA:SEQ` sequence (no `"once"` anchor) doesn't play on this hardware --
  this is now believed to be WRONG, or at least not the real explanation.**
  The actual Keysight 33500/33600 programming manual's own `DATA:SEQuence`
  examples (`MMEMory` subsystem example, e.g.
  `"dc5v",2,repeat,maintain,5`) use sequences made entirely of `"repeat"`
  segments with no `"once"` segment at all, and document them as working.
  The original ad hoc test that seemed to show all-`"repeat"` failing most
  likely actually hit the separately-documented "re-uploading a
  `DATA:SEQ`/arb name that already exists silently does nothing (or
  errors)" issue instead (see `t1_test.py`'s `upload_sequence()` docstring
  and the `DATA:ARBitrary` manual entry, page 241: "Specifying a waveform
  that is already loaded generates a 'Specified arb waveform already
  exists' error"), triggered by re-running the same upload cell more than
  once in one AWG session -- not a fundamental restriction on all-`repeat`
  sequences. The `"once"`-anchor structure was removed from the test
  notebook's sequence-building cell as a result; it now uses two plain
  `"repeat"` segments per block per channel, matching the manual's
  validated pattern, and this still produces correct sequence playback.
- **Confirmed real bug: `SOURce{ch}:FUNC:ARBitrary:PTPeak`
  (`FUNC:ARB:PTP`) does NOT reliably update the channel's actual output
  amplitude register on this firmware.** Set `SOUR2:FUNC:ARB:PTP 5.0` and
  `SOUR2:VOLT:OFFS 2.5`, then queried `SOUR2:VOLT?`/`SOUR2:VOLT:OFFS?`
  afterward and got back `0.1`/`0.0` (old/default values) -- no SCPI error
  raised at any point. Switching to the plain `VOLTage`/`VOLTage:OFFSet`
  commands instead (`SOUR{ch}:VOLT <vpp>`) and re-querying confirmed they
  actually stick. **Use `VOLTage`, not `FUNC:ARB:PTPeak`, for setting ARB
  channel amplitude on this instrument going forward** (affects
  `tests/rabi_awg_marker_test.ipynb`'s `configure-output` cell; check any
  other script using `FUNC:ARB:PTP` for amplitude, e.g. `t1_test.py`'s
  `setup_awg_output()`, `pulsed_odmr.py` -- not yet audited/fixed there).
- **Confirmed real (but not confirmed as the cause of any specific
  symptom) driver gap**: `KS33600A.upload_waveform()` uploads arb data via
  a raw `pyvisa.write_binary_values()` call, bypassing `write()`'s
  `SYST:ERR?` check entirely. Since re-uploading an arb under an existing
  name errors per the manual (see above), re-running a waveform-upload
  cell in the same AWG session without first clearing volatile memory
  could silently leave stale data in place with no error surfaced
  anywhere. This is a real gap worth fixing in `ks33600a.py` (mirror
  `write()`'s error check after the binary transfer) -- not yet done.
  Added a `SOUR{1,2}:DATA:VOL:CLE` cell to
  `tests/rabi_awg_marker_test.ipynb` as a precaution before every
  waveform re-upload, but this was **never independently confirmed** to
  be the actual cause of a specific symptom seen in this session (a
  missing `PRE_US` gap) -- that was a plausible hypothesis at the time,
  not a verified diagnosis. Don't cite it as a confirmed fix.
- **`SOUR2:DATA:SEQ` (targeting channel 2's sequence table with an
  explicit prefix, since t1_test.py's proven CH1 usage is unprefixed) is
  confirmed accepted without a SCPI error on this firmware** -- this was
  flagged as unverified in `pulsed_odmr.py`'s own docstring; now checked.
- **Resolved: CH1's Sync/marker BNC output IS a clean single edge per
  block, not a burst of `n_reps` quick edges.** Confirmed on real hardware
  with a `"repeat"`-segment `DATA:SEQ` sequence and `marker_mode`
  `highAtStart`/`lowAtStart` on each block's segment -- the Sync signal
  showed a smooth square wave at the block period with no restarts or
  extra edges, even across many loop cycles. This confirms the
  block-chopped lock-in reference scheme (SR830 external reference driven
  by CH1's Sync BNC) is viable in principle -- the per-segment
  `marker_mode` field does toggle once per block, not once per repeat, as
  hoped. The fallback (a single big pre-concatenated arb per block with
  per-sample marker data) is NOT needed.
- **Confirmed real bug: `PHASe:SYNChronize` reliably kills all output**
  when issued after both channels are already configured/enabled and
  running under `TRIG:SOUR IMM`, in this continuous (non-burst, non-sweep)
  `DATA:SEQ`-based ARB setup. Reproduced twice, on two separate AWG
  connections, with the `FUNC:ARB:PTP` amplitude bug and the
  stale-arb-upload gap already accounted for -- so this is a real,
  independent incompatibility, not just a symptom of those other bugs (an
  earlier hypothesis that it was innocent turned out to be wrong). Do not
  use `PHASe:SYNChronize` with this sequence-table + `TRIG:SOUR IMM`
  combination on this firmware.
- **RESOLVED: CH1/CH2 startup alignment**, via the manual's documented
  "start a sequence on a trigger" technique (p.181) -- confirmed working
  on real hardware, duty cycle measured at 49.18% (target 50%, see below
  for why it's not exact). With independent `TRIG:SOUR IMM`
  self-triggering, each channel starts looping its sequence the instant
  its own `OUTPUT ON` is processed; since `OUTPUT1 ON`/`OUTPUT2 ON` are
  separate SCPI writes issued moments apart, CH2 landed at an uncontrolled
  point in the cycle relative to CH1 every time -- confirmed to vary from
  run to run ("depending on initial conditions/luck"). Dead ends tried
  first, all confirmed broken/inapplicable on this firmware, not just
  failed attempts:
  - `PHASe:SYNChronize`: reliably kills all output entirely when issued
    after both channels are configured/running under `TRIG:SOUR IMM`.
  - `TRIG:SOUR BUS` + `INIT:IMM:ALL` + a shared `*TRG` (targeting the
    whole continuous sequence directly, no anchor segment): worked once,
    didn't reproduce.
  - Root cause of why plain triggering never worked, confirmed in the
    manual: `TRIGger[1|2]:SOURce`/`LEVel`/`SLOPe` only apply to sweep and
    burst mode (p.138-139) -- they do nothing for a plain continuous
    `FUNC ARB` sequence with no burst/sweep enabled.
  - `BURSt:MODE GATed` (bypasses `TRIGger` entirely, watches the Ext Trig
    BNC level directly): works on a plain single arb, but errors with
    `-221 "Settings conflict; not able to burst this function"` on a
    multi-segment `DATA:SEQ` sequence -- contradicts the datasheet's
    capability table, which claims "Sequenced arbitrary" supports burst.
  - **The actual fix**: place a brief DC "anchor" segment (minimum 32 Sa
    on 33600 Series) at the START of each channel's sequence, marked
    `onceWaitTrig` (play once, then hold and wait for a trigger before
    advancing) -- this is a documented *sequence-level* trigger
    (segment-advance-on-trigger), completely distinct from the
    burst/sweep-only `TRIGger:SOURce` restriction above. Both channels get
    this same anchor; once each has played it (near-instantly, 32 Sa) and
    is sitting in the wait-for-trigger state, a single trigger event
    advances both into their real sequences at the same instant.
    - `TRIG:SOUR BUS` + `*TRG` produced no output at all for this
      mechanism (unclear why -- possibly `*TRG` doesn't drive
      sequence-advance triggering the same way it does burst/sweep;
      not fully root-caused). Switched to `TRIG:SOUR EXT` with a real
      external edge (Siglent SDG1062X, `sdg1062x.py`, wired into the
      rear-panel Ext Trig BNC) instead -- **this worked**, confirmed
      correct CH1-vs-CH2 relative timing on the scope.
    - **Critical subtlety, confirmed on real hardware: the external
      trigger must be a CONTINUOUS, fast signal, not a one-time edge.**
      When the sequence finishes its last segment, it wraps back to the
      FIRST segment (the anchor) to loop -- and since that's
      `onceWaitTrig`, every single lap needs a fresh trigger. A slow
      trigger (100 Hz, tried first) let the sequence blaze through one
      off+on cycle right after each edge, then sit frozen at the anchor
      (holding low) for most of the 10 ms until the next edge -- a badly
      asymmetric duty cycle, not a clean square wave. The external
      trigger frequency must be at or above the real block-cycle rate
      (`1 / (2 * BLOCK_US)`) so every wraparound is retriggered
      essentially immediately.
    - Even with a fast trigger, the duty cycle is never *exactly* 50%,
      because the external trigger free-runs asynchronously to the
      sequence's own wrap-around moment -- each lap picks up a random
      extra dead time of up to one trigger period waiting for the next
      edge, which always lengthens the "low" (anchor) portion. At 50 kHz
      (20us period) this was a visible few-percent skew; at 1 MHz (1us
      period, bounding the worst case to ~1.7% of a 60us block cycle) it
      measured 49.18% -- close enough not to matter, but not perfect by
      construction.
    - **Real design implication for `rabi.py`**: since `BLOCK_US` depends
      on `MW_US`, which is swept, the external trigger frequency needs to
      be reconfigured (or just set very high, e.g. several MHz,
      comfortably above every sweep point's own block rate) for the real
      sweep, not left at one fixed value.
    - **Anchor value must match each channel's own real "off"/rest level,
      not a generic value -- confirmed on real hardware.** Originally used
      a plain `0.0`-valued (normalized) anchor arb, shared conceptually
      between both channels. This is fine for CH1 (simple 0-centered
      `VOLTage` convention, where `0.0` normalized already is the "off"
      level `ch1_rep` itself uses), but wrong for CH2, which uses an
      asymmetric unipolar mapping (`VOLT 5.0`, `VOLT:OFFS 2.5`, i.e. 0-5V)
      where `0.0` normalized maps to the *midpoint* (2.5V), not the real
      "off" level (`-1.0` normalized -> 0V, matching `gate_off_rep`).
      Symptom: a brief (~32 ns) unwanted blip to 2.5V right after the
      MW-on block ends (when the sequence wraps to the anchor) and before
      `gate_off_rep` pulls it down to the true 0V off level -- exactly at
      the ZYSWA switch's control-voltage midpoint, i.e. potentially in an
      undefined switch state for that instant. Fixed by giving each
      channel its own anchor arb at that channel's actual rest level
      (`anchor_ch1` at `0.0`, `anchor_ch2` at `-1.0`) instead of reusing
      one shared `0.0`-valued anchor. Apply the same channel-specific
      anchor-level care in `rabi.py`'s real `setup_awg_sequences()`.
  - **Also fixed independently, still valid regardless of the above**:
    the marker lives on CH2's OWN sequence now, not CH1's (`highAtStart`/
    `lowAtStart` on CH2's segments, `OUTPut:SYNC:SOURce CH2` to switch the
    shared Sync/Marker BNC to source from CH2). Since there's only one
    physical Sync/Marker connector, shared between channels, this ties
    the reference signal directly to CH2's own internal sequence
    position -- always exactly aligned with CH2's real MW pulses by
    construction, independent of CH1/CH2 relative timing. This alone
    already fully solved lock-in reference validity even before the
    anchor/external-trigger fix above solved the separate (but still
    important, e.g. for `PRE_US`/`POST_US`'s RF-pickup-avoidance purpose)
    question of CH1-vs-CH2 relative timing.
- **Open item, still unverified**: interlock timing during a pulsed (not
  continuous) RF source. `read_reflected_power_dbm()`'s single-sweep
  approach (used everywhere else in this codebase) can't reliably land a
  reading inside a microsecond-scale MW pulse -- GPIB round-trip plus the
  analyzer's own sweep time are both far slower than that. `rabi.py`
  instead uses a new `HP8673H.read_max_hold_reflected_power_dbm()`
  (MAX HOLD over several full reference periods, guaranteeing it catches
  at least one real pulse at its peak somewhere in that window, at the
  cost of timing precision) checked only periodically
  (`interlock_check_interval`), not every sweep point, since each check
  now takes multiple reference periods instead of one quick sweep. Not yet
  validated against real hardware -- in particular, whether the check
  cadence is safe enough, and whether the MAX HOLD window's wall-clock
  cost (currently counted as time *in addition to* the settle wait, even
  though the pulse sequence is already running throughout the check) is
  worth optimizing away.
- **`settle_time_constants`'s old default (5.0) was NOT actually enough to
  settle the SR830's own filter, given the filter slope this codebase
  configures.** `setup_lock_in()` sets a 24 dB/oct filter (`set_filter_
  slope_db_oct(24)`), which is a 4-pole cascaded RC filter, not a single
  pole -- cascaded filters have a slower, S-shaped step response (delayed
  initial response before catching up), so "5 time constants" means very
  different things depending on filter order. Computing the actual
  cascaded-filter step-response formula
  (`1 - exp(-t/tau) * sum_{k=0}^{n-1} (t/tau)^k / k!`, the Erlang-CDF
  form, for n=4 poles): 5 TC only reaches **~73.5% settled** (a ~26.5%
  residual of the *previous* point's value still contaminating every
  reading) -- compare to a single-pole filter, where 5 TC already reaches
  ~99.3%. For reference: 1-pole needs ~5 TC for 99%, 2-pole ~7 TC, 3-pole
  ~9 TC, 4-pole (ours) ~9-10 TC for ~98-99%. This is a real, systematic
  bias, not just added noise -- and critically, it's NOT fixed by more
  averaging (`n_reps`), which is consistent with a 250-`n_reps` run
  (`rabi2`) not showing cleaner Rabi data than a 50-`n_reps` run
  (`rabi1`) despite 5x more averaging. **Fixed**: `settle_time_constants`
  default raised from `5.0` to `9.0` in both `cmd_run()` and
  `cmd_calibrate_phase()`. Worth re-running previous data (`rabi1`/
  `rabi2`/`rabi3`, all taken with the old insufficient default) with the
  corrected settle time before trusting their apparent decay/noise
  characteristics.
- **Why this under-settling issue may bite `rabi.py` harder than
  `cw_odmr_lock_in.py` even though both share the identical 24 dB/oct
  filter and the identical (old) `settle_time_constants=5.0` default.**
  `settle_time_constants` (the ~9-10 multiplier) is a pure filter-order
  property -- it only depends on the 4-pole cascade, not on the reference
  frequency -- so it's the same fixed ratio in both scripts. But
  `time_constant_s` itself is chosen relative to the reference *period*,
  and that period differs a lot between the two: `cw_odmr_lock_in.py`
  chops at a fixed `chop_freq_hz` (e.g. 1 kHz -> 1 ms period), while
  `rabi.py`'s block-based reference period is several ms and varies with
  `n_reps`/`mw_us`. With the shared `time_constant_s=0.1` (100 ms) value,
  that's ~100 reference cycles per time constant for CW-ODMR vs. only
  ~22-50 cycles per time constant for `rabi.py` -- CW-ODMR's tau was
  generous relative to its own reference rate. Since the *absolute* wait
  is `settle_time_constants * time_constant_s`, CW-ODMR's oversized tau
  means its old 5 TC wait (500 ms) may have amounted to enough real dwell
  time in practice to mostly converge, even though 5 TC is formally only
  73.5% settled in cascaded-filter terms -- the generous tau likely masked
  the shortfall. `rabi.py`'s tau sits much closer to its own reference
  period (less slack), so the same formal 73.5%-settled shortfall is more
  exposed and more likely to actually show up in the data. This is a
  plausible explanation, not proof, for why `rabi1`/`rabi2`'s data shows
  the effect while previously-collected CW-ODMR data (`static_new*`/
  `powerscan*`/`powerstudy*`, same old `settle_time_constants=5.0`
  default, intentionally NOT changed since that data is already
  collected) may still be fine -- it would need the same absolute-dwell-
  time-vs-reference-period analysis on that data to confirm either way.
- **`setup_awg_sequences()` used to reissue the AWG's output-stage config
  (amplitude `VOLT`, `OUTP:LOAD`, `TRIG:SOUR/SLOP/LEV`, `OUTPUT ON`,
  `OUTPut:SYNC:SOURce`) from inside the per-sweep-point loop, even though
  none of those values depend on `mw_us` -- a 250-point run reissued the
  identical output-stage settings 250 times. These are plausibly
  relay-switched internally (amplitude range, load impedance, and the
  shared Sync/Marker source are all things AWGs commonly implement with
  mechanical relays), unlike uploading arb waveform data
  (`SOUR:DATA:ARB`), which is a pure digital memory write with no analog
  output-stage involvement. Repeatedly re-triggering relays with no
  functional benefit risks wearing them out over many sweeps. **Tried,
  then REVERTED**: split the output-stage config out into a new
  `configure_awg_outputs()`, called ONCE before the sweep starts (and
  again after `awg.reset()`) instead of every point, leaving
  `setup_awg_sequences()` to only touch arbs/sequence/`FUNC:ARB` selection
  per point. On real hardware this did NOT reduce the relay clicking, and
  the resulting CH1/CH2 sequence looked wrong on the scope -- so the
  theory that VOLT/OUTP:LOAD/OUTPUT ON/SYNC:SOURce were the (sole) source
  of the clicking, and that they're safe to only set once, is not
  confirmed and may be flat wrong (e.g. the click could be coming from
  something inside setup_awg_sequences() that still runs every point --
  DATA:VOL:CLE, the arb uploads themselves, or DATA:SEQ -- not the
  output-stage block at all). Reverted `setup_awg_sequences()` back to
  reconfiguring the full output stage every call, matching the last
  confirmed-working behavior; `configure_awg_outputs()` was removed
  entirely. The relay-wear question is still open and unsolved -- next
  step, if revisited, should isolate which specific SCPI write(s) the
  click actually correlates with (e.g. by testing them one at a time on
  the bench) rather than assuming which category is the culprit.
- **`resonance_sweep()`'s fine sweep used to always center on the coarse
  sweep's dip**, which is right for `cw_odmr.py`/`cw_odmr_lock_in.py`'s
  `use_resonance_sweep` (the whole point there is finding f0 from the dip),
  but wrong for `rabi.py`'s pre-flight reflected-power check: `freq_hz` is
  already fixed and NOT being re-derived from this sweep (see `cmd_run()`'s
  docstring), so if the coarse dip lands somewhere other than `freq_hz`
  (resonance drift, a different nearby dip, etc.), the fine sweep/Q
  estimate ends up describing the wrong point instead of characterizing
  what's actually being driven. **Fixed**: added an optional
  `fine_center_hz` parameter to `resonance_sweep()` (`hp8673h.py`) --
  `None` (default) preserves the original dip-centering behavior for
  `cw_odmr.py`/`cw_odmr_lock_in.py` (neither passes it), while `rabi.py`
  now passes `fine_center_hz=freq_hz` so its pre-flight fine sweep is
  always centered on the frequency actually being driven.
- **Added a reflected-power readout at the real operating point.** The
  pre-flight coarse/fine sweep only ever runs at the low, safe
  `res_power_dbm` -- it never actually checks reflected power at the real
  `drive_power_dbm` that the tau_mw sweep is about to use. `cmd_run()` now
  reads `HP8673H.read_reflected_power_dbm(ilock_sa, freq_hz)` right after
  the generator is set to `freq_hz`/`drive_power_dbm` and RF is turned on
  (CH2 is still statically routed to the sample path from
  `set_switch_static()`, not chopping yet, so the signal is CW and a
  single-sweep read is enough -- no need for `read_max_hold_reflected_
  power_dbm()`'s pulsed-signal handling, which only matters once the
  per-point loop starts gating). **Bug found on real hardware**: this
  readout was print-only and never actually enforced `threshold_dbm` --
  confirmed live when a `0.61 dBm` reading (well over the default
  `threshold_dbm=-10.0`, i.e. more power reflected than makes sense for a
  remotely-matched resonator) printed and the run just continued into the
  tau_mw sweep with no trip at all. Unlike the periodic in-sweep interlock
  check, which does call `gen.trip_interlock()` when `power_dbm >
  threshold_dbm`, this pre-flight readout was purely informational.
  **Fixed**: added the same threshold check + `gen.trip_interlock()` +
  `return` right after this readout, before the tau_mw sweep starts --
  mirrors the existing "spectrum analyzer unreachable at startup" early
  return a few lines above it, and runs inside the same `try` block so the
  existing `finally` cleanup (RF off, PSUs off, etc.) still executes.
- **Added a `fine_sweep` option to skip `resonance_sweep()`'s fine stage.**
  `HP8673H.resonance_sweep()` (`hp8673h.py`) gained a `run_fine_sweep=True`
  parameter -- when `False`, it runs only the coarse sweep, skips the fine
  sweep and its `estimate_q()`-based Q/FWHM estimate entirely, doesn't
  write `{output_prefix}_fine.csv`, and returns a smaller dict (just
  `coarse_freqs_hz`/`coarse_power_dbm`/`dip_freq_hz`/`dip_power_dbm`,
  plus `coarse_power_dbm_cal` if calibrated) instead of the full
  `estimate_q()` result. Default `True` preserves existing behavior for
  `cw_odmr.py`/`cw_odmr_lock_in.py` (neither passes it). `rabi.py`'s
  `cmd_run()` exposes this as a new `fine_sweep=true` (default) key=value
  override, independent of `reflected_power_scan` (which gates the whole
  pre-flight coarse+fine sweep on/off) -- `fine_sweep=false` keeps the
  quick coarse sanity check around `freq_hz` but skips the slower fine
  stage. `rabi_result.ipynb`'s pre-flight plot cell (`eb08ea9d`) updated
  to handle a missing `_resonance_fine.csv` gracefully (shows the coarse
  panel with a "fine sweep skipped" placeholder instead of treating the
  whole run as having no resonance CSVs at all).
- **Found a spurious lock-in signal that isn't real ODMR contrast.**
  Real-hardware testing found a signal off the actual NV resonance
  frequency (`rabi_off_resonance` at 2.26 GHz, R even LARGER than an
  on-resonance run's -- see the `rabi8` vs. `rabi_off_resonance` R
  comparison), and separately found the signal persists even with the
  amplifier's output routed into a dummy load instead of near the diamond
  (no MW field reaching the sample/resonator at all). The SAME signal
  disappears when the PMT's optical path is physically blocked. Together:
  it's a genuine optical signal (needs real light hitting the PMT, so it's
  not electrical pickup straight into the PMT cable) that doesn't require
  actual MW-NV coupling (persists into a dummy load) -- i.e. NOT real spin
  physics. Leading hypothesis: electrical crosstalk between the
  MW-switching side (CH2/amplifier/switch) and the laser/AOM drive side
  (CH1), modulating the ACTUAL laser intensity in sync with the same block
  reference used for demodulation, regardless of where the RF power ends
  up (shared power supply loading, AWG channel-to-channel crosstalk, or
  RF pickup on the AOM driver line are all plausible mechanisms). This
  casts doubt on whether ANY of `rabi1`-`rabi8`'s apparent contrast is
  genuine NV physics rather than this artifact -- needs to be ruled out
  before trusting that data. **Added `cmd_run_no_mw()`/`run-no-mw`
  command** to help isolate it -- a fully SEPARATE, self-contained
  function (not a flag/wrapper on `cmd_run()`), which never connects to
  or commands the HP8673H generator or spectrum-analyzer interlock at
  all. It runs the same AWG sequences and CH2 gate/switch toggling and the
  same tau_mw sweep/lock-in reads as `cmd_run()`, but with no generator
  object in the picture whatsoever -- the user turns the generator's RF
  off (or disconnects it) themselves beforehand, deliberately avoiding any
  reliance on this code to manage RF state correctly (a real concern:
  `gen.preset()` resets the instrument and its RF-on/off state after
  preset was never actually verified, so a flag-based approach inside
  `cmd_run()` risked `preset()` silently re-enabling RF regardless of the
  flag). `freq_hz`/`drive_power_dbm` are accepted only as labels recorded
  in the log/metadata, never sent to any instrument. Saves the same
  `{file_name}_rabi_*` file set as `cmd_run()` (with `_rabi_reflected_
  dbm.npy` all-NaN, no SA involved) so it loads into `rabi_result.ipynb`
  the same way. **Confirmed on real hardware: the spurious signal
  persists even with the generator's RF confirmed off** -- CH2 was still
  toggling the switch/amplifier chain, pointing at CH2's switching action
  itself (AWG crosstalk, or the switch/amplifier drawing current even
  with no RF applied) rather than real RF power as the mechanism.
  **Follow-up**: `cmd_run_no_mw()` extended to also never connect to or
  command the SPD1168X amplifier supply or SPD1305X coil supply -- same
  reasoning as the generator (turn both off/disconnect them yourself
  before running), testing whether the effect depends on either supply
  being powered at all, or persists even with them off too (which would
  point at something in the AWG/lock-in/PMT chain itself, independent of
  every piece of RF-side hardware). **Confirmed on real hardware: the
  spurious signal persists with the generator's RF AND both PSUs off** --
  the only things still active at that point are the AWG (CH1 laser/AOM
  drive, CH2 switch-control/marker), the SDG1062X external trigger, and
  the SR830 lock-in. Turning off the AOM's own RF driver (and the AOM)
  also kills the signal, but that's expected and not a new distinguishing
  clue -- disabling the AOM stops ALL light from reaching the PMT at all
  (equivalent to the earlier optical-block test), so it doesn't say
  anything about WHERE in the chain the artifact originates, only that it
  (like everything) needs light to exist in the first place.

  This narrows the culprit to the AWG itself -- CH1/CH2 channel-to-channel
  crosstalk (internal to the Keysight 33600A, or on the CH1/CH2 cabling/
  grounding) -- since CH2 is still physically toggling its own output
  voltage every block regardless of what's powered downstream. Notably,
  CH2 is ALSO the source of the lock-in's own reference (`OUTPut:SYNC:
  SOURce CH2`), so the demodulation reference and the hypothesized source
  of crosstalk are generated by the same physical instrument -- if the
  crosstalk is genuinely AWG-internal, it's structurally inseparable from
  this measurement architecture as long as CH2 supplies the reference.

  **Added `cmd_run_ch2_constant()`/`run-ch2-constant` command** to test
  the next specific question: does the artifact need CH2's own ANALOG
  output to physically switch, or only need the marker/reference to
  toggle? The marker (`highAtStart`/`lowAtStart`) and a segment's analog
  waveform value are independent `DATA:SEQ` attributes -- normally they
  change together, but don't have to. `setup_awg_sequences()` (`rabi.py`)
  gained a `ch2_hold_constant` parameter -- when `True`, CH2's "on"
  segment is reassigned to use the SAME constant,
  physically-off `gate_off_rep` arb as the "off" segment (normalized
  `-1.0` -> `0V` given `ch2_offset_v=2.5`), so CH2's real output voltage
  never changes at all, while its marker flag stays `highAtStart` as
  usual -- the lock-in still gets an identical, correctly-toggling
  reference. If the spurious signal disappears with CH2 truly held
  constant, that confirms it depends on CH2's own analog switching
  action; if it persists, the culprit is the marker/Sync output
  circuitry itself (or elsewhere), not CH2's analog DAC/switch-driving
  output. Refactored `cmd_run_no_mw()`'s body into a shared
  `_run_no_mw_impl(file_name, ch2_hold_constant, ch1_hold_constant, **kw)`
  so all three diagnostic commands reuse the same sweep logic without
  duplicating it, while still remaining separate top-level commands per
  the user's preference (not a flag on `cmd_run()` itself).

  **Confirmed on real hardware: the spurious signal persists even with
  CH2 held constant.** This rules out CH2's own analog output/switch-
  driving voltage entirely -- the only thing left toggling in that test
  was CH2's marker (`highAtStart`/`lowAtStart`, routed to the lock-in's
  reference via `OUTPut:SYNC:SOURce CH2`). But CH1's OWN sequence table
  ALSO couldn't be ruled out yet: even with `ch2_hold_constant`, CH1 still
  transitioned between two separately-listed "rep" segments (both marked
  "maintain", so nothing visibly changed on CH1's output) at exactly the
  same block boundary as CH2 -- an internal segment-advance event on
  CH1's OWN sequencer that's synchronous with the block reference,
  independent of anything CH2 does. **Added `ch1_hold_constant` to
  `setup_awg_sequences()`** -- when `True`, CH1 is reconfigured as a
  plain, non-sequenced continuous `FUNC SIN` at 80 MHz / `ch1_vpp`
  (no `DATA:SEQ` at all for CH1, so no sequence table to advance through,
  and therefore no internal segment-transition event on CH1 whatsoever --
  doesn't need `TRIG1:SOUR EXT` either, since a plain continuous function
  ignores `TRIG:SOURce`, unlike burst/sweep). **Added
  `cmd_run_ch1_ch2_constant()`/`run-ch1-ch2-constant` command**, building
  on `cmd_run_ch2_constant()` with `ch1_hold_constant=True` too -- with
  both channels' analog outputs and sequence tables now fully decoupled
  from the block structure, the ONLY thing anywhere in the AWG still
  synchronous with the block reference is CH2's marker/Sync output. If
  the spurious signal disappears here, the mechanism needed CH1's own
  sequencer transition; if it persists, that isolates the marker/Sync
  line itself (or something further upstream, e.g. the SDG1062X external
  trigger, or the lock-in's own reference input circuitry) as the sole
  remaining candidate. Not yet run on real hardware to confirm.
- **Found a real confound in every sweep run so far, including the
  constant-hold diagnostics above: the SDG1062X external trigger
  frequency changes monotonically across every sweep, tracking tau_mw.**
  `_configure_external_trigger(sdg, ref_period_s, margin=trigger_margin)`
  is called every point with `ref_period_s = 2 * n_reps * rep_us`, and
  `rep_us = laser_us + pre_us + mw_us + post_us` grows with `mw_us` --
  none of `ch2_hold_constant`/`ch1_hold_constant` touch this, since a
  segment's LENGTH (unlike its analog value or marker) still depends on
  `mw_us` regardless. So even with `run-ch2-constant`'s CH2 output truly
  unchanging, the external trigger's frequency (`margin/ref_period_s`) is
  still monotonically decreasing across the sweep -- meaning something
  was still varying with tau_mw even in that "everything held constant"
  test. Since a monotonic, decaying R vs. tau_mw trend was observed even
  there, this raised the question of whether that trend reflects
  anything about tau_mw at all, or is just an artifact of the trigger
  frequency happening to be a monotonic function of tau_mw in every run
  collected so far (the two are not distinguishable from any of the data
  gathered to date). **Fixed, for `cmd_run_ch2_constant()`/
  `run-ch2-constant` only (not yet applied to `cmd_run()` or the other
  two constant-hold diagnostics)**: `_run_no_mw_impl()` gained a
  `fixed_external_trigger` parameter -- when `True`, the SDG1062X's
  trigger frequency is configured ONCE before the sweep starts (derived
  from `mw_start_us`, the sweep's shortest rep and therefore its largest
  bare-minimum trigger requirement -- comfortably fast enough for every
  longer rep later in the sweep too, per the existing `trigger_margin`
  safety-factor reasoning) instead of being recomputed every point. If
  the decaying R vs. tau_mw trend disappears once the trigger frequency
  stops varying, that confirms it was a trigger-frequency artifact, not
  a tau_mw-dependent one. **Result on real hardware**: with the trigger
  fixed, the trend didn't disappear -- it changed shape, from a smooth
  decay to an oscillation. Consistent with a beat/aliasing effect: CH1's
  own segment-transition still happens once per block (governed by
  `rep_us`, which still varies with `mw_us`), but now the external
  trigger period is fixed and no longer locked to it -- previously the
  two were always in a fixed ratio (smooth trend), now their relative
  phase sweeps through different alignments as `mw_us` changes
  (oscillation), rather than tracking a fixed relationship.

  **This, combined with the CH1/CH2-constant diagnostics above, points
  conclusively at CH1's OWN internal sequence-table segment transition as
  the artifact's source, independent of CH2 entirely.** Confirmed on real
  hardware: `run-ch1-ch2-constant` (both channels held constant, CH1 as a
  plain unsequenced continuous tone with no DATA:SEQ at all) shows NO
  signal, while `run-ch2-constant` (CH1 still running its normal two-
  listing sequence, even though both listings are identical "maintain"
  content) still shows one. The AWG's sequencer evidently produces a
  real, synchronous transient on CH1's own analog output whenever it
  advances between listed segments, REGARDLESS of whether the content
  differs -- and `cmd_run()`'s real CH1 sequence has exactly this
  structure (two separate `n_reps` listings of the identical "rep" arb,
  present purely to mirror CH2's on/off block split, which CH1's own
  content never actually needed). This means every real Rabi run
  collected so far (`rabi1`-`rabi8`, `rabi_off_resonance`, etc.) likely
  carries this same internal artifact on top of (or dominating) whatever
  real ODMR signal might be present.

  **First attempted fix (collapsing the two listings) did NOT eliminate
  it on real hardware** -- confirmed the mid-sequence transition between
  the two `n_reps` listings wasn't the real cause. What's left, and what
  actually differs from the no-signal `run-ch1-ch2-constant` case: CH1's
  sequence still had an anchor segment marked `onceWaitTrig`, meaning it
  still waited on and reacted to the EXTERNAL TRIGGER EDGE (from the
  SDG1062X into the AWG's Ext Trig BNC -- yet another shared physical
  connector) once per full block cycle to wrap back around. `run-ch1-
  ch2-constant`'s plain `FUNC SIN` has no trigger dependency at all. So
  the real distinguishing variable isn't listing count, it's whether CH1
  reacts to the external trigger edge in any way. **Fixed (second
  attempt), for `ch2_hold_constant` only**: `setup_awg_sequences()`'s CH1
  sequence now drops the anchor/`onceWaitTrig` entirely when
  `ch2_hold_constant=True` -- a single `"repeat"` segment (`2 * n_reps`
  reps of `"rep"`) plays immediately on `OUTPUT1 ON` and never needs
  retriggering, fully decoupling CH1 from the external trigger while
  still playing its real RF-pulse waveform (unlike `ch1_hold_constant`'s
  plain sine). Safe specifically for this diagnostic because CH2 isn't
  gating anything real anymore, so CH1/CH2 relative timing doesn't
  matter here. `TRIG1:SOUR/SLOP/LEV` writes are skipped too in this case
  (meaningless without a trigger-gated segment). `cmd_run()`'s normal
  path (and `cmd_run_no_mw()`) still use the original onceWaitTrig-gated
  structure, which IS load-bearing there for real CH1/CH2 synchronization
  -- this fix is diagnostic-only for now. **Confirmed on real hardware:
  the spurious signal disappeared** with CH1 fully decoupled from the
  external trigger.

  The mechanism, once understood: it's not electrical crosstalk at all --
  it's a genuine, physical dip in the LASER OUTPUT ITSELF. Every time
  CH1's sequence wraps back through its `onceWaitTrig` anchor (once per
  full off+on cycle), CH1 drops to the anchor level (no RF driving the
  AOM) and sits there for a small but real, asynchronous dead time before
  the next available trigger edge arrives and it resumes real pulsing --
  the same dead time already measured and documented early on as a
  duty-cycle imperfection (49.18% vs. an exact 50%). This dip lands at
  ONLY ONE of the two transitions per cycle: the sequence order is
  `anchor -> gate_off_rep (xN) -> gate_on_rep (xN) -> [wrap through
  anchor] -> gate_off_rep -> ...`, so the anchor-wait glitch contaminates
  only the very START of the "off"/low phase every cycle; the "off" to
  "on" transition mid-cycle is a plain `"repeat"`-to-`"repeat"` handoff
  with no trigger involved and no glitch. Because the artifact only ever
  lands on ONE side (not both), it doesn't cancel between the two halves
  the way a symmetric artifact would -- it demodulates out as a real,
  nonzero, synchronous "signal" indistinguishable from genuine contrast.

  **Follow-up: reduce (not just relocate) the glitch's impact by making
  it recur less often.** Since only `onceWaitTrig`-marked segments need a
  trigger -- a plain `"repeat"` segment loops via its own internal count
  with zero trigger dependency -- a channel's sequence can list multiple
  off+on (or "rep") cycles in the table before wrapping back to its
  anchor, at near-zero cost (each listing just references the SAME
  already-uploaded arb data by name, not new waveform memory).
  `setup_awg_sequences()` gained `anchor_free_reps` (default `1`, i.e.
  unchanged behavior) -- when higher, that many cycles are listed before
  each channel's anchor, so the trigger-wait glitch recurs once every
  `anchor_free_reps` cycles instead of every cycle.

  **Bug found and fixed**: `anchor_free_reps` was initially applied to
  CH2 only, while CH1 either kept its own anchor wrapping every single
  cycle, or (in `run-ch2-constant`, which had separately dropped CH1's
  anchor entirely to eliminate the artifact for THAT diagnostic) had no
  periodic structure at all. Either way, CH1 and CH2 no longer wrapped
  through their anchors together -- confirmed on real hardware as visible
  CH1/Sync misalignment on a scope. **Fixed**: `anchor_free_reps` now
  applies uniformly to BOTH channels' sequences (when neither is held
  constant/free-running via `ch1_hold_constant`/`ch2_hold_constant`),
  keeping them wrapping through their own anchors in lockstep at the same
  cadence, exactly like the original single-cycle design just less
  often. This also generalizes `anchor_free_reps` to normal, non-
  diagnostic operation -- it's no longer tied to `ch2_hold_constant` at
  all, so it can be applied directly to `cmd_run()`'s real sequence
  (though `cmd_run()` itself doesn't pass it yet -- only `cmd_run_
  ch2_constant()` defaults it to `20`). Added `tests/rabi_anchor_free_
  reps_test.ipynb` (using the real `setup_awg_sequences()`/
  `_configure_external_trigger()`, neither channel held constant) to
  inspect this directly on a scope before trusting it in a real sweep.
  **Confirmed CH1/CH2 stay aligned on real hardware** when the trigger is
  slowed down enough to see individual pulses.

  **Found the actual sequence-table ceiling on real hardware: ~250
  off+on-pair listings** (`VisaIOError`/`VI_ERROR_TMO` at 20000 listings;
  50 worked). Consistent with the ~512-sequence-steps-per-channel spec
  estimated for this AWG series (`512 - 1 anchor, /2 per pair ~= 255`).
  Since `n_reps` (the repeat COUNT inside each listed segment) doesn't
  consume extra table steps -- only the number of listed off+on PAIRS
  does -- this ~250-pair ceiling carries over unchanged to real production
  `n_reps=250`. At real parameters, one off+on cycle is `500 * (4 +
  mw_us)` us (~2.0-4.5 ms across the sweep); 250 cycles covers ~0.50 s
  (short `mw_us`) to ~1.13 s (long `mw_us`). Compared to `settle_s`
  (dominated by the `settle_time_constants * time_constant_s = 0.9 s`
  floor across essentially the whole sweep): this doesn't fully eliminate
  the trigger-wait glitch at the very shortest `mw_us` (covers ~0.5s of
  the ~0.9s window, so ~1 retrigger still occurs there, down from ~450
  today), but fully eliminates it (zero retriggers during the read) for
  the majority of the sweep where per-cycle duration is longer.

  **Applied to `cmd_run()`'s real (non-diagnostic) path.** New
  `anchor_free_reps` key=value override, default `200` (a bit of headroom
  under the observed ~250 ceiling, not run right up against it).
  `_configure_external_trigger()` is now given the FULL `anchor_free_reps`
  -scaled period (`ref_period_s * anchor_free_reps`), not a single
  cycle's period, since the trigger now only needs to keep up with the
  much-less-frequent anchor-wrap rate. `trigger_margin`'s default also
  lowered from `100` to `3.0` -- the large margin was compensating for
  the trigger's own asynchronous dead time being a meaningful fraction of
  a SHORT single cycle (visible duty-cycle skew); with anchor-wraps now
  spaced out over a much longer stretch, that same dead time is a tiny
  fraction of the period, so a modest margin is "just enough" without
  needlessly fast triggering.

  **Not yet re-validated**: `RESEQUENCE_INTERVAL` (still `20`) was tuned
  against the OLD, much smaller per-point sequences (3 listed segments)
  and a DIFFERENT AWG resource (total distinct sequence names
  accumulated across points) than the one `anchor_free_reps` bumps into
  (total listed segments within ONE sequence) -- each point's sequence is
  now much larger (up to `2*anchor_free_reps+1` ~= 401 segments), so it's
  not yet confirmed whether 20 points' worth of these larger sequences
  still fits before hitting a real limit. Watch for errors here on real
  hardware; may need retuning. Also not yet run on real hardware at all
  with `anchor_free_reps` applied to `cmd_run()` specifically (only the
  standalone notebook and the `run-ch2-constant` diagnostic have been
  tested so far).
- **`cmd_run()`'s external trigger was still recomputed every point** --
  scaled by `anchor_free_reps` (better than before), but still a function
  of the current `mw_us`, so not actually constant across the sweep the
  way `cmd_run_ch2_constant()`'s `fixed_external_trigger` already was.
  **Fixed**: added the same `fixed_external_trigger` option to `cmd_run()`
  (default `true`) -- when set, the SDG1062X trigger is configured ONCE
  before the sweep starts, derived from `mw_start_us` (the sweep's
  shortest rep, hence its largest bare-minimum trigger requirement --
  comfortably fast enough for every longer rep later in the sweep too),
  instead of being recomputed every point. Set `fixed_external_trigger=
  false` to restore the old per-point recomputation. Note: `rabi_new`/
  `rabi_new1` (the first runs collected under the `anchor_free_reps` fix)
  were taken BEFORE this trigger-fixing change, so their external trigger
  still varied point-to-point (scaled by `anchor_free_reps`, but not
  constant) -- worth keeping in mind if re-analyzing them, though this is
  a much smaller effect than the background artifact those runs already
  fixed. Not yet run on real hardware to confirm.
- **`rabi1` through `rabi9` (all data collected before the
  anchor_free_reps/trigger fixes above) are NOT reliable Rabi
  measurements and should not be used or reanalyzed as such.** All of
  that data was taken with `anchor_free_reps=1` (the original, unfixed
  behavior) -- meaning every single off+on cycle went through the
  `onceWaitTrig` anchor's trigger-wait dead time, producing the
  synchronous background artifact described above on every cycle. Given
  the diagnostic testing found this background artifact persists with
  the generator RF off, both PSUs off, and CH2 held constant -- i.e. it
  dominates or entirely constitutes the measured signal under those
  conditions -- there's no way to separate out how much (if any) of
  `rabi1`-`rabi9`'s apparent contrast/decay was genuine NV physics versus
  this artifact. Treat all of it (`rabi1`-`rabi5`, `rabi6_50ohm`,
  `rabi7_50ohm`, `rabi8`, `rabi9`) as measuring the background, not real
  ODMR/Rabi signal. `rabi_result.ipynb` (built to plot/compare this data)
  has been deleted for the same reason -- re-collect fresh data with the
  anchor_free_reps fix applied before trusting any Rabi oscillation
  analysis again. (The various `rabi_off_resonance*`/`no_mw*` runs are
  exempt from this caveat -- those were themselves the diagnostic probes
  that found this artifact, not intended as real physics measurements.)
- **Found and fixed a real overload/underload sensitivity-bounce
  oscillation, distinct from the background artifact above.** FFT
  analysis of `rabi_new2_fix_trigger`/`rabi_new3_fix_trigger`/`rabi_new4_
  fix_trigger` (collected after the anchor_free_reps/trigger fixes) still
  showed a peak near each run's own Nyquist frequency -- but UNLIKE the
  earlier `rabi1`/`rabi2`/`rabi3` case (near-zero lag-1 autocorrelation,
  consistent with finite-sample white noise randomly peaking anywhere)
  and the `rabi_off_resonance_fix_long` noise-floor control (also
  near-zero, 0.054), these three runs showed strongly NEGATIVE lag-1
  autocorrelation (-0.19 to -0.60) -- genuine point-to-point alternation,
  which by construction sits exactly at Nyquist frequency, not a
  coincidental noise peak. Root cause: the existing overload/underload
  logic reacts to a SINGLE reading each way -- `OVERLOAD` coarsens
  sensitivity, then the very next point's reading (now measured at that
  coarser range) satisfies `auto_rescale_on_underload`'s margin check and
  immediately reverts back to the finer range that just overloaded, which
  then overloads again next point, repeating indefinitely -- exactly a
  period-2 bounce. **Fixed**: added `underload_persistence` (default `3`)
  -- the underload condition must now hold for that many CONSECUTIVE
  points before actually stepping to a finer range, acting as a low-pass
  filter on the decision (a one-off reading right after an overload-
  forced coarsening won't immediately trigger a revert, since subsequent
  points must also independently confirm it). Also: any point where an
  `OVERLOAD` actually triggered a coarsening this same point resets
  `underload_streak` to 0 unconditionally (via a new `overloaded_this_
  point` flag), so a point still recovering from an overload can never
  itself count toward the persistence requirement, even if it happens to
  look "underloaded" relative to its newly-coarsened range. Applied to
  both `cmd_run()` and `_run_no_mw_impl()` (shared by `cmd_run_no_mw()`/
  `cmd_run_ch2_constant()`/`cmd_run_ch1_ch2_constant()`). Not yet run on
  real hardware to confirm the oscillation is actually gone (check via
  the same lag-1-autocorrelation test used to find it). **Confirmed on
  real hardware**: `rabi_new5_fix_overload`'s lag-1 autocorrelation
  flipped from strongly negative (-0.19 to -0.60 in the pre-fix runs) to
  positive (0.324) -- the period-2 bounce signature is gone, though not
  yet as close to zero as the noise floor's own 0.054. (Also checked
  lag-3 across all runs as a second check -- it sits around -0.2 to -0.3
  for several runs regardless of whether they ever showed the lag-1
  bounce, including ones that never had it, while the noise floor itself
  is +0.150 -- no clean separation there, consistent with lag-3 being
  dominated by sampling variance at these modest sample sizes (n=20-100)
  rather than tracking a real periodic effect. A genuine period-2 bounce
  shows its signature at lag-1 specifically, not lag-3, so this doesn't
  undermine the lag-1 result.)
- **Added `fixed_sensitivity` convenience override** (`cmd_run()` and
  `_run_no_mw_impl()`) -- when `true`, forces `auto_sensitivity`/
  `auto_rescale_on_overload`/`auto_rescale_on_underload` all to `False`
  regardless of their own individual settings, using `sensitivity_v` as-
  is for the entire sweep with no AGAN and no rescaling at all. Useful
  for isolating whether auto-rescaling itself (even with the
  `underload_persistence` fix above) still contributes noise/artifacts,
  by comparing directly against a run with sensitivity held completely
  fixed throughout. Since `auto_sensitivity` is forced off, `sensitivity_
  v` must be picked manually (e.g. from a previous auto_sensitivity run's
  logged range) rather than relying on AGAN to choose it.
  **Bug found on real hardware, fixed**: `fixed_sensitivity=true
  sensitivity_v=1e-4` still rescaled. Root cause: the override block was
  placed right after `auto_rescale_on_overload`/`max_rescale_attempts`
  were parsed, but BEFORE `auto_rescale_on_underload`'s own `kw.get(...)`
  line further down -- that line ran unconditionally afterward and
  silently reset `auto_rescale_on_underload` back to `True`, undoing half
  the override (auto_sensitivity and auto_rescale_on_overload stayed
  correctly disabled since nothing reassigned them again, but underload
  rescaling kept happening). Fixed by moving the override block to AFTER
  all three flags (and `underload_margin`/`underload_persistence`) are
  parsed, in both `cmd_run()` and `_run_no_mw_impl()`.
- **Solved the ~250-listing sequence-table ceiling properly, instead of
  just capping `anchor_free_reps` under it.** Investigated the actual
  Keysight 33500/33600 manual's `<marker mode>` options for `DATA:SEQ`
  segments: `maintain`, `lowAtStart`, `highAtStart`, and
  `highAtStartGoLow` ("force marker high at start of segment and then low
  at marker position", where `<marker point>` is a sample index into the
  arb, required to be in `[4, N-3]`). This 4th mode makes it possible to
  bake a FULL off+on cycle (`n_reps` copies of on-content followed by
  `n_reps` copies of off-content) into ONE combined arb, with
  `marker_point` set to the sample where off-content begins, instead of
  needing two separate listings (one per half) each relying on the
  segment's own `repeat_count` field. Open question before implementing:
  does `highAtStartGoLow`'s assert-then-negate pattern re-fire on EVERY
  repeat of a `"repeat"`-type segment (needed for this to work), or only
  once across the whole multi-repeat block (the previously-confirmed
  behavior for the simpler `highAtStart`-only mode, which doesn't
  generalize automatically since it's a different mechanism -- a hold-
  only mode has nothing to "redo" per repeat, while a sample-relative
  negate point plausibly resets with each repeat's own sample counter).
  **Confirmed on real hardware** (`tests/rabi_combined_arb_marker_
  test.ipynb`, standalone, didn't touch `rabi.py`): it DOES re-fire every
  repeat, correctly reproducing the HIGH/LOW block pattern
  `anchor_free_reps` times over -- tested up to 1000 repeats (~60 ms) at
  small `N_REPS=5`, and separately confirmed the arb upload itself works
  fine at real production size (`n_reps=500`).

  **Applied to `setup_awg_sequences()`, replacing the old two-listing-
  per-cycle design entirely** (not just adding a variant): both CH1
  ("ch1_combined": `2*n_reps` copies of the same "rep" content, one
  listing, `maintain` marker) and CH2 ("ch2_combined": `n_reps` on-copies
  + `n_reps` off-copies, one listing, `highAtStartGoLow` with computed
  `marker_point`) are now built once and listed with `repeat_count=
  anchor_free_reps` -- a SINGLE table entry regardless of how large
  `anchor_free_reps` is, exactly like `n_reps` itself already was free.
  This removes the ~250-listing ceiling entirely (confirmed: no longer
  capped by table size at all) -- the tradeoff moves from "table entries"
  to "waveform memory / per-point upload time" instead, since the
  combined arb is `n_reps` times bigger than a single rep (confirmed
  fine on real hardware up to `n_reps=500`). This also means
  `anchor_free_reps` can now be set large enough to make one combined-arb
  stretch reliably exceed `settle_s` at EVERY `mw_us` in the sweep
  (unlike the old ~250-listing cap, which fell short at the shortest
  `mw_us` values) -- potentially eliminating the anchor-wrap dead-time
  glitch from landing inside any point's read window at all, not just
  reducing how often it does.

  **Bug found and fixed along the way**: an earlier edit (unifying
  `anchor_free_reps` across both channels) had removed CH1's old
  "decoupled from trigger when `ch2_hold_constant`" special case from the
  sequence-BUILDING code, but left a stale `if not ch2_hold_constant:`
  guard around the `TRIG1:SOUR/SLOP/LEV` writes in the output-CONFIG
  code -- meaning CH1's (now-always-present) `onceWaitTrig` anchor could
  end up with no trigger source configured when `ch2_hold_constant=True`.
  Fixed by making CH1's trigger configuration unconditional, matching the
  fact its sequence always has an anchor now regardless of
  `ch2_hold_constant`.

  `cmd_run_ch2_constant()`'s `anchor_free_reps` default (`20`) predates
  this fix and is now more conservative than structurally necessary, but
  wasn't changed since there's no urgency. Docstrings updated throughout
  to describe the combined-arb design instead of the retired two-listing
  one. Not yet run on real hardware with this applied to a full `cmd_run()`
  sweep at real parameters (only the standalone notebook and isolated
  upload timing test have been confirmed so far).

  **`RESEQUENCE_INTERVAL` lowered from `20` to `4`.** Flagged above as not
  yet re-validated -- now addressed. The combined-arb design bakes
  `n_reps` (up to hundreds) copies of on/off content into ONE waveform per
  channel per point, so each point's arb data is orders of magnitude
  bigger than the old per-rep arbs, even though the *sequence table* itself
  is now tiny (2 listed segments instead of up to ~401). `RESEQUENCE_
  INTERVAL`'s `awg.reset()` was originally tuned only against the
  "too many sequences defined" limit (accumulated distinct sequence names
  across points), a concern from the old design that's now much less
  pressing. The new concern it also needs to cover is accumulated/
  fragmented ARB WAVEFORM MEMORY across many large uploads -- each point's
  `SOUR1:DATA:VOL:CLE`/`SOUR2:DATA:VOL:CLE` calls should free the previous
  point's arb before uploading the new one, but with per-point arbs this
  much larger, a periodic full reset every 4 points (vs. every 20) gives
  the AWG's memory management a cleaner slate more often as a margin of
  safety. Not yet stress-tested on real hardware at this new interval;
  revisit if upload errors or slowdowns appear over a long sweep.
- **SR830 sensitivity auto-rescaling was one-directional: only coarser,
  never finer again.** `_step_sensitivity_coarser()` + `auto_rescale_on_
  overload` back out of a real-time `OVERLOAD` by stepping to a LESS
  sensitive range, but nothing complementary ever steps back to a MORE
  sensitive range once the real signal has settled to a small fraction of
  whatever range an earlier transient/overload left it pinned at --
  `auto_gain()` only runs once, at the very start of a sweep. Symptom seen
  on real hardware: sensitivity pinned at `1.000e+00 V` (the single
  coarsest entry in `SR830.SENSITIVITY_V`) for the rest of a run, with
  `_step_sensitivity_coarser()`'s rescale attempts becoming no-ops
  (`old_v == new_v`, nowhere coarser to go) on later spurious `OVERLOAD`
  hits, while the real signal was almost certainly much smaller than that
  range's full scale by then -- under-resolving X/Y against the SR830's
  own noise/quantization floor at that range, a plausible extra
  contributor to `rabi1`/`rabi2`/`rabi3`'s noise on top of the settle-time
  issue above. **Fixed in `rabi.py` only**: added `_step_sensitivity_
  finer()` (mirrors `_step_sensitivity_coarser()`, steps one range
  finer) and a new `auto_rescale_on_underload`/`underload_margin` check in
  `cmd_run()`'s per-point loop, run after the existing overload-rescale
  block -- if `R = sqrt(X^2+Y^2)` would still use less than
  `underload_margin` (default 0.5) of the NEXT finer range's full scale,
  step down one range for the following point. Checked after saving each
  point's reading (not urgent the way overload is), so it takes effect on
  the next point rather than paying an extra `settle_s` wait to re-read
  immediately. `cw_odmr_lock_in.py` has the identical one-directional gap
  (confirmed via its own `_step_sensitivity_coarser()`/`auto_rescale_on_
  overload`, same pattern) but was intentionally NOT changed here, per
  the standing instruction not to modify that script's behavior after
  data was already collected under it.
- **The overload-rescale logic above could overshoot in response to a
  stale/transient `OVERLOAD` flag, not a genuinely too-sensitive range.**
  Observed on real hardware: entering a point at 100 uV full scale, the
  first check reported `OVERLOAD`, coarsened to 200 uV, still reported
  `OVERLOAD`, coarsened again to 500 uV -- but the reading that finally
  came back clean was only `1.261e-05 V`, just 2.5% of that 500 uV range.
  A signal that size would never have overloaded even the original 100 uV
  range, so the first two `OVERLOAD` reports weren't describing the real
  steady-state signal. Root cause: `read_overload_status()`'s own
  docstring already flagged this -- the SR830's `LIAS?` status bits latch
  until read, so a brief transient (from the AWG being reprogrammed for
  this point's new mw_us/trigger rate, or from the `SENS` range switch
  itself causing a momentary input-stage transient) sets a bit that then
  reads back as "still overloaded" on the NEXT check, even once the
  signal has genuinely settled fine at the new range. This directly
  explains why the `auto_rescale_on_underload` fix above immediately
  stepped back down afterward -- the true signal was small the whole
  time; the coarsening was chasing a ghost. **Fixed**: added two
  discard-only `lia.read_overload_status()` calls (return value
  intentionally unused) to clear stale latched bits before they can be
  mistaken for a live overload -- one right after `setup_awg_sequences()`/
  `_configure_external_trigger()` for the point (clears any transient from
  reprogramming before the settle wait even starts), and one right after
  each `_step_sensitivity_coarser()` call inside the rescale loop (clears
  any transient the range switch itself produced before the next
  iteration's check). Adds one cheap GPIB query per point (and one per
  rescale attempt) -- negligible next to the existing `settle_s` waits,
  and doesn't change behavior at all when no overload ever occurs.
- **The two discard-reads above weren't enough -- spurious `OVERLOAD`s
  were still happening on EVERY point**, just as single-step bounces
  (coarsen once, read clean, then `auto_rescale_on_underload` immediately
  reverts back down) instead of the earlier double-step cascade. Real root
  cause: the discard right after reconfiguring the AWG only clears flags
  latched BEFORE the settle wait starts -- but `time.sleep(settle_s)`
  followed by a single overload check afterward (the code as it stood)
  catches ANY excursion during the ENTIRE settle window, including the
  expected transient right at the start as the demodulated output swings
  from the PREVIOUS point's steady value toward this one before the
  filter (needing ~9-10 TC to truly converge, see the settle-time fix
  above) actually settles. That transient is normal step-response
  behavior, not a real overload of the final reading -- but it happens on
  literally every point, since every point has that same value-to-value
  transition. **Fixed**: added `_wait_settle_discarding_transient_
  overload()`, which splits `settle_s` into two phases -- waits
  `transient_fraction` (default 0.8) of it, discards whatever overload
  flag latched during that portion (the expected transition swing), then
  waits the remainder before the real read/check. A genuine overload
  still present right up to the actual read (not just an artifact of the
  transition) still latches during that final portion and is still
  caught. Used both for the main per-point wait before `lia.read_xy()`
  and for the re-settle wait after each `_step_sensitivity_coarser()` call
  in the rescale loop.
- **The PMT-to-voltage load resistor (1 kOhm, ~0.5 m BNC cable) was
  checked for RC-bandwidth concerns and is NOT a significant issue at
  current settings.** Estimated stray capacitance ~80-100 pF (dominated by
  ~50 pF for 0.5 m of 50-ohm coax at ~100 pF/m, plus SR830's own 25 pF
  input capacitance, plus a few pF of PMT/socket capacitance) gives
  `tau = R*C ~= 100 ns`, rise time ~220 ns -- comfortably faster than the
  2 us laser pulse (settles to >99% within ~500 ns, about a quarter of the
  pulse) and irrelevant to `tau_mw` timing (the PMT only responds to the
  laser gate, not the MW pulse itself, so short `tau_mw` values were never
  actually something this circuit needed to resolve). This was checked
  because a slow PMT readout stage could in principle produce a
  monotonic, saturating-looking `R` vs. `tau_mw` curve for a purely
  instrumental reason, indistinguishable from the physical overdamped-
  Rabi explanation discussed below -- ruled out at these specific
  component values, though only estimated from typical cable/tube specs,
  not directly measured with a fast test pulse.
- **NV readout window vs. polarization window -- discussed, NOT changed.**
  Per NV-ODMR literature, spin-dependent PL contrast is highest in
  roughly the first ~300 ns of a readout laser pulse, then decays/
  saturates as continued illumination optically re-polarizes the NV back
  to `ms=0` regardless of starting state. `rabi.py`'s current CH1 sequence
  uses a single combined `laser_us` pulse (2.0 us) that does double duty
  across cycle boundaries -- its first ~300 ns reads out the PREVIOUS
  rep's MW result, the remaining ~1.7 us re-polarizes for THIS rep's
  upcoming MW pulse -- rather than separate, purpose-built polarize/
  readout segments. Initially considered this a likely source of
  contrast dilution needing a fix (either a dedicated readout-gate signal
  on the PMT side, or splitting CH1's arb into distinct polarize/readout
  segments), but on closer analysis the concern is smaller than it first
  seemed: the lock-in demodulates against the BLOCK-level (mw-on-block vs
  mw-off-block) reference, not anything finer within a single rep, so any
  part of the laser pulse that's genuinely identical between mw-on and
  mw-off reps (i.e. the late, fully-re-polarized portion) already cancels
  in the differential/synchronous detection on its own -- no gate or arb
  restructuring is strictly required for an unbiased MEAN contrast
  reading. The remaining real (but secondary) cost of the current
  single-pulse design is extra photon SHOT NOISE from the additional
  ~1.7 us of non-informative brightness reaching the PMT, which a
  dedicated readout gate (blocking that light from ever reaching the
  lock-in electronically) or a split polarize/readout arb structure could
  reduce. Decided NOT worth the implementation effort for now -- revisit
  if SNR remains a limiting factor after other fixes above are validated
  on real hardware.
- **Analysis of `rabi1`/`rabi2`/`rabi3`'s R data (taken before the settle-
  time fix above) found no convincing evidence of genuine Rabi
  oscillations yet** -- FFT of the detrended R data shows no low-frequency
  peak consistent across runs (each run's "loudest" frequency component
  sits near its own Nyquist limit and differs between runs, the signature
  of picking out the largest bin of white noise in a finite dataset, not
  a real shared oscillation frequency); lag-1 autocorrelation of the
  residuals is close to zero (-0.006/+0.025/-0.089), ruling out a
  systematic point-to-point alternating artifact as the explanation for
  the near-Nyquist FFT peaks. A plain (non-oscillating) exponential decay
  does fit `rabi1`/`rabi2` reasonably (R^2 0.74/0.44) with a similar time
  constant (~1.57-1.59 us) between the two independent runs -- suggestive
  of *something* reproducible as a function of tau_mw, but adding a
  decaying-cosine term never improved the fit in any of the three runs
  (frequency always collapsed to ~0), so there's no confirmed oscillatory
  (as opposed to monotonic-drift) component yet. Can't yet distinguish
  "real T2*/Rabi-decay envelope, oscillation just not resolved" from
  "unrelated monotonic drift over the sweep's wall-clock duration that
  happens to correlate with tau_mw" -- the clean test is a
  reversed-order or randomized/interleaved tau_mw sweep, not yet done.
  Also notable: all three runs hit `OVERLOAD`/auto-rescale multiple times
  right at the START of the sweep (e.g. `rabi3`: 4 times in its first
  34/197 points), immediately after `auto_gain()` had just picked a
  sensitivity -- direct evidence the raw signal was unstable enough to
  blow past a just-chosen range almost immediately, independent of the
  FFT/noise analysis above.

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

- **`rabi_new11_fix_anchor` showed `anchor_free_reps=200` (the old
  `cmd_run()` default) isn't long enough to keep an anchor-wrap out of
  every point's settle window.** At its parameters (n_reps=250, tau_mw
  0.02-3.02 us), `ref_period_s` ranges 2.01-3.51 ms, so the anchor period
  (`ref_period_s * anchor_free_reps`) was only 0.40-0.70 s -- shorter than
  `settle_s=0.9s` (dominated by `settle_time_constants*time_constant_s`)
  everywhere in the sweep, guaranteeing 1-2 anchor-wrap glitches inside
  every single settle window. Needed `anchor_free_reps > 0.9 /
  2.01e-3 ~= 448` (worst case, shortest tau_mw) just to make it possible
  for a window to land clean. **Fixed**: `cmd_run()`'s `anchor_free_reps`
  default raised from `200` to `500` (comfortably past that threshold
  across the default sweep range too -- 500-rep anchor periods run
  1.0-2.0 s at `mw_start_us=0.02`-`mw_stop_us=5.0`, both above
  `settle_s=0.9s`), already confirmed safe up to 1000 repeats on real
  hardware in `tests/rabi_combined_arb_marker_test.ipynb`.

  Side effect: since a full 500-rep anchor cycle (1.0-2.0 s) now runs
  LONGER than `settle_s` (0.9 s) itself, the per-point loop always moves
  on to the next point mid-cycle -- the AWG is never idled back at its
  anchor, waiting for a trigger, by the time `setup_awg_sequences()` gets
  called again. This isn't new (it already happened at `anchor_free_
  reps=200` whenever a cycle ran long, and DATA:VOL:CLE-while-playing has
  worked fine on real hardware across every `rabi_new*` run so far), but
  it's now the ALWAYS case rather than the occasional one. **Hardened
  anyway**: added `awg.write("ABOR")` right before the `SOUR1/2:DATA:VOL:
  CLE` calls at the top of `setup_awg_sequences()`, so the previous
  point's sequence is explicitly stopped before its arb memory gets
  cleared and replaced, instead of relying on clear-while-playing being
  safe. Not yet re-run on real hardware with this combination (`anchor_
  free_reps=500` default + `ABOR`) at full `cmd_run()` sweep scale.

- **`rabi_new13_fix_anchor2` (500 default + ABOR) still showed 4/31
  outlier points, but NOT random -- pooling `rabi_new11/12/13` (123 points
  total) and bucketing by offset from the nearest periodic interlock check
  (`interlock_check_interval=5`) showed the check point itself is the
  CLEANEST (0/27 near the sensitivity rail) while points at offset +1/+2
  are the WORST (33% near the rail, ~2x the mean of offset 0) -- a period-5
  pattern, matching `interlock_check_interval`, not `RESEQUENCE_INTERVAL`
  (4) or anything n_reps/anchor-period related. Traced the actual
  synchronization: `settle_s` is a blind `time.sleep()` in Python that
  starts counting AFTER `setup_awg_sequences()` returns, `lia.read_
  overload_status()`'s discard, and (if this point runs one) the interlock
  check -- there is NO handshake with the AWG confirming it has actually
  resumed real signal output; the code just assumes real content resumes
  almost immediately after upload. Meanwhile the external trigger (SDG1062X,
  `fixed_external_trigger=true`) free-runs continuously and totally
  independent of Python's loop timing.

  **Root cause**: `HP8673H.read_max_hold_reflected_power_dbm()`'s GPIB
  round trip measured at **~1s on real hardware** -- squarely comparable to
  `anchor_period_s` (`ref_period_s * anchor_free_reps`, ~1.0-1.5s across
  most of this sweep's `mw_us` range at the new `anchor_free_reps=500`
  default). Since the interlock check runs BEFORE `settle_s` starts (same
  point, `cmd_run()`'s per-point loop), ordinary GPIB timing variance is
  enough to occasionally push the check's duration past `anchor_period_s`
  -- meaning the AWG's current anchor_free_reps-repeat run SOMETIMES
  finishes and wraps back to its onceWaitTrig anchor DURING the check, and
  sometimes doesn't. That's a genuine race, not a fixed offset -- exactly
  the kind of thing that produces sporadic, unpredictable spikes rather
  than a clean, reproducible pattern, and explains why it clusters near
  (though doesn't perfectly align point-for-point with) the periodic
  check rather than showing up uniformly across the sweep.

  **Fixed**: rather than trying to guess a margin big enough to keep the
  check reliably under `anchor_period_s` (fragile, and still a race), added
  an unconditional `awg.write("ABOR")` right after the interlock check
  block (whenever `i % interlock_check_interval == 0`), before `_wait_
  settle_discarding_transient_overload()`. This forces the AWG into a
  KNOWN state -- stopped, idle at its anchor, waiting for the next trigger
  edge -- every time a check runs, instead of leaving "mid-run or already
  re-idled?" ambiguous. The resulting dead time (bounded by one trigger
  period, ~0.3s at current settings) is the same kind of transient the
  existing discard-at-80%-of-settle_s logic already absorbs, just now
  guaranteed to occur (and be covered) rather than randomly sometimes
  needed and sometimes not. No reupload needed -- the sequence/arb data is
  untouched, ABOR just stops playback; the next trigger edge resumes it
  from segment 1 (the anchor) same as ever.

  Also added a diagnostic print (timing of the interlock check vs. that
  point's `anchor_period_s`) right before this fix, which is what
  confirmed the ~1s check duration in the first place -- left in place
  since it's cheap and directly useful if this ever needs re-diagnosing at
  different `anchor_free_reps`/`n_reps`/`mw_us` combinations.

  **Re-tested on real hardware: the post-check `ABOR` did NOT eliminate
  the spikes.** So the race with `anchor_period_s` either isn't the whole
  story, or `ABOR` itself doesn't cleanly force the assumed known state
  (e.g. maybe it doesn't fully suppress whatever the check's GPIB/RF
  activity couples into the signal path, independent of the AWG's own
  play state). Rather than keep guessing at the exact mechanism, added an
  `interlock_during_sweep` option (default `true`) to `cmd_run()` that
  skips the PERIODIC per-point check entirely (the `i % interlock_check_
  interval == 0` block, including this `ABOR` and the timing diagnostic)
  when set `false` -- lets the periodic check be removed as a variable
  altogether to isolate whether it's the cause at all, without touching
  the PRE-FLIGHT reflected-power check (`reflected_power_scan`'s coarse/
  fine sweep + the single operating-point check right before the sweep
  loop starts, `rabi.py:~775-849`), which always still runs regardless of
  this flag. Caution: `interlock_during_sweep=false` means NO reflected-
  power protection while sweeping tau_mw -- only use for short diagnostic
  runs at power levels already confirmed safe, not routine data
  collection. Not yet run on real hardware with this option.

- **`rabi_new17_no_interlock` (interlock_during_sweep=false, zero periodic
  checks) still showed the same spikes, at the same near-identical clipped
  magnitude (~1.545e-4) as every prior test run** -- this ruled out the
  interlock/GPIB-timing race entirely (it was a real, confirmed ~1s-vs-
  anchor_period_s race, but not THE cause of the background artifact).
  Re-examined by bucketing `rabi_new14/15/16/17` (123 points) by distance
  from the nearest periodic `awg.reset()` (`RESEQUENCE_INTERVAL`, then
  still 4) instead of the interlock check: `i%4==0` (just reset) was clean
  (0/16 near the rail), while `i%4==1/2/3` got progressively worse
  (15%/25%/26%) right up to the point before the next reset -- a clean
  monotonic climb-then-flush pattern, not noise.

  `awg.reset()` (`ks33600a.py:34`) is `*RST` + `*CLS` -- a full instrument
  reset. Every other point only gets `ABOR` (in `setup_awg_sequences()`)
  before clearing/reuploading arb memory -- a much lighter operation.
  Leading theory: `ABOR` isn't fully flushing something in the AWG's
  trigger input logic (most plausibly a buffered/stale external trigger
  edge) that `*RST` does -- so per-point `ABOR`-only reconfigures let
  timing uncertainty compound across the `RESEQUENCE_INTERVAL` window
  until the next full reset flushes it clean.

  **Made `RESEQUENCE_INTERVAL` a runtime override** (`resequence_interval`
  kwarg, `cmd_run()` and `_run_no_mw_impl()`, defaults to the module
  constant) instead of only a hardcoded module constant, specifically to
  let this theory be tested directly: `resequence_interval=1` resets the
  AWG every single point (slow -- full `*RST` per point -- but a clean
  pass/fail test).

  **Confirmed on real hardware**: `rabi_new18_resets` (`resequence_
  interval=1`, otherwise identical params to new14-17) came back
  completely clean -- max R = 6.7e-5, nowhere near the ~1.5e-4 rail, 0/16
  points anomalous. This is the first test-sweep run at these tight
  (`fixed_sensitivity=true`, 100 uV) settings with NO spikes at all.
  Confirms the theory: full `*RST` per point (not just `ABOR`) is what
  actually keeps the AWG's trigger/timing state clean.

  **Not yet resolved**: running `*RST` every single point is slow and not
  practical for long, fine-grained sweeps (hundreds of points). Still
  need to find whichever SPECIFIC piece of `*RST` (beyond `ABOR`) is doing
  the flushing -- so it can be replicated cheaply every point without a
  full instrument reset -- or otherwise settle on a resequence_interval
  low enough to keep this acceptable for real sweeps without paying full
  `*RST` cost every point.

- **Investigated what specifically about `*RST` (vs. plain `ABOR`) fixes
  it, by tracing the actual command sequence rather than guessing.**
  `setup_awg_sequences()` unconditionally rewrites `TRIG1:SOUR EXT`/
  `TRIG2:SOUR EXT` every single point, regardless of whether that point
  went through `ABOR` or a full reset. On a normal `ABOR`-only point,
  `TRIG:SOUR` was already `EXT` before this write and stays `EXT` after --
  a same-value re-assertion, not a real transition. But Keysight 33500/
  33600-series `*RST` resets `TRIG:SOUR` to its factory default,
  `IMMediate` -- so on a just-reset point, `setup_awg_sequences()`'s own
  `TRIG:SOUR EXT` write causes a REAL `IMM -> EXT` transition.

  **Leading theory**: `*RST` fixing the spikes is incidental to its main
  purpose -- the actual fix is that `IMM -> EXT` transition flushing a
  stale/latched external trigger edge sitting in the AWG's trigger input
  circuitry, something a same-value `EXT -> EXT` write never touches. A
  same-value write and a genuine transition are not guaranteed to behave
  identically at the hardware level even though the end state looks
  identical from software.

  **Tried, FALSIFIED on real hardware**: added `awg.write("TRIG1:SOUR
  IMM")` / `awg.write("TRIG2:SOUR IMM")` right after `ABOR` at the top of
  `setup_awg_sequences()`, forcing an `IMM -> EXT` round trip every point.
  `rabi_new20_test` (6 points, `mw_us` 0.02-1.02) still hit the same
  ~1.3-1.5e-4 rail at 2 of 6 points with the toggle in place -- the
  trigger-source-transition theory is wrong (or at least not sufficient).
  Removed the toggle (`rabi.py:326`) rather than leave dead code from a
  disproven theory in place.

  **Next candidate, more directly justified**: `FUNCtion:ARBitrary:
  SYNChronize` -- a real, documented Keysight 33500/33600-series command
  (normally used to re-align coupled channels' arb phase after changing
  sample rate/frequency), not a guess about a side effect. Added `awg.
  write("SOUR1:FUNC:ARB:SYNC")` / `awg.write("SOUR2:FUNC:ARB:SYNC")` at
  the end of `setup_awg_sequences()`, right after both channels' sequences
  are freshly selected and armed (`OUTPut:SYNC:SOURce CH2` line) -- forces
  each point's freshly-loaded sequence to start from a known phase before
  it ever sees a trigger edge, rather than inheriting whatever phase the
  AWG's internal clock/DDS happened to be at from continuous free-running
  operation since the last real reset.

  **Also FALSIFIED on real hardware**: `rabi_new20_test` (`FUNC:ARB:SYNC`
  in place, normal `resequence_interval`) still hit the identical rail
  value (`1.092e-4`) at 4 of 16 points -- same magnitude, same rate as
  before. This candidate doesn't fix it either.

- **Major reframing: the background artifact is NOT on the signal path at
  all.** With the PMT turned off entirely, `~10 uV` signals persisted --
  ruling out anything optical. Then, with the SR830's signal input
  properly TERMINATED with 50 ohm (not just disconnected/floating, which
  would still act as a pickup antenna) and `resequence_interval=1` (the
  one AWG-side fix that had looked reliable), the EXACT SAME rail value
  (`X=Y=1.092190e-04`) still appeared at 1 of 6 points. A 50-ohm-terminated
  input has nothing left on the signal side for anything to couple into --
  this rules out the PMT, its cable, ground loops on the signal path, and
  radiated pickup landing on a floating antenna, all at once.

  This reframes the whole investigation: the artifact must originate from
  the REFERENCE path or from something internal to the SR830 itself, not
  the signal path -- meaning `anchor_free_reps`, `ABOR`, `FUNC:ARB:SYNC`,
  and `resequence_interval` were all attempts to fix something on the AWG/
  sequencing side, but every one of them still necessarily disrupts the
  reference (CH2's Sync output) somehow when a point's `mw_us` changes --
  that disruption may be unavoidable by construction, not a bug in any one
  approach. Leading hypothesis: reference feedthrough -- a lock-in's
  reference conditioning and signal preamp are never perfectly isolated
  internally, and while a STEADY periodic reference contributes zero net
  DC to X/Y (it averages out over a cycle), an IRREGULAR/discontinuous
  reference edge (exactly what happens every time the AWG reconfigures)
  would not average out, showing up as a transient, roughly fixed-
  magnitude artifact -- consistent with the same numeric value recurring
  across totally different AWG-side conditions.

  **Added diagnostic**: `cmd_run()` now also queries the SR830's own
  measured reference frequency (`SR830.get_frequency_hz()`, wraps `FREQ?`)
  right after `read_xy()` each point, saved to a new `_rabi_ref_freq_hz.
  npy` file, printed every point (changed from only-on->1%-deviation to a
  plain per-point print, per request, since scanning full console output
  for a rare warning is more friction than just seeing every value).

  **Tested on real hardware -- frequency looks completely normal at spike
  points.** A live run showed `X=Y=1.092220e-04`-type spikes at points 4
  and 6, and BOTH printed a reference frequency matching the expected
  `1/ref_period_s` to well under 0.01% (e.g. `432.901000 Hz` vs expected
  `432.900433 Hz`). This rules out a gross reference frequency error or
  lock loss lasting long enough to skew `FREQ?`'s reading -- but `FREQ?`
  is presumably an averaged/tracked measurement, so a single brief edge
  glitch (a missing/extra/jittered edge, not a sustained frequency error)
  wouldn't necessarily show up in it at all.

  **More sensitive follow-up added**: the SR830's `LIAS?` status byte has
  a bit 3 = reference unlock, which `sr830.py`'s `read_overload_status()`
  wasn't decoding (only bits 0-2, the overload bits). Added it as a new
  `reference_unlock` key on the returned dict (kept separate from `"any"`,
  which still only covers the overload bits, since nothing currently
  rescues sensitivity in response to an unlock and callers gating overload
  -rescue logic on `"any"` shouldn't have that logic triggered by it).
  Unlike `FREQ?`, `LIAS?` bits LATCH until read (see `read_overload_
  status()`'s docstring), so this should catch even a brief unlock event
  that occurred any time since the last read, not just the exact instant
  read. `cmd_run()` now reads this unconditionally right after `read_xy()`
  (previously the only `read_overload_status()` call was gated behind
  `auto_rescale_on_overload`, which `fixed_sensitivity=true` -- used for
  every test run in this whole investigation -- disables, meaning
  reference_unlock was never actually being checked at all until now).
  Prints it every point and saves it to a new `_rabi_reference_unlock.
  npy` file.

  **Confirmed on real hardware: `reference_unlock=False` at a spike
  point**, with frequency also spot-on (`243.307000 Hz` vs expected
  `243.309002 Hz`) -- rules out the reference-side theory entirely. Also
  added `input_overload`/`filter_overload`/`output_overload` to the same
  print (already fetched in the same `LIAS?` read, no extra query) since
  only `reference_unlock` had been wired up. That print immediately paid
  off: **`output_overload=True`, `input_overload=False`** at a spike
  point (`X=Y=1.092190e-04`). This is a real, distinct finding -- the
  analog front end never saw anything too large (consistent with a 50 ohm
  -terminated input and no PMT), but the INTERNAL DEMODULATED OUTPUT
  calculation transiently exceeded its representable range. First
  concrete evidence the artifact originates inside the SR830's own signal
  processing, not from anything external on the signal OR reference path
  (both already ruled out above).

  **Found a real, independent contributing factor while investigating
  this**: `setup_lock_in()` (shared by all three `rabi.py` entry points)
  never calls `lia.reset()` at connect time, and nothing anywhere sets
  OEXP (offset/expand). Any nonzero offset or >1x expand left over from a
  PREVIOUS session (another script, e.g. `cw_odmr_lock_in.py`, or a manual
  front-panel adjustment) would silently persist across every `rabi.py`
  run indefinitely, shrinking the SR830's usable output range and making
  a nuisance `output_overload` trip far easier to hit from an otherwise
  harmless transient -- with NO relation to the actual input signal or
  any of the AWG-timing work. **Fixed**: added `get_offset_expand()`/
  `set_offset_expand()` to `sr830.py` (wraps `OEXP?`/`OEXP`), and
  `setup_lock_in()` now queries and logs whatever was there before,
  then forces both X and Y to a known-clean 0%/1x state on every run.
  Also added `check_sr830_offset_expand.py`, a standalone script to
  spot-check the instrument's current offset/expand state directly
  without running a full sweep. Confirmed on real hardware: X and Y
  (the only two channels `rabi.py` ever actually reads, via `SNAP? X,Y`)
  were already clean (0%/1x) -- only R had a negligible 0.09% offset,
  irrelevant since R is always computed from X/Y in software, never read
  from the instrument directly. Offset/expand is NOT the explanation.

  **Removed the `setup_lock_in()` OEXP check entirely.** It crashed a real
  `cmd_run()` (`ValueError: not enough values to unpack (expected 2, got
  1)` from `get_offset_expand()`'s `OEXP? {channel}` query) even though
  the identical code had worked fine when run standalone via `check_sr830_
  offset_expand.py` earlier -- the query's response format apparently
  isn't reliable across every calling context on this instrument/driver,
  and since the check already confirmed offset/expand wasn't the cause of
  the background artifact, it wasn't worth hardening further. `setup_lock_
  in()` no longer touches OEXP at all. `get_offset_expand()`/`set_offset_
  expand()` are still in `sr830.py` and `check_sr830_offset_expand.py`
  still exists as a standalone spot-check if ever needed again, but
  nothing in a real `cmd_run()`/`cmd_run_repeat()` call path uses them
  anymore.

- **Isolated the artifact from the real sweep entirely with two new
  scripts, `debug_repeat_one_point.py` (`with-reupload`/`no-reupload`
  commands) and `check_sr830_offset_expand.py`** -- both connect ONLY to
  the AWG, its SDG1062X external trigger, and the SR830, deliberately
  never touching the MW generator, either PSU, or the interlock analyzer,
  so they're safe to run with the MW generator/amplifier physically off.

  **`no-reupload`** (uploads once, then repeats `_wait_settle_discarding_
  transient_overload()` + `read_xy()` forever on that same never-touched
  sequence) showed the TRUE noise floor is far quieter than anything seen
  in this whole investigation: iteration 0 (right after the one-time
  upload) showed a modest ~6-11 uV bump, but by iteration 1 it had already
  settled to tens of NANOvolts and stayed there. This means the true
  noise floor, once nothing is reconfiguring, is comfortably below the
  ~10 uV CW-ODMR signal -- not comparable to it like the earlier stale
  `rabi_off_resonance_fix_long` measurement suggested (that measurement
  predates this whole investigation and used different sensitivity
  settings -- see the "off-resonance noise floor is comparable to the CW-
  ODMR signal" entry above). It also directly confirms a reconfigure event
  causes a real transient (the iter-0 bump) that fully decays within one
  normal settle_s window -- but that's the everyday few-uV bump, not the
  rare ~1.5e-4 V rail spikes chased throughout this investigation.

  **`with-reupload` + new `extra_settle_s` override** (a flat extra wait
  added right after `setup_awg_sequences()`'s reupload/re-arm, before
  settle_s's own countdown starts) tested whether the BIG rail spikes
  specifically need more margin after a reconfigure event, at a normal
  every-point-reupload cadence (unlike `resequence_interval=1`, this
  doesn't need a full `*RST`, just a longer wait after the normal `ABOR`+
  reupload). **Confirmed on real hardware: `extra_settle_s=1.0` showed
  zero rail spikes across several iterations of every-point reupload** --
  values stayed in the ~1e-7-1e-8 V range throughout, no `output_overload`
  or magic-rail values at all. This is the first fix in this whole
  investigation that's actually been validated against the BIG spikes at
  a normal reconfigure cadence (not just the everyday small bump, and not
  requiring a full-reset-every-point workaround).

  **Applied to `rabi.py` itself**: added `extra_settle_s` (default `0.0`,
  no behavior change) to both `cmd_run()` and `_run_no_mw_impl()`, added
  right after each's `setup_awg_sequences()` call via `time.sleep()`,
  before the settle-and-read logic. Not yet swept to find the MINIMUM
  sufficient value (1.0s was just what was tested) -- worth trying smaller
  values (e.g. 0.2, 0.5) to avoid needlessly slowing down a real sweep,
  now that `debug_repeat_one_point.py with-reupload extra_settle_s=<x>`
  gives a fast, MW-generator-free way to test values without spending
  real sweep time. Also still an open question WHY extra dwell right
  after reconfigure specifically fixes this (a real transient that needs
  more decay time before being read, presumably) rather than a deeper
  mechanistic understanding -- but it's now an empirically validated,
  practical mitigation for real data collection.

- **`extra_settle_s` interacts with `anchor_free_reps` -- raising one
  without the other reintroduces the wrap-mid-window problem.**
  `anchor_free_reps` was sized (`rabi_new11_fix_anchor` onward) so
  `anchor_period_s` (`ref_period_s * anchor_free_reps`) comfortably
  exceeds the dwell time before a point's read -- but that dwell used to
  just be `settle_s`. With `extra_settle_s` now adding a wait BEFORE
  `settle_s`'s own countdown starts, the real dwell to cover is
  `extra_settle_s + settle_s`. At `extra_settle_s=1.0` (the validated
  value) and `settle_s=0.9` (default sweep params), that's ~1.9 s total,
  needing `anchor_free_reps > ~945` at the shortest `tau_mw` to still
  cover it -- the old default of 500 falls well short, which would let
  the sequence wrap and need an uncontrolled retrigger mid-window again,
  the exact failure mode `anchor_free_reps` exists to prevent. **Fixed**:
  raised `cmd_run()`'s `anchor_free_reps` default from `500` to `1000`
  (confirmed fine on real hardware up to that value already, per `tests/
  rabi_combined_arb_marker_test.ipynb`). If `extra_settle_s` is raised
  further, `anchor_free_reps` needs to go up correspondingly too -- noted
  in the code comment, not automatically computed (kept as two
  independent kwargs rather than deriving one from the other, since
  that's cheap to compute in each real run's own math via
  `_configure_external_trigger`'s prints anyway).

- **Milestone: the electronic/reconfigure-side background artifact
  investigation is resolved as far as it can meaningfully go.** With
  `extra_settle_s` increased further (beyond the initially-validated
  1.0 s -- exact value not recorded here, bump it up from real usage if
  revisiting) on top of `anchor_free_reps=1000`, tested on real hardware:
  - Input terminated (50 ohm, no PMT): noise reduced from the ~1.5e-4 V
    rail spikes seen throughout this investigation down to a much smaller
    residual.
  - PMT connected: ~4 uV noise floor -- a workable SNR (~2.5:1) against
    the ~10 uV CW-ODMR signal, a large improvement over noise being
    comparable to or exceeding signal (the state at the start of this
    investigation).
  - **PMT off (with these same current settings): much quieter than the
    ~4 uV PMT-connected floor.** This is the key result -- it confirms the
    remaining ~4 uV is NOT the electronic/reconfigure-side artifact this
    entire investigation has been chasing (that's now below the PMT's own
    contribution), but something in the actual PMT/optical signal chain
    instead: PMT gain/bias, dark counts, ambient light leakage, preamp
    noise, or similar. Further noise reduction from here is a DIFFERENT
    investigation, not an AWG-sequencing/lock-in-reference one.

  Current best-known-good settings for real data collection, pending
  further tuning: `anchor_free_reps=1000` (default), `extra_settle_s`
  set explicitly (not yet re-confirmed what value was last used --
  check the actual `cmd_run()` invocation when resuming), `resequence_
  interval` at its default (4) since `extra_settle_s` was validated
  specifically as an alternative to needing `resequence_interval=1`.
  `rabi_result.ipynb` was deleted and is being rebuilt fresh going
  forward -- this whole investigation's data (`rabi1`-`rabi9`, `rabi_new`
  through `rabi_new20`, `rabi_off_resonance_fix_long`) still lives in
  `D:/rabi/` if old runs need referencing again, just no longer plotted
  in a live notebook.

  **Follow-up isolation of the remaining ~4 uV (PMT-connected) floor,
  same current settings**: RF amplifier OFF -- noise still there (rules
  out MW/RF leakage/EMI as the cause). Laser OFF (PMT still connected/
  powered) -- much quieter. Full chain: amp off doesn't help, PMT off
  fixes it, laser off ALSO fixes it -- the remaining noise is LASER-
  DRIVEN OPTICAL noise reaching the PMT, not PMT dark counts/electronics
  (which wouldn't need light to appear), not MW/RF-related, and not
  anything AWG-reference-side (already resolved above). Likely
  candidates: scattered/reflected excitation laser light leaking past
  whatever filtering sits before the PMT, laser intensity noise (RIN)
  contributing broadband shot-noise-like noise, or genuine background
  fluorescence from the substrate/optics unrelated to the NV center.
  Consistent with CH1 (laser gate) firing identically during both on-
  cycle and off-cycle halves of the reference (no reference-synchronous
  difference in laser drive, unlike CH2) -- a coherent laser-driven
  signal shouldn't demodulate into X/Y at all, so this reads as
  fluctuating broadband noise (~4 uV) rather than a discrete artifact,
  consistent with real optical shot noise rather than another timing
  glitch. This is now an OPTICS investigation (laser filtering before
  the PMT, beam path stray reflections, RIN, collection optics), not an
  AWG-sequencing/lock-in-reference one -- separate from everything above.

  **Confirmed by physically blocking the beam path to the PMT (not just
  turning the laser off electronically) -- quieter.** This is a stronger
  result than the laser-off test: it isolates that actual PHOTONS from
  the laser are reaching the PMT (via scatter, reflection, or leakage
  past whatever filtering exists), not electrical crosstalk from the
  laser driver/AOM electronics (turning the laser off electronically
  would also remove any such electrical coupling, so it alone couldn't
  distinguish the two -- physically blocking the beam while everything
  electrical stays running rules electrical crosstalk out specifically).
  Next steps are physical/optical, not code: check whether the excitation
  filter before the PMT (notch/longpass/bandpass, whichever is installed)
  is appropriate for the laser wavelength and not degraded/bypassed;
  check the beam path for uncovered optics, loose mounts, or reflective
  surfaces near the collection path that could scatter excitation light
  toward the PMT; check any pinhole/spatial-filtering/fiber-coupling
  alignment, since misalignment lets in more background light than
  intended.

- **Side investigation while chasing the optics noise: tested `n_reps=50`
  (down from 250) to see if noise scales with the laser/AOM's switching-
  edge count rather than total light dose -- but the FIRST attempt was
  confounded.** `anchor_free_reps=1000` was sized for `n_reps=250`;
  dropping to `n_reps=50` without scaling it up shrinks `anchor_period_s`
  proportionally (`ref_period_s` scales with `n_reps`) -- from ~2.0 s down
  to ~0.4 s, far under the ~1.9 s dwell window (`settle_s + extra_settle_
  s`). This reintroduced the exact anchor-wrap problem `anchor_free_reps`
  exists to prevent, confirmed directly on real hardware: `reference_
  unlock=True` (the first time this bit has ever been seen set in this
  whole investigation) alongside the classic `output_overload`/rail-value
  signature. **Not evidence about laser-switching noise** -- re-tested
  with `anchor_free_reps=5000` (computed to restore the same margin
  `n_reps=250` had) and `reference_unlock` stayed `False` across the
  whole sweep.

  That re-test surfaced a real, separate bug though: point 1 (the only
  point that went through the periodic interlock check, `interlock_
  check_interval=5` default) still hit the classic rail value
  (`output_overload=True`, `X=Y=-1.09222e-4`) while every other point was
  clean. Traced it to a gap in the `extra_settle_s` fix: the interlock-
  check path has its OWN `awg.write("ABOR")` (added earlier specifically
  to force a known state after the ~1s GPIB round trip -- see the
  "spikes near interlock checks" entry above), which is a SEPARATE
  reconfigure event from the main `setup_awg_sequences()` call, but went
  straight into `_wait_settle_discarding_transient_overload(settle_s)`
  with no `extra_settle_s` buffer at all -- `extra_settle_s` had only
  been added after the main `setup_awg_sequences()` call, not after this
  second ABOR. **Fixed**: added the same `time.sleep(extra_settle_s)`
  right after this ABOR too (`rabi.py`, inside the `interlock_during_
  sweep` block). Not yet re-run on real hardware to confirm this
  specific point is now also clean, but the mechanism is well understood
  from the exact parallel to the already-validated main-path fix.

  The underlying laser-switching-vs-light-dose question (why `n_reps=50`
  test was started) is still open -- needs a clean re-run with `anchor_
  free_reps` properly scaled for `n_reps=50` AND this interlock-path fix
  in place before it's a fair comparison against `n_reps=250`.

  **Clean re-run done (`backgroundfree13_less_reps` at `n_reps=50` with
  `anchor_free_reps` properly scaled, vs `backgroundfree14` at
  `n_reps=250`, both at 2.14 GHz, `sensitivity_v=1e-4`): both came back
  fully clean (no `reference_unlock`, no rail spikes), confirming the
  fix.** `n_reps=50` was actually QUIETER, not noisier -- mean R 4.20 uV
  (std 1.07) vs 5.52 uV (std 2.75) at `n_reps=250`, max 6.03 uV vs
  9.52 uV. Only 5 points each, so not a lot of statistical weight, but
  this argues AGAINST "more laser-switching edges -> more noise": the two
  runs have the identical AOM switching RATE per second regardless of
  `n_reps` (`rep_us` is fixed by `laser_us+pre_us+mw_us+post_us`,
  independent of `n_reps` -- `n_reps` only changes how many switches
  happen before the lock-in's OWN reference toggles, not the AOM's
  physical switching cadence). What differs between the two runs is the
  reference/chop frequency itself (`n_reps=50` chops ~5x faster), and the
  faster-chop run being quieter is more consistent with ordinary 1/f-type
  noise (common in environmental, electronic, and laser RIN sources) than
  with a distinct AOM-switching-count effect. The laser-driven optical
  noise identified earlier (beam-block test) is real, but this particular
  n_reps comparison doesn't point to switching-edge COUNT as its
  mechanism -- worth keeping in mind if pursuing the optics investigation
  further (e.g. RIN, or genuinely lower background dose, matter more than
  switching frequency per this data).

- **Added `rabi.py run-repeat` (`cmd_run_repeat()`)** to repeat a full
  sweep `n_repeats` times (default 10) and average X/Y elementwise across
  completed repeats (R computed from the averaged X/Y after, never
  averaged directly -- same convention as `cw_odmr_lock_in.py`'s
  `cmd_sweep_average()`). Deliberately implemented as a thin wrapper that
  calls the existing `cmd_run()` UNCHANGED once per repeat (full
  reconnect/pre-flight/PSU cycle each time) rather than refactoring
  `cmd_run()`'s internals to share one long-lived connection across
  repeats -- slower, but zero risk of a repeat-batching refactor
  introducing a new timing bug into the single-sweep path this whole
  session was spent validating. A repeat that trips the interlock partway
  through saves a shorter `mw_us` array and is excluded from the average
  (grid mismatch against the first fully-completed repeat).

  **Initially saved each repeat under its own `<file_name>_repeat{i}/`
  folder -- 100 repeats meant 100 folders, which wasn't wanted.** Fixed by
  adding an `output_dir` override to `cmd_run()` itself (defaults to
  `file_name`, so normal single-`run` calls are completely unaffected):
  when given, it controls only the save DIRECTORY, while `file_name`
  still controls the file-name PREFIX within it -- previously these were
  the same thing by construction (`run_path = f"{DATA_DIR}/{file_name}"`).
  `cmd_run_repeat()` now calls `cmd_run(f"{file_name}_repeat{i}",
  output_dir=file_name, **kw)` for every repeat, so all repeats (and the
  final average) land in ONE shared `D:/rabi/<file_name>/` folder,
  distinguished by file name (`<file_name>_repeat{i}_rabi_x.npy`, ...,
  `<file_name>_avg_rabi_x.npy`), not by directory. Not yet run on real
  hardware.

- **`cmd_run_repeat()` surfaced a real GPIB reliability bug on real
  hardware: a later repeat's `read_xy()` (`SNAP? X,Y`) crashed with
  `ValueError: not enough values to unpack (expected 2, got 1)`**, and the
  debug log showed a garbled non-numeric `ERRS?` response
  (`-1.37837e-007,-1.13361e-005`, not a valid SR830 error code) right
  before it. Root cause: `cmd_run_repeat()` reconnects to the SR830 fresh
  for every repeat, all within the SAME Python process (unlike a normal
  single `cmd_run()` invocation, where the OS/VISA layer fully tears down
  the GPIB session on process exit) -- a response still in flight, or a
  write the instrument hadn't finished processing, from the PREVIOUS
  repeat's connection can linger and get misread by the start of the NEXT
  one. **Fixed**: `SR830.close()` now calls `self.inst.clear()` (IEEE-
  488.2 selected device clear, flushes/aborts any pending I/O on the
  interface) before actually closing, wrapped in a try/except since a
  truly wedged instrument could fail the clear itself without that
  blocking the rest of shutdown. Since all three of `rabi.py`'s per-
  command shutdown blocks already call `lia.close()` in their `finally`
  clause (confirmed `KeyboardInterrupt` is caught INSIDE the try block
  around the per-point loop, so `finally` -- and therefore this -- always
  runs on Ctrl+C too, not just normal completion), this fix applies
  everywhere automatically without touching `rabi.py` itself. Not yet
  re-confirmed on real hardware that this actually prevents the garbled-
  response failure recurring.

- **Added `use_resonance_freq` to `cmd_run()`** -- `freq_hz` becomes a
  search MIDPOINT instead of the fixed drive frequency: the existing
  pre-flight coarse(+fine) sweep (already there for `reflected_power_
  scan`'s diagnostic check) runs regardless of `reflected_power_scan`,
  always includes the fine stage regardless of `fine_sweep` (a coarse-
  only dip isn't precise enough to actually drive at), and -- the key
  behavioral difference from the existing diagnostic mode -- does NOT
  pin the fine sweep's center to `freq_hz` (`fine_center_hz=None` instead
  of `fine_center_hz=freq_hz`), letting it land on wherever the coarse
  dip actually is, same as `cw_odmr_lock_in.py`'s `use_resonance_sweep`.
  `freq_hz` is then reassigned to the resolved `result["f0_hz"]` before
  the generator is set up for the real sweep, so everything downstream
  (the actual drive, the interlock check messages, the saved metadata)
  automatically uses the resolved value with no separate plumbing needed.
  Default `false` -- existing behavior (fixed `freq_hz`, fine sweep
  centered on it, diagnostic only) is completely unchanged.

  **`cmd_run_repeat()` pins every repeat after the first to repeat 0's
  own recorded `freq_hz`** (read back from its saved metadata file),
  with `use_resonance_freq` forced `false` for those -- one code path
  handles both the plain-fixed-frequency case (repeat 0's recorded
  `freq_hz` is just whatever was passed in, so pinning changes nothing)
  and the `use_resonance_freq=true` case (repeat 0 resolves it via the
  coarse+fine search, every later repeat reuses that exact value instead
  of independently re-searching resonance each time -- slower, and could
  drift point-to-point across an averaged batch otherwise).

- **`cmd_run_repeat()`'s in-process design crashed on real hardware after
  enough repeats**: `numpy._core._exceptions._ArrayMemoryError: Unable to
  allocate 7.68 MiB` inside `upload_waveform()` -- a tiny allocation that
  should never fail on a healthy system. Root cause: calling `cmd_run()`
  directly, many times, in the SAME long-lived Python process (the
  original design specifically to avoid touching `cmd_run()`'s internals)
  meant nothing forced a truly clean slate between repeats -- heap
  fragmentation and/or VISA session state from hundreds of large arb
  uploads and instrument reconnects accumulated until an ordinary-sized
  allocation failed. This is exactly the risk that design tradeoff
  accepted in exchange for reusing `cmd_run()` unmodified (see the
  original entry above) -- it just hadn't shown up yet.

  **Fixed**: each repeat now runs `python rabi.py run <repeat_name> ...`
  as a genuine SEPARATE OS PROCESS via `subprocess.run()` (inheriting
  stdout/stderr so live progress still prints straight through), not an
  in-process function call. A real process boundary eliminates this whole
  class of accumulation bug for free -- the OS reclaims everything on
  exit -- at the cost of one extra Python interpreter startup per repeat
  (~1s, negligible next to an actual sweep). `cmd_run_repeat()` still
  reuses `cmd_run()` completely unmodified; only how it's INVOKED changed
  (subprocess argv instead of a direct Python call), so the single-sweep
  path itself is still untouched by this. A `KeyboardInterrupt` while
  waiting on a repeat's subprocess now stops the repeat loop early
  (printed, not raised further) and proceeds to average whatever
  completed so far, same as the previous in-process version's behavior.
  Not yet re-run on real hardware to confirm this actually prevents the
  memory error recurring.

- **Added a coil-current optimization scan to `cmd_run()`, gated on
  `use_resonance_freq=true`.** After resonance is found (and the pre-
  flight operating-point reflected-power check passes), scans
  `coil_current_a` from `coil_scan_start_a` to `coil_scan_stop_a`
  (defaults 1.0-4.0 A, step 0.5 A) at a FIXED `coil_scan_tau_mw_us`
  (default 0.5 us) and the already-resolved `freq_hz` -- uploads ONE
  fixed pulse sequence via `setup_awg_sequences()` (never reconfigured
  during the scan, same "no per-point reconfigure" simplicity as `pulsed_
  odmr.py`), then at each current: sets the coil PSU (`voltage_for_
  current(current_a) * coil_voltage_margin`, same pattern used
  everywhere else in this codebase), waits `coil_scan_current_settle_s`
  (default 2.0 s) for the field to settle, takes `coil_scan_n_repeats`
  (default 5) repeated readings, and averages X/Y across them (R computed
  from the averaged X/Y after, never averaged directly -- same
  convention as `cmd_run_repeat()`/`cw_odmr_lock_in.py`). Picks the
  current with the largest resulting R, overrides `coil_current_a` with
  it for the rest of the run (re-applies it to the coil PSU before
  continuing, since the scan loop's last-tried current isn't necessarily
  the best one), and saves the whole scan
  (`<file_name>_coil_scan_current_a.npy`/`_x.npy`/`_y.npy`/`_r.npy`).
  `coil_current_a` is now also written to the run's saved metadata
  (wasn't before), so it's visible whether a run used the value passed in
  or one resolved by this scan.

  **Important physics caveat, documented in the code comment**: the coil
  current changes the static field, which shifts the NV resonance
  frequency itself (Zeeman splitting) -- this scan does NOT re-find
  resonance at each current, it holds `freq_hz` fixed at the ORIGINALLY
  -resolved value throughout. So what's actually being optimized is
  "signal at the original resonance frequency as a function of current,"
  not "true on-resonance signal as a function of current" -- if the real
  resonance drifts enough across the scanned current range (relative to
  the linewidth), R could read artificially low at currents where the
  line has moved off `freq_hz`, not because the true signal is weaker
  there. Fine for a narrow scan relative to the linewidth; worth keeping
  in mind if `coil_scan_stop_a - coil_scan_start_a` is large relative to
  how fast the resonance shifts with current on this setup.

  **Confirmed on real hardware (`measurement14`, `run-repeat` with
  `use_resonance_freq=true`, 5/5 repeats).** Resolved `freq_hz=2839.85`
  MHz from the coarse+fine scan, then the coil-current scan (1.0-4.0 A,
  0.5 A step, `tau_mw=0.5 us`, 5 repeats/point) gave a clean, single-
  peaked curve (roughly 16-23 uV across the range) with a clear maximum
  at `coil_current_a=2.6` A -- picked and applied correctly, matching
  what's in the saved metadata. Only `repeat0` actually ran the scan
  (as designed -- `cmd_run_repeat()` pins later repeats to repeat 0's
  resolved `freq_hz`, and since `coil_current_a` is likewise resolved and
  then just an ordinary override for the rest of that same `cmd_run()`
  call, subsequent repeats reuse it too without re-scanning).

- **Added `constant_shot_period` to `cmd_run()`/`setup_awg_sequences()`**:
  holds the TOTAL rep period (`laser_us+pre_us+mw_us+post_us+pad_us`)
  identical at every point across the tau_mw sweep, instead of it varying
  with `mw_us` like it always has. `pad_us = (mw_max_us - mw_us) +
  pad_min_us` (`mw_max_us` = `mw_values_us.max()`, the sweep's actual
  realized largest tau_mw -- NOT the nominal `mw_stop_us`, since `np.
  arange`'s `stop+step/2` rounding can realize a value slightly past
  `mw_stop_us`, which would make `pad_us` go negative for that point if
  the nominal value were used instead). `pad_us` is appended as an extra
  dead-time segment to the END of every rep (after `post_us`), on BOTH
  channels -- `setup_awg_sequences()` takes a new `pad_us=0.0` parameter
  for this, and 0.0 skips adding the segment ENTIRELY (not just a zero-
  length one), so every existing default-off call site reproduces the
  exact sample counts it always has, byte for byte.

  Motivation: `fixed_external_trigger` already keeps the EXTERNAL
  trigger's rate constant across the sweep regardless of `mw_us`, but the
  AWG's own actual rep period (and therefore the true reference
  frequency and the laser's duty cycle) still varied point to point
  purely because `mw_us` does -- this closes that gap, making every
  point's laser duty cycle and repetition rate genuinely identical, not
  just the external trigger rate. `constant_shot_period` and `pad_min_us`
  are threaded through the `fixed_external_trigger` setup block (using
  `mw_start_us` as before, now also correctly padded) and the main per-
  point loop's `rep_us`/`ref_period_s`/`settle_s` computation, and
  `constant_shot_period`/`pad_min_us` are now saved to the run's
  metadata (`constant_shot_period` written as `1`/`0`, not `True`/
  `False`, since every existing metadata reader does `float(v)` on every
  line uniformly -- see `rabi_result.ipynb`'s load cell). Off by default.

  **Confirmed on real hardware (`measurement16`, `run-repeat` with
  `use_resonance_freq=true` + `constant_shot_period=1`, `pad_min_us=0.1`,
  5/5 repeats).** Resolved `freq_hz=2800.5` MHz, coil scan picked
  `coil_current_a=3.9` A, ran clean (`reference_unlock=False` across all
  5 repeats, no output-overload spikes) -- the padding segment addition
  didn't introduce any new AWG/reference-side issue.

- **Real bug found on real hardware: `cmd_run_repeat()` pinned `freq_hz`
  across repeats but NOT `coil_current_a`.** Symptom: repeat 0's coil
  current was correct (whatever the coil-current scan found), but every
  later repeat's wasn't. Root cause: the pinning block sets `use_
  resonance_freq=false` for repeats after the first, and the coil-current
  scan is gated on that SAME flag -- so it never runs again for those
  repeats, and since `coil_current_a` was never separately pinned, they
  silently fell back to whatever was in the ORIGINAL kwargs (the
  unoptimized starting value), not the value the scan actually found on
  repeat 0. **Fixed**: read back `coil_current_a` from repeat 0's saved
  metadata alongside `freq_hz` (it's saved unconditionally regardless of
  `use_resonance_freq`, so this always works once repeat 0 completes),
  and pin it into every later repeat's kwargs the same way. Not yet
  re-run on real hardware to confirm.

- **Also skip the FINE resonance sweep on repeats after the first**,
  since `freq_hz` is already pinned by then and re-running it is pure
  waste -- forces `fine_sweep=false` in the same pinning block. The
  coarse sweep and the reflected-power-at-operating-point check still run
  on every repeat regardless (both are unconditional inside `cmd_run()`'s
  `if reflected_power_scan:` block -- only the fine STAGE is gated on
  `fine_sweep`), since those are real safety checks, not part of the
  resonance search being skipped. Not yet run on real hardware.

- **`cmd_calibrate_phase()` was significantly out of date relative to
  everything fixed in `cmd_run()` this session -- found when asked to
  audit it.** Its `setup_awg_sequences()` call didn't pass `anchor_free_
  reps` at all, silently defaulting to `1` -- the exact pre-fix worst-
  case configuration behind the entire "spurious off-resonance/no-MW-
  near-sample signal" investigation above. Since `auto_phase()` needs a
  clean, accurate X/Y null, running phase calibration under that same
  background artifact could silently produce a corrupted `phase_deg`
  with no way to tell. Also missing: `extra_settle_s` entirely; the
  `_configure_external_trigger()` call used raw `ref_period_s` instead of
  `anchor_period_s = ref_period_s * anchor_free_reps` (would have needed
  fixing together with adding `anchor_free_reps`, or the trigger
  wouldn't actually keep up); `settle_s` hardcoded `5 * ref_period_s`
  instead of an overridable `settle_periods` like `cmd_run()`; and none
  of the `reference_unlock`/overload diagnostics added to `cmd_run()`
  during the investigation.

  **Fixed**: added `anchor_free_reps` (default `1000`, matching `cmd_
  run()`'s current validated default), `extra_settle_s` (default `0.0`),
  and `settle_periods` (default `5.0`) as proper overrides; fixed the
  trigger call to use `anchor_period_s`; added the `extra_settle_s` sleep
  after `setup_awg_sequences()`; and added the same `reference_unlock`/
  overload check `cmd_run()` does, printed right at the auto-phase
  reading with a warning if anything looks off. Not yet run on real
  hardware -- worth re-calibrating phase with this fixed version before
  trusting any `phase_deg` obtained from the OLD version, since it may
  have been silently affected by the anchor_free_reps=1 issue.

  **Follow-up bug found on real hardware**: `WARNING: reference unlock or
  overload flagged right at the auto-phase reading` persisted
  (`reference_unlock=True, output_overload=True`) even at
  `anchor_free_reps=3000` -- well above the margin the anchor-wrap math
  requires (`anchor_period_s` came out to ~7.5x the settle window, vs.
  ~2.5x margin already considered fine at the `anchor_free_reps=1000`
  default). Anchor-wrap timing wasn't actually the problem this time.
  Root cause: unlike `cmd_run()`'s per-point loop (which calls `lia.
  read_overload_status()` right after `setup_awg_sequences()`/`extra_
  settle_s`, specifically to discard the transient reference-unlock/
  overload latch caused by the reconfigure itself -- see the
  `anchor_free_reps` entry above), `cmd_calibrate_phase()` went straight
  from `setup_awg_sequences()` into the `settle_s` sleep with no such
  discard read. `setup_awg_sequences()`'s own `ABOR`+clear+reupload
  momentarily stops CH2's marker output, which the SR830 reads as a
  reference dropout and LATCHES `reference_unlock` (and often an
  `output_overload` from the resulting glitch) until the status is next
  read -- with no discard read here, that stale latch survives the
  entire settle/auto-gain/auto-phase sequence and gets misreported by the
  final diagnostic as if it happened during the actual auto-phase
  measurement, even though the reference had already relocked well before
  then. **Fixed**: added the same discard `lia.read_overload_status()`
  call right after `setup_awg_sequences()`/`extra_settle_s`, before the
  `settle_s` sleep, matching `cmd_run()`'s pattern exactly. Not yet
  re-run on real hardware to confirm the warning clears.

  **Confirmed on real hardware, and a second distinct bug found underneath
  it.** Added per-stage diagnostic prints (reconfigure/discard, after 1st
  settle, after auto_gain, after auto_phase) to localize exactly where a
  persisting warning was coming from at `anchor_free_reps=3000, mw_us=0.5,
  coil_current_a=5.0`. Result: `reference_unlock` DID clear after the
  discard read (confirming the fix above works) and stayed `False` for
  the rest of the run -- but `output_overload` flipped `False` -> `True`
  right after `auto_gain()` and never cleared. Two separate conditions
  had been getting reported as one ambiguous warning the whole time
  (`if reference_unlock or any_overload`), which is why re-running with a
  bigger `anchor_free_reps` (correctly fixing the unlock half) looked like
  it did nothing.

  Root cause of the overload half: SR830's `AGAN` (auto-gain) only steps
  sensitivity ONE range per call (per the manual) -- `cmd_run()`'s
  per-point loop already accounts for this with a `_step_sensitivity_
  coarser()` rescale-retry loop after its own single `auto_gain()` call,
  but `cmd_calibrate_phase()` never had one, just a single `auto_gain()` +
  settle with no fallback. `mw_us`/`coil_current_a` picked to maximize
  Rabi contrast for a good auto-phase reading (per this function's own
  docstring) are exactly the conditions most likely to overshoot a single
  AGAN step. **Fixed**: added the same `_step_sensitivity_coarser()` +
  settle + re-check retry loop (`max_rescale_attempts=5`) right after
  `auto_gain()`, before `auto_phase()`. Confirmed on real hardware: the
  rescale loop correctly clears an `AGAN`-undershoot overload in one
  attempt (`1e-4 V -> 2e-4 V`), and the resulting `phase_deg` reading is
  reproducible (~-67 deg) across runs regardless.

  **Third gap found in the same investigation**: even with both fixes
  above, the FINAL diagnostic (after `auto_phase()` + settle) still
  intermittently showed `output_overload=True` -- but the actual settled
  X/Y values printed alongside it (`X=2.644410e-05 V, Y=-6.016380e-07 V`)
  are tiny, nowhere near overloaded for any plausible sensitivity in use.
  Root cause: `APHS` (auto-phase) steps the reference phase, which
  transiently redistributes signal between X/Y while the filter catches
  up -- the exact same category of transient as `setup_awg_sequences()`'s
  reconfigure glitch and the SENS-range-switch glitch (both already
  discarded elsewhere in this function), just one more un-discarded spot.
  With no read between `auto_phase()` and the final check, that transient
  survives the whole final `settle_s` sleep and gets misreported as if
  the final, already-settled reading were overloaded. **Fixed**: added a
  discard `lia.read_overload_status()` call right after `auto_phase()`,
  before the final settle. Not yet re-run on real hardware to confirm
  this closes out the warning for good -- if it recurs again, it's not a
  transient-discard gap anymore and needs fresh diagnosis.

  **It recurred, and the anchor-wrap theory is now ruled out.** Re-tested
  with `anchor_free_reps` raised to `20000` (`anchor_period_s` ~45s,
  comfortably longer than the whole calibration procedure could plausibly
  take) -- the exact same "works once, fails once" alternating pattern
  persisted at the SAME settings across consecutive runs. A >6x increase
  in anchor margin not changing the failure rate at all means this isn't
  a timing/latch bug anymore. Current best explanation: `calibrate-phase`
  is deliberately run at the `mw_us`/`coil_current_a` that give the
  STRONGEST signal (best Rabi contrast / coil-scan optimum, per this
  function's own docstring), and the rescale-retry loop only backs off
  enough to clear overload ONCE -- leaving the chosen sensitivity range
  right at the edge, where ordinary real signal fluctuation (mechanical,
  thermal, RF drift) can intermittently tip it back into overload on a
  run-to-run basis. Supporting evidence: `phase_deg` has stayed consistent
  across every run regardless of the warning (~-66 to -70 deg), i.e. the
  auto-phase result itself isn't actually corrupted by this -- consistent
  with "borderline range, occasionally trips" rather than "corrupted
  reading." **Mitigated** (not a bug fix, a headroom tradeoff): added one
  more deliberate `_step_sensitivity_coarser()` step after the rescale
  loop clears, trading resolution for margin against exactly this. Not
  yet confirmed on real hardware whether this actually reduces the
  intermittent-overload rate, or whether it's simply an inherent property
  of calibrating right at peak signal that occasional re-runs are the
  practical answer to (the warning's own "consider re-running" guidance
  may just be correct as originally written).

  **The extra-margin mitigation overcorrected -- reverted.** Confirmed on
  real hardware: the overload warning went away, but `phase_deg` then
  swung wildly run-to-run (70 deg -> 40 deg) instead of the previously
  reproducible ~-66 to -70 deg. `auto_phase()`'s null is `atan2(Y, X)` --
  pushing the sensitivity too far below full scale put X/Y close enough
  to the SR830's own noise floor that the null angle became noise-
  dominated rather than signal-dominated, exactly the "needs a real,
  reasonably strong signal" caveat already in this function's own
  docstring. An intermittently-overloading-but-reproducible phase reading
  was the better tradeoff. **Fixed**: reverted the extra deliberate
  coarsening step. **Also added**: a signal-strength diagnostic (R as a
  percentage of the active sensitivity's full scale, printed after
  `auto_phase()`, warning if under 5%) -- overload only ever catches the
  TOO-STRONG failure mode; there was nothing at all catching the TOO-WEAK
  one, which is exactly what silently broke this. Not yet re-run on real
  hardware to confirm `phase_deg` is reproducible again with the revert
  in place.

- **Coil current scan (`use_resonance_freq`'s coil-scan block) had no
  overload protection at all -- signal confirmed saturating partway
  through the scan on real hardware.** The scan sweeps a physical field
  (`coil_scan_start_a` to `coil_scan_stop_a`), so unlike `tau_mw` in the
  main sweep (a timing parameter that doesn't itself change signal
  strength independent of contrast), signal strength genuinely varies --
  often non-monotonically -- across the current range, and the
  sensitivity chosen before the scan started can saturate partway
  through. The main `tau_mw` sweep loop already had a per-point rescale-
  retry loop for exactly this; the coil-scan block, added later, never
  got one -- it just called `lia.read_xy()` directly per repeat with no
  overload check. **Fixed**: added the same `auto_rescale_on_overload`/
  `max_rescale_attempts`-gated rescale loop (`_step_sensitivity_coarser()`
  + re-settle + re-check, reusing `cmd_run()`'s own settings for both)
  right before each repeat's `read_xy()` inside the coil-scan's per-
  current loop. Since X/Y are absolute-volt readings (not normalized to
  the active sensitivity), a sensitivity change partway through the scan
  doesn't bias the R comparison across currents as long as no point is
  actually clipped -- only ever steps coarser (never finer) since the
  scan is a monotonic increasing current sweep and saturation is expected
  to occur going forward (higher current/signal), not backward. Not yet
  confirmed on real hardware.

- **Added the same coil current scan to `pulsed_odmr.py`.** Physics
  reasoning (from the user): the resonance sweep (`use_resonance_sweep`,
  `E4403B`-based, low power) finds `f0_hz` -- the MW delivery resonator's
  OWN peak, an antenna/circuit property, not the NV's. `coil_current_a`
  sets the static field and therefore the NV's Zeeman-shifted transition
  frequency. Scanning coil current while driving AT `f0_hz` with the real
  pulsed sequence and watching the lock-in signal finds the field that
  brings the NV's actual transition into coincidence with the resonator's
  peak -- maximizing the signal the subsequent frequency sweep (still
  centered on `f0_hz`/`fwhm_hz`) then traces the lineshape around. Same
  idea as `rabi.py`'s `use_resonance_freq` coil scan, but simpler here:
  `pulsed_odmr.py` already fixes `tau_mw_us` and uploads the pulse
  sequence ONCE in step 1 (unlike `rabi.py`, which sweeps `tau_mw` and
  needed its own `coil_scan_tau_mw_us`) -- so the coil scan just reuses
  `tau_mw_us` and the already-configured AWG/trigger directly, no
  reconfigure needed. Inserted right after `f0_hz`/`fwhm_hz` are found
  (before computing `start_hz`/`stop_hz`), gated on `use_resonance_sweep`
  (same gating rabi.py uses for its coil scan, since it needs a resolved
  peak frequency to drive at). New params: `coil_scan_start_a=1.0`,
  `coil_scan_stop_a=4.0`, `coil_scan_step_a=0.5`,
  `coil_scan_n_repeats=5`, `coil_scan_current_settle_s=2.0` (same
  defaults as `rabi.py`'s). Includes the same per-current overload
  rescale-retry loop as `rabi.py`'s coil scan (reusing `rabi.
  _step_sensitivity_coarser()`/`auto_rescale_on_overload`/
  `max_rescale_attempts`), since signal strength genuinely varies across
  the current range and can saturate whatever sensitivity was picked
  beforehand. `coil_current_a` is now also written to
  `_pulsed_odmr_metadata.txt` (wasn't tracked there before). RF/gen is
  left on at `f0_hz` after the scan -- harmless, since the frequency loop
  does its own `gen.preset()`/`set_frequency_hz(freqs_hz[0])`/`rf_on()`
  before starting anyway. Not yet run on real hardware.

- **Bug caught by the user before ever running `pulsed_odmr.py` on real
  hardware: the ZYSWA switch wasn't held static during the resonance
  sweep.** `pulsed_odmr.py`'s step 1 configured the REAL gated pulse
  sequence (`rabi.setup_awg_sequences()`, CH2 toggling per the MW on/off
  gate) BEFORE step 2's `resonance_sweep()` call -- so CH2 was already
  chopping between the sample path and the dump path while the spectrum
  analyzer tried to read a clean reflected-power trace, exactly the
  problem `cw_odmr_lock_in.py`'s `set_switch_static()` exists to prevent
  (see its docstring: "if CH2 is already chopping while resonance_sweep()
  reads the analyzer, the analyzer sees RF flipping between the sample
  path and the dump path on every chop cycle -- amplitude-modulation
  sidebands/garbage riding on top of the resonance dip, not a clean
  reflected-power trace").

  Fix is NOT as simple as calling `set_switch_static()` before an
  otherwise-unchanged step 1, though, because `rabi.py`'s `setup_awg_
  sequences()` requires CH1 and CH2 to wrap together via the shared
  `onceWaitTrig` anchor mechanism (its own docstring: "CH1 and CH2 MUST
  use the same value and wrap together... applying it to only one channel
  visibly misaligns them") -- there's no safe partial state where CH1
  pulses normally while CH2 sits static, unlike `cw_odmr_lock_in.py` where
  CH1 is just a plain continuous carrier with no synchronization
  dependency on CH2 at all. **Fixed** by restructuring the ordering
  instead of patching around it: step 1 now holds CH2 static (`set_
  switch_static(awg, route_to_sample=True)`) and does NOT configure the
  real pulse sequence or the SDG1062X external trigger yet when `use_
  resonance_sweep=true`; CH1 is left unconfigured too (harmless -- the
  resonance sweep is a pure RF reflection/transmission measurement via
  the spectrum analyzer, entirely independent of the laser/PMT signal
  path). Once `resonance_sweep()` finds `f0_hz`/`fwhm_hz`, a new step 2
  block calls `rabi._configure_external_trigger()` + `rabi.setup_awg_
  sequences()` for the first time, starting the real synchronized pulse
  sequence -- THEN the coil-current scan and frequency sweep run as
  before, both of which genuinely need the real gated lock-in detection.
  When `use_resonance_sweep=false` (no resonance sweep, manual `start_hz`/
  `stop_hz`), the pulse sequence is configured immediately in step 1 as
  before, since there's no low-power scan to interfere with. Not yet run
  on real hardware.

- **`anchor_free_reps` sized like rabi.py's per-point case is NOT enough
  for `pulsed_odmr.py` -- caught by the user reasoning about it, then
  confirmed against real data (`pulsed1`/`pulsed2`/`pulsed3`).** rabi.py
  calls `setup_awg_sequences()` FRESH at the start of every point's settle
  window, so as long as `anchor_period_s` (`ref_period_s * anchor_free_
  reps`) exceeds that ONE settle window, the sequence's wrap simply hasn't
  happened yet by the time that point's measurement is taken --
  deterministic non-overlap. `pulsed_odmr.py` uploads the sequence ONCE
  and free-runs it, wrapping over and over, asynchronously, for the
  ENTIRE multi-point sweep (often 100+ points, minutes long) -- whether
  any given point's `time.sleep()`-timed settle window happens to overlap
  a wrap is essentially random, and sizing `anchor_free_reps` against a
  single settle window (rabi.py's reasoning) only reduces the wrap RATE,
  not the total number of wrap events across a long sweep.

  Back-of-envelope check against the observed data: at `anchor_free_
  reps=1000`, `tau_mw_us=5.0`, `n_reps=250` -> `anchor_period_s=6.0 s`,
  `dwell_settle_s=0.9 s`; `pulsed1`'s ~137s estimated sweep duration ->
  ~23 wrap events, each with a naive `dwell_settle_s/anchor_period_s`
  (~15%) chance of clipping some point's settle window -> ~3.4 expected
  corrupted points. Observed: 5/144. Same order of magnitude, confirming
  the mechanism (this is NOT the same "stale reconfigure-transient latch"
  class of bug fixed elsewhere -- it's a real, ongoing, probabilistic
  overlap that persists for the entire run at this `anchor_free_reps`).

  **Fixed**: moved `start_hz`/`stop_hz`/`freqs_hz` computation to run
  immediately after `resonance_sweep()` finds `f0_hz`/`fwhm_hz` (previously
  computed later, after the coil scan) so the real sweep length is known
  before the pulse sequence is uploaded. Added a new warning check
  comparing `anchor_period_s` against an ESTIMATED TOTAL sweep duration
  (coil-current scan duration + main frequency sweep duration, both of
  which run on the same uploaded sequence) rather than just one settle
  window, printing a suggested `anchor_free_reps` (with 1.5x margin) sized
  to exceed the whole run if the current setting is insufficient. Doesn't
  auto-resize (would silently change the SDG1062X trigger rate to
  something potentially very slow without the user choosing it) -- just
  gives an actionable number, same spirit as the existing single-settle-
  window warning at the top of `cmd_run()`. Making `anchor_period_s`
  exceed the whole sweep is essentially free: `anchor_free_reps` is a
  sequence-table REPEAT COUNT, not a re-uploaded waveform, so it doesn't
  cost AWG memory (see `setup_awg_sequences()`'s docstring). Not yet run
  on real hardware to confirm the suggested value actually eliminates the
  unlocks.

- **The suggested fix above (`anchor_free_reps=50000`) caused a much worse
  regression on real hardware -- a design flaw in `_configure_external_
  trigger()`'s call sites, not another anchor-wrap coincidence.** At
  `anchor_free_reps=50000`, `reference_unlock=True` stopped being an
  occasional flag and became PERSISTENT across many consecutive points,
  with the SR830's reported reference frequency stuck at `999.989 Hz`
  (not the expected `166.667 Hz`) -- consistent with the SR830 free-
  running at its own internal default when the reference input has been
  genuinely ABSENT for a while, not a brief dropout.

  Root cause: `_configure_external_trigger(sdg, anchor_period_s, margin=
  trigger_margin)` derives the SDG1062X's retrigger rate directly from
  `anchor_period_s` (`= ref_period_s * anchor_free_reps`) -- fine when
  `anchor_free_reps` is modest (1000), but at `50000` this stretched the
  trigger's own period out to `anchor_period_s / trigger_margin ~= 100s`.
  The periodic interlock check does its own `awg.write("ABOR")`, which
  forces the sequence back to its `onceWaitTrig` anchor waiting for the
  NEXT trigger edge -- which could then be up to ~100s away. Every point
  measured during that window reads garbage (no real reference at all),
  explaining the long, persistent run of corrupted points instead of the
  previous rare, brief ones.

  The real issue: the SDG's retrigger rate and `anchor_free_reps` answer
  two DIFFERENT questions that had been coupled through the same `anchor_
  period_s` value. `anchor_free_reps` should be large, to make the
  sequence's OWN natural wrap rare across a long sweep (the fix two
  entries up). But the trigger itself should always stay FAST, so recovery
  from ANY reset -- a natural wrap OR a forced `ABOR` -- is quick,
  regardless of how large `anchor_free_reps` is sized for the sequence
  table. **Fixed**: added `trigger_retrigger_free_reps` (default `1000`,
  independent of `anchor_free_reps`) and `trigger_period_s = ref_period_s
  * min(anchor_free_reps, trigger_retrigger_free_reps)`, used at both
  `_configure_external_trigger()` call sites instead of `anchor_period_s`
  directly -- keeps the same validated `anchor_free_reps=1000`-equivalent
  trigger rate this codebase has always used, no matter how large `anchor_
  free_reps` itself is sized for a long sweep. `trigger_period_s` is now
  also printed alongside `anchor_period_s` in the startup log for
  visibility. Not yet re-run on real hardware to confirm this resolves the
  persistent-unlock regression.

- **Raised `rabi.py`'s `max_rescale_attempts` default from `3` to `10`**
  (`cmd_run()` and `cmd_run_no_mw()`; `RUN_PARAMS_HELP` updated to match).
  Each rescale attempt only steps ONE sensitivity range coarser
  (`_step_sensitivity_coarser()`) out of 27 total (`SR830.SENSITIVITY_V`),
  so 3 attempts isn't much headroom if the actual signal is many ranges
  away from whatever sensitivity was active when an overload hits (e.g.
  right after a big coil-current jump or a resonance shift) -- the old
  default would give up and save an as-is (likely still railed/clipped)
  reading well before reaching the correct range in that case. `cmd_
  calibrate_phase()`'s own hardcoded `max_rescale_attempts=5` (added
  earlier, see the phase-calibration overload entry above) was left
  as-is, since it was already above the old default of 3.

- **Added log-spaced `tau_mw` sweeping to `cmd_run()`.** New `mw_spacing`
  (default `linear`, unchanged behavior) and `mw_log_ratio` (default
  `1.15`) params: `mw_spacing=log` builds `mw_values_us` as a geometric
  progression (`mw_start_us * mw_log_ratio ** arange(n)`) instead of
  `np.arange(mw_start_us, mw_stop_us, mw_step_us)`, stopping at the last
  point not exceeding `mw_stop_us`; `mw_step_us` is ignored in this mode.
  Lets a single sweep cover a wide `tau_mw` range (e.g. 10 ns to 3 us,
  example from the user) with fine resolution at short `tau_mw` (where
  the Rabi oscillation is fastest) without the huge point count a linear
  step fine enough for that would need all the way to the long end.
  `mw_start_us` must be > 0 in this mode (raises `ValueError` otherwise --
  a geometric progression can't start at 0). Verified the math directly:
  `mw_start_us=0.01, mw_stop_us=3.0, mw_log_ratio=1.15` gives 41 points
  from 0.01 to ~2.68 us, never exceeding `mw_stop_us`. Only implemented in
  `cmd_run()` (the main Rabi sweep) -- not added to `cmd_run_ch1_ch2_
  constant()` (the crosstalk-isolation diagnostic variant, which has an
  identical `mw_start_us`/`mw_stop_us`/`mw_step_us` block) since that's
  out of scope for what was asked. Metadata doesn't need updating for
  this -- it already doesn't record `mw_start_us`/`mw_stop_us`/`mw_step_
  us` at all, since the actual realized sweep points are captured
  directly via the saved `_rabi_mw_us.npy` array regardless of spacing
  mode. Not yet run on real hardware.

- **Added `calibrate-phase` to `pulsed_odmr.py`** (`python pulsed_odmr.py
  calibrate-phase <file_name> freq_hz=... [key=value ...]`). pulsed_odmr.
  py had no phase-calibration path at all before this -- `cmd_run()`
  applies whatever `phase_deg` is passed in, same gap `rabi.py`'s own
  `cmd_calibrate_phase()` exists to fill. Adapted line-for-line from
  `rabi.py`'s version (reusing `rabi.setup_lock_in()`/`rabi.setup_awg_
  sequences()`/`rabi._configure_external_trigger()`/`rabi._step_
  sensitivity_coarser()` via the existing `import rabi`), with the ONE
  meaningful difference: `rabi.py`'s version fixes `tau_mw` and requires
  a `freq_hz`; this one fixes `tau_mw_us` (this module's convention,
  matching `cmd_run()`) and requires a `freq_hz` to run at -- pick
  whatever frequency showed the strongest signal in a previous `cmd_run()`
  ODMR sweep, same "needs real contrast, not an arbitrary point" guidance
  as `rabi.py`'s docstring. Carries over every real-hardware fix already
  found in `rabi.py`'s version: discard reads after `setup_awg_sequences()`
  and after `auto_phase()` (clears reconfigure/phase-step transients),
  the `AGAN`-undershoot rescale-retry loop, and the R-vs-full-scale
  signal-strength check (catches the too-weak failure mode overload alone
  misses). Also uses this module's own `trigger_period_s`/`trigger_
  retrigger_free_reps` decoupling from `anchor_free_reps` (added earlier
  this session) for consistency, though a single calibration point is
  short enough that it rarely matters in practice. Not yet run on real
  hardware.

- **Added `run-repeat` to `pulsed_odmr.py`** (`python pulsed_odmr.py
  run-repeat <file_name> n_repeats=10 [key=value ...]`). Adapted from
  `rabi.py`'s `cmd_run_repeat()` -- same subprocess-per-repeat design
  (avoids cross-repeat resource accumulation), same shared-folder-via-
  `output_dir`-override pattern (which `cmd_run()` didn't have at all
  until now -- added `output_dir` the same way `rabi.py`'s `cmd_run()`
  has it, defaulting to `file_name`), same repeat-0-pins-later-repeats
  idea. Two adaptations for this module's different sweep axis:
  (1) `rabi.py` pins a single resolved `freq_hz`; this module sweeps
  FREQUENCY, so what `use_resonance_sweep` resolves is a whole RANGE --
  repeat 0's actual realized `start_hz`/`stop_hz` are read back from its
  saved `_pulsed_odmr_freqs_hz.npy` (min/max) rather than a metadata
  field, and passed explicitly (with `use_resonance_sweep=false`) to
  every later repeat. (2) `rabi.py`'s version keeps a separate coarse-
  sweep-plus-pre-flight-check running on pinned repeats, only skipping
  the fine stage; this module has no equivalent standalone pre-flight
  check outside `resonance_sweep()` itself, so pinned repeats skip the
  WHOLE resonance sweep -- doesn't lose any independent safety check,
  since `interlock_during_sweep`'s periodic check during the main
  frequency loop is the real per-point safety net and stays active on
  every repeat regardless. `coil_current_a` pinning (for when `use_
  resonance_sweep` also ran the coil-current scan) works the same as
  `rabi.py`'s version -- read back from repeat 0's metadata. Saves
  `_avg_pulsed_odmr_freqs_hz.npy`/`_x.npy`/`_y.npy`/`_r.npy`/`_metadata.
  txt` (records `n_repeats_requested` vs. `n_repeats_averaged`), same
  convention as `rabi.py`'s `_avg_rabi_*` files. Confirmed working on real
  hardware (`pulsed_repeat`, 5+ repeats averaged cleanly).

- **Gave `pulsed_odmr.py` its own dedicated data directory**
  (`DATA_DIR = "D:\\pulsed_odmr"`, was `rabi.DATA_DIR` i.e. `D:/rabi`).
  Matches `cw_odmr.py`'s/`cw_odmr_lock_in.py`'s convention of each
  measurement TYPE getting its own top-level folder, instead of mixing
  pulsed ODMR frequency sweeps into `rabi.py`'s own `tau_mw`-sweep folder.
  Only affects NEW runs -- existing data (`pulsed1-5`, `pulsed_repeat`,
  etc.) stays under `D:/rabi` where it was already saved and already
  analyzed in `pulsed_odmr_result.ipynb`; not moved automatically. Any
  new `pulsed_odmr.py` runs from now on need `DATA_DIR = "D:/pulsed_odmr"`
  in the analysis notebook instead of `"D:/rabi"`.

- **Added `tau_mw_us_list` to `pulsed_odmr.py`'s `run-repeat`** (e.g.
  `tau_mw_us_list=0.5,1.0,2.0,5.0`), mirroring `cw_odmr_lock_in.py`'s
  `drive_power_dbm_list` on its own `cmd_sweep_average()`. For EACH
  `tau_mw_us` value, runs its own independent batch of `n_repeats`
  repeats -- that batch's OWN repeat 0 finds/pins its own resonance range
  and coil current (not shared across `tau_mw_us` values, for simplicity,
  matching `cw_odmr_lock_in.py`'s per-power independence even though the
  resonance frequency itself doesn't actually depend on `tau_mw`), and
  averages within that batch alone. New `_tau_tag()` helper (mirrors
  `cw_odmr_lock_in.py`'s `_power_tag()`) makes output files filesystem-
  safe-tagged per `tau_mw_us` value (e.g. `0.5` -> `0p5us`) so all
  batches still land in the same shared folder
  (`D:/pulsed_odmr/<file_name>/`) without colliding:
  `<file_name>_tau0p5us_repeat0_...`, `<file_name>_tau0p5us_avg_...`,
  etc. Without `tau_mw_us_list`, behavior is unchanged (no tag, plain
  `<file_name>_repeat{i}_...`, exactly as before). Not yet run on real
  hardware.

- **Added `tests/amplifier_power_scan.ipynb`** -- a proper amplifier
  power/compression sweep, companion to `tests/new_amplifier_gain.ipynb`
  (which only ever sampled TWO fixed drive powers, -40 and 0 dBm, across
  a frequency sweep). This new one fixes frequency and sweeps `power_dbm`
  continuously from -40 to 0 dBm (`POWER_STEP_DBM=2.0` default) to trace
  a real P_out-vs-P_in compression curve and compute P1dB (1 dB
  compression point, linearly interpolated between bracketing points,
  referenced to the lowest-power/least-compressed point's small-signal
  gain). Uses the SAME coupled physical path (amplifier -> isolator ->
  coupler -> load, forward-coupled port -> E4403B) as `new_amplifier_
  gain.ipynb`'s "Compression point" section, for the WHOLE sweep (not
  just the high end) -- 0 dBm in plus ~30 dB of gain is unsafe direct
  into the analyzer at any point in this range, not only at the top.
  `FIXED_FREQ_HZ` defaults to 2.5 GHz (band center, no more specific
  target given) -- docstring notes 2.22 GHz (this amplifier's own
  measured peak-output frequency at 0 dBm, from the already-saved
  `new_amplifier_max_output_0dbm.csv`) and ~2.84 GHz (the actual NV drive
  frequency used elsewhere in this project) as alternatives depending on
  whether the goal is characterizing the amplifier itself or real
  operating performance.

  Settling time: `hp8673h.py`'s `frequency_sweep()` docstring documents a
  confirmed real GPIB-race bug on this generator (`settle_s=0` -> false
  >13 dB "dips" at specific frequencies, since the analyzer's `INIT:IMM`
  fires before the generator finishes processing the previous SCPI
  command). That bug was characterized for FREQUENCY steps, but a `PL...
  DB` power-level command still has to settle through the generator's own
  leveling loop, so the same race is plausible for POWER steps too --
  applied the identical defense: `INITIAL_SETTLE_S=1.0` once after the
  first power/frequency jump (mirrors `frequency_sweep()`'s `initial_
  settle_s`), a smaller but still nonzero `SETTLE_S=0.05` between each
  subsequent power step (never 0), and the same `INIT:IMM` + `*OPC?`
  synchronization before reading the marker (the actual defense against
  the race -- the sleep alone reduces the odds but `*OPC?` is what
  confirms the analyzer's own measurement genuinely completed first).

  Saves `data/new_amplifier_power_scan.csv` (`input_power_dbm`,
  `coupled_power_dbm`, `estimated_output_dbm`, `gain_db`) and `data/
  new_amplifier_power_scan_metadata.txt` (`fixed_freq_hz`, `coupling_db`,
  `small_signal_gain_db`, `p1db_dbm`), plus a standalone "Analysis from
  saved data" section reproducing both plots + P1dB from disk, same
  pattern as `new_amplifier_gain.ipynb`'s own reload section. Validated
  the notebook's JSON structure and that every code cell parses as valid
  Python -- NOT yet run against real hardware (needs the HP8673H/E4403B
  connected, which this environment doesn't have).
