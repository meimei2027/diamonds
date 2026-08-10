"""
rabi.py -- pulsed Rabi oscillation measurement: sweeps the microwave pulse
length (tau_mw) at a FIXED, known ODMR resonance frequency, reading out via
lock-in synchronous detection against a slow block-chopped reference
(n_reps loop iterations with the MW pulse present, then n_reps without),
instead of cw_odmr_lock_in.py's continuous fast chop.

AWG sequence upload (setup_awg_sequences() below) mirrors what was verified
on real hardware in tests/rabi_awg_marker_test.ipynb -- see notes.md's
"Rabi oscillation" section for the full debugging history. Key points:

- The lock-in reference marker lives on CH2's own DATA:SEQ sequence
  (highAtStart/lowAtStart on its mw-on/mw-off blocks), not CH1's -- there
  is only one physical Sync/Marker BNC, shared between channels and
  switched to CH2 via OUTPut:SYNC:SOURce, so the reference is correct
  regardless of any residual CH1/CH2 timing offset.
- CH1/CH2 relative timing (does the laser pulse land in the intended place
  relative to the MW pulse) is kept aligned using the Keysight manual's
  documented "start a sequence on a trigger" technique (p.181): a brief
  DC "anchor" segment marked "onceWaitTrig" at the front of each channel's
  sequence. Both channels hold at their anchor until a shared external
  trigger edge advances them together. This REQUIRES a second instrument
  (the Siglent SDG1062X, see SDG_RESOURCE) supplying a CONTINUOUS,
  sufficiently fast square wave into the AWG's rear-panel Ext Trig BNC --
  every full sequence loop wraps back to the anchor and needs a fresh
  trigger, so a one-time edge is not enough; the SDG1062X's frequency is
  reconfigured every sweep point since it must stay at or above the
  current point's own block-cycle rate (see _configure_external_trigger()).
  `PHASe:SYNChronize` and plain `TRIG:SOUR`/burst-mode approaches were all
  tried first and confirmed broken/inapplicable on this firmware -- see
  notes.md before reintroducing any of them.

See notes.md's "cw_odmr_lock_in.py operation notes" section for the
lock-in/PSU conventions reused here.

Interlock note: reflected power is checked with the spectrum analyzer in
MAX HOLD mode (HP8673H.read_max_hold_reflected_power_dbm()), not a single
sweep. A single sweep (read_reflected_power_dbm(), used everywhere else in
this codebase) has no way to reliably land inside a microsecond-scale MW
pulse -- GPIB round-trip time plus the analyzer's own sweep time both run
far slower than that. MAX HOLD instead free-runs for several full
reference periods, so it's guaranteed to catch at least one real pulse at
its peak somewhere in that window -- trading timing precision for a
hardware guarantee. Each check takes multiple reference periods to
complete, so it only runs periodically (interlock_check_interval), not on
every sweep point.

Usage:
    python rabi.py run <file_name> [key=value ...]
        Sweeps the MW pulse length across [mw_start_us, mw_stop_us],
        recording the SR830's X/Y at each point. See cmd_run()'s docstring
        for the full list of key=value overrides. Saves
        data/<file_name>/<file_name>_rabi_mw_us.npy, _rabi_x.npy,
        _rabi_y.npy, _rabi_reflected_dbm.npy, _rabi_ref_freq_hz.npy,
        _rabi_reference_unlock.npy, _rabi_metadata.txt.

    python rabi.py run-no-mw <file_name> [key=value ...]
        Crosstalk-isolation diagnostic -- a separate, self-contained sweep
        (same AWG sequences, same CH2 gate/switch toggling, same sweep)
        that never connects to or commands the generator, amplifier
        supply, or coil supply at all -- turn the generator's RF off/
        disconnect it, and power down both PSUs, yourself before running
        this. See cmd_run_no_mw()'s docstring for why (isolating whether a
        spurious off-resonance/no-MW-near-sample signal found on real
        hardware -- confirmed to persist even with RF off -- also depends
        on either PSU being powered). Confirmed on real hardware to
        persist with the generator's RF AND both PSUs off.

    python rabi.py run-ch2-constant <file_name> [key=value ...]
        Crosstalk-isolation diagnostic, second stage -- builds on
        run-no-mw (no generator, no interlock, no PSUs) and additionally
        holds CH2's own analog output physically constant (never
        switching) while its marker/reference still toggles normally. See
        cmd_run_ch2_constant()'s docstring for why (isolating whether the
        spurious signal needs CH2's analog output to actually switch, or
        only needs the marker/reference toggle). Confirmed on real
        hardware to persist even with CH2 held constant.

    python rabi.py run-ch1-ch2-constant <file_name> [key=value ...]
        Crosstalk-isolation diagnostic, third stage -- builds on
        run-ch2-constant and additionally holds CH1 constant too (a
        plain, non-sequenced continuous 80 MHz sine, no DATA:SEQ at all).
        See cmd_run_ch1_ch2_constant()'s docstring for why (isolating
        whether the spurious signal needs CH1's OWN sequence table to be
        advancing through segments, even ones with identical output, or
        only needs CH2's marker/reference toggling).

    python rabi.py calibrate-phase <file_name> [key=value ...]
        Runs continuously at a single, fixed tau_mw and calls the SR830's
        auto-phase (APHS) to null Y against the real reference -- phase_deg
        is NOT auto-calibrated by cmd_run() itself, so run this first and
        pass the result into cmd_run() via phase_deg=<value>. See
        cmd_calibrate_phase()'s docstring for key=value overrides.

Example:
    python rabi.py run rabi1 freq_hz=2.8692e9 mw_stop_us=4.0
    python rabi.py run-no-mw rabi_no_mw_test
    python rabi.py run-ch2-constant rabi_ch2_const_test
    python rabi.py run-ch1-ch2-constant rabi_ch1_ch2_const_test
    python rabi.py calibrate-phase phasecal1 mw_us=1.2
"""
import sys
import time

import numpy as np

import ks33600a
from hp8673h import HP8673H
from sr830 import SR830
from spd1305x import SPD1305X, voltage_for_current
from spd1168x import SPD1168X
from sdg1062x import SDG1062X
from cw_odmr import parse_kv_args, _tee_stdout
from cw_odmr_lock_in import set_switch_static

AWG_RESOURCE = "USB0::0x0957::0x5707::MY53800810::INSTR"
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"
SR830_RESOURCE = "GPIB2::2::INSTR"
# Siglent SDG1062X, driven by sdg1062x.py -- supplies the continuous
# external trigger the AWG's "onceWaitTrig" anchor segments need to keep
# CH1/CH2 aligned (see module docstring / notes.md). NOT used via its own
# run() method (that configures it to be externally triggered itself, the
# opposite of what's needed here) -- configured directly as a plain
# continuous square-wave source by _configure_external_trigger() below.
SDG_RESOURCE = "USB0::0xF4EC::0x1103::SDG1XDDX6R5043::INSTR"
# NOT YET VERIFIED -- confirm with pyvisa.ResourceManager().list_resources()
# before trusting this (see notes.md's GPIB-bus-numbering gotchas).
# SPD1305X driving the static-field coil (see spd1305x.py) -- chosen for
# this role over the SPD1168X for its higher current limit.
COIL_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCD7R1877::INSTR"
# SPD1168X driving the RF amplifier supply.
AMP_PSU_RESOURCE = "USB0::0xF4EC::0x1410::SPD13DCQ7R0986::INSTR"

DATA_DIR = "D:\\rabi"

RESEQUENCE_INTERVAL = 4  # reset the AWG this often to clear out accumulated
                          # DATA:SEQ sequences before hitting its "too many
                          # sequences defined" limit -- same lesson as
                          # t1_test.py's RESEQUENCE_INTERVAL. Lowered from 20
                          # now that each point's combined arb (n_reps copies
                          # of on/off baked into one waveform) is much larger,
                          # so resetting more often also keeps the AWG's arb
                          # waveform memory from accumulating/fragmenting
                          # across points, not just the sequence table.
                           # NOT yet re-validated against anchor_free_reps:
                           # each point's own sequence is now much larger
                           # (up to ~2*anchor_free_reps+1 listed segments,
                           # vs. 3 before), a DIFFERENT AWG resource (total
                           # listed segments within one sequence, see
                           # setup_awg_sequences()'s docstring) than the
                           # "too many sequences defined" limit this
                           # interval was tuned against (total DISTINCT
                           # sequence names accumulated). Watch for errors
                           # here on real hardware -- may need retuning.


def build_block_descriptor(sequence_name, segments):
    """Same DATA:SEQ block-descriptor builder as t1_test.py/pulsed_odmr.py."""
    parts = [f'"{sequence_name}"']
    for arb_name, repeat_count, play_control, marker_mode, marker_point in segments:
        parts.append(
            f'"{arb_name}",{repeat_count},{play_control},{marker_mode},{marker_point}'
        )
    payload = ",".join(parts)
    payload_bytes = payload.encode("utf-8")
    payload_len = len(payload_bytes)
    n = len(str(payload_len))
    return f"#{n}{payload_len}{payload}"


def _us_to_samples(duration_us, sample_rate_hz):
    return max(1, round(duration_us * 1e-6 * sample_rate_hz))


def _rf_pulse(freq_hz, n_samples, sample_rate_hz):
    t = np.arange(n_samples) / sample_rate_hz
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32)


def _const(n_samples, value):
    return np.full(n_samples, value, dtype=np.float32)


ANCHOR_SAMPLES = 32  # minimum DATA:SEQ segment length on the 33600 Series


def setup_awg_sequences(awg, mw_us, n_reps, laser_us, pre_us, post_us,
                         sequence_name_ch1, sequence_name_ch2,
                         ch1_vpp=0.632, ch2_vpp=5.0, ch2_offset_v=2.5,
                         sample_rate_hz=1e9, ch2_hold_constant=False,
                         ch1_hold_constant=False, anchor_free_reps=1):
    """
    Uploads CH1's laser-pulse-train arb and CH2's mw-on/mw-off gate arbs
    for one rep at the current mw_us, then builds and uploads a DATA:SEQ
    sequence per channel, and configures both channels' output. Verified
    on real hardware in tests/rabi_awg_marker_test.ipynb -- see notes.md's
    "Rabi oscillation" section for the debugging history behind every
    choice below.

    ch2_hold_constant=True (crosstalk-isolation diagnostic, see notes.md's
    "spurious off-resonance/no-MW-near-sample signal" entry and
    cmd_run_ch2_constant()'s docstring): the marker (asserted/negated via
    "highAtStartGoLow", which is what the lock-in's reference is actually
    derived from via OUTPut:SYNC:SOURce CH2) and CH2's own ANALOG output
    value are independent attributes of a DATA:SEQ segment -- normally
    they change together (the combined arb's on-half uses gate_on_rep's
    real gate content, off-half uses gate_off_rep), but they don't have
    to. With this on, the arb's "on" half is built from gate_off_rep's
    content too (constant, physically off -- normalized -1.0, i.e. 0V
    once ch2_offset_v is applied) while the marker still asserts/negates
    at the same marker_point -- so CH2's real analog output voltage never
    actually changes at all (no physical switching happens downstream),
    but the lock-in still receives an identical, correctly-toggling
    reference. Isolates whether a spurious signal genuinely requires
    CH2's own analog output to physically switch, or only requires the
    marker/sync output to toggle (which alone would point at the marker/
    Sync BNC circuitry specifically, not CH2's analog DAC/switch-driving
    output).

    anchor_free_reps (applies to BOTH channels, regardless of
    ch2_hold_constant/ch1_hold_constant -- see notes.md's "spurious
    off-resonance/no-MW-near-sample signal" entry for the full history):
    each onceWaitTrig anchor's own LEVEL isn't the concern -- CH1's
    anchor (0.0) matches its own real "off" gap value, and CH2's anchor
    (-1.0) matches gate_off_rep's value. The concern is the trigger-wait
    DEAD TIME itself: every time a sequence wraps back through its
    anchor, it sits there for a small, asynchronous delay before the next
    external trigger edge arrives, delaying exactly when the next cycle's
    real content resumes -- a real, reproducible glitch landing at the
    SAME point in every cycle (see cmd_run_ch2_constant()'s docstring for
    how this was isolated on real hardware). anchor_free_reps sets how
    many full off+on (or 2*n_reps-rep, for CH1) cycles play as ONE
    combined, pre-baked arb before wrapping back to the anchor -- CH1 and
    CH2 MUST use the same value and wrap together (confirmed on real
    hardware that applying it to only one channel visibly misaligns them,
    CH1's pulses drifting relative to the Sync/marker output). Each
    channel's combined arb is listed exactly ONCE with repeat_count=
    anchor_free_reps -- a SINGLE table entry regardless of how large
    anchor_free_reps is (repeat count is free, like n_reps already is),
    unlike an earlier version of this that listed anchor_free_reps
    separate off+on/rep PAIRS (2*anchor_free_reps table entries), which
    hit a real ~250-listing AWG sequence-table ceiling on real hardware.
    The tradeoff moved from table-entry count to waveform memory/upload
    time instead: the combined arb is n_reps times bigger than a single
    rep -- confirmed working on real hardware up to n_reps=500 (see
    tests/rabi_combined_arb_marker_test.ipynb).

    ch1_hold_constant=True (paired crosstalk-isolation diagnostic, see
    cmd_run_ch1_ch2_constant()'s docstring): configures CH1 as a plain,
    non-sequenced continuous FUNC SIN at 80 MHz / ch1_vpp instead of
    building/uploading its own DATA:SEQ sequence at all -- no "anchor"/
    "ch1_combined" arbs, no onceWaitTrig, no external-trigger dependency
    for CH1. With ch1_hold_constant on, CH1 is fully decoupled from the
    block structure -- if ch2_hold_constant is also on, the ONLY thing
    left anywhere in the AWG still synchronous with the block reference
    is CH2's marker.

    REVERTED an attempted split (configure_awg_outputs() called once,
    output-stage config removed from here) that was meant to cut down on
    suspected relay wear from reissuing VOLT/OUTP:LOAD/OUTPUT ON/etc every
    sweep point -- on real hardware this did NOT reduce clicking and the
    resulting sequence/waveforms looked wrong, so it's reverted back to
    reconfiguring the full output stage every call, matching the last
    confirmed-working behavior. See notes.md for the (now retracted) theory
    and this revert.

    CH1: "ch1_combined" -- 2*n_reps copies of a bright laser pulse
    (laser_us) followed by a flat dark gap (pre_us + mw_us + post_us),
    concatenated into one arb -- CH1 doesn't care about the MW pulse's
    internal position or which half of the cycle is "on" vs. "off", just
    the total gap length, so it's the same content repeated throughout.

    CH2: "ch2_combined" -- gate_on_rep's content (high for mw_us, low for
    laser_us+pre_us+post_us) repeated n_reps times, followed by
    gate_off_rep's content (low for the entire rep) repeated n_reps times,
    concatenated into ONE arb. Each underlying rep has the SAME sample
    count as CH1's rep content (checked with an assert) so the two
    channels' blocks don't drift out of step.

    Both channels' sequences start with a brief "anchor" segment
    (ANCHOR_SAMPLES, "onceWaitTrig") before the real content -- the
    manual's documented "start a sequence on a trigger" technique
    (p.181). Each channel's anchor sits at THAT channel's own real
    "off"/rest level (0.0 normalized for CH1's 0-centered VOLTage
    convention, -1.0 normalized for CH2's unipolar 0-ch2_vpp mapping) --
    reusing one generic anchor value across both channels produces a
    brief wrong-level glitch on whichever channel doesn't use a 0-centered
    convention (confirmed on real hardware for CH2).

    The lock-in reference marker lives on CH2's sequence -- the
    "ch2_combined" listing is marked "highAtStartGoLow" (assert the
    marker high at the start of the segment, negate it low at
    marker_point, the sample index where the arb's on-content ends and
    off-content begins) -- OUTPut:SYNC:SOURce is set to CH2 here so the
    shared Sync/Marker BNC reflects it, independent of CH1/CH2 relative
    timing. Confirmed on real hardware that "highAtStartGoLow" re-fires
    on EVERY repeat of the segment, not just once across the whole
    anchor_free_reps-repeat block (see tests/rabi_combined_arb_marker_
    test.ipynb) -- this is what makes collapsing to one listing per
    channel actually give the same alternating reference as before.

    Clears both channels' volatile arb memory first: DATA:ARBitrary errors
    if an arb name already exists, and upload_waveform() doesn't check for
    that error (see notes.md), so reusing the same arb names across sweep
    points without clearing would silently leave stale data in place.

    sequence_name_ch1/sequence_name_ch2 must be unique per distinct mw_us
    used since the AWG's sequence table was last cleared (see
    RESEQUENCE_INTERVAL) -- re-uploading a DATA:SEQ under an already-used
    name does not actually replace it (see notes.md / t1_test.py's
    upload_sequence() docstring).

    Both channels are set to TRIG:SOUR EXT -- this requires a continuous
    external trigger on the AWG's rear-panel Ext Trig BNC to ever actually
    play (see _configure_external_trigger()/cmd_run()'s SDG1062X setup);
    without it, both channels sit forever at their anchor outputting
    nothing.
    """
    # Stop whatever sequence is currently playing before clearing/
    # reuploading arb memory. Previously relied on DATA:VOL:CLE alone to
    # safely replace an actively-playing arb -- worked in practice, but
    # with anchor_free_reps now often long enough that a point's sequence
    # is still mid-cycle (not idled back at its anchor) when we move to
    # the next point (see notes.md), abort first so the old sequence is
    # actually stopped rather than implicitly cut off by the clear.
    awg.write("ABOR")
    # NOTE: forcing TRIG1/2:SOUR through an IMM->EXT transition here (on
    # the theory that *RST's benefit was incidentally flushing a stale
    # trigger edge via that transition) was tried and DID NOT fix it --
    # rabi_new20_test still hit the same ~1.3-1.5e-4 rail at 2 of 6 points
    # with the toggle in place. That theory is falsified; see notes.md.
    awg.write("SOUR1:DATA:VOL:CLE")
    awg.write("SOUR2:DATA:VOL:CLE")

    laser_samples = _us_to_samples(laser_us, sample_rate_hz)
    pre_samples = _us_to_samples(pre_us, sample_rate_hz)
    mw_samples = _us_to_samples(mw_us, sample_rate_hz)
    post_samples = _us_to_samples(post_us, sample_rate_hz)

    gate_on_rep = np.concatenate([
        _const(laser_samples + pre_samples, -1.0),
        _const(mw_samples, 1.0),
        _const(post_samples, -1.0),
    ])
    gate_off_rep = _const(laser_samples + pre_samples + mw_samples + post_samples, -1.0)

    if not ch1_hold_constant:
        ch1_rep = np.concatenate([
            _rf_pulse(80e6, laser_samples, sample_rate_hz),
            _const(pre_samples + mw_samples + post_samples, 0.0),
        ])
        assert len(ch1_rep) == len(gate_on_rep) == len(gate_off_rep), (
            "CH1 and CH2 rep arbs must have identical sample counts, or the "
            "two channels' blocks will drift out of step"
        )
        anchor_ch1 = _const(ANCHOR_SAMPLES, 0.0)
        awg.upload_waveform(anchor_ch1, arb_name="anchor", ch=1, sample_rate=sample_rate_hz)

        # ONE combined arb covering a full 2*n_reps-rep cycle (CH1 never
        # distinguishes on/off, so it's just 2*n_reps copies of the same
        # "rep" content back to back), listed ONCE with repeat_count=
        # anchor_free_reps -- a single table entry regardless of how large
        # anchor_free_reps is, unlike the old two-separate-n_reps-listing-
        # pair-repeated-anchor_free_reps-times approach (2*anchor_free_reps
        # entries, which hit a real ~250-listing AWG ceiling on real
        # hardware). Confirmed working on real hardware up to n_reps=500
        # via tests/rabi_combined_arb_marker_test.ipynb. See notes.md's
        # "spurious off-resonance/no-MW-near-sample signal" entry.
        ch1_combined = np.concatenate([ch1_rep] * (2 * n_reps))
        awg.upload_waveform(ch1_combined, arb_name="ch1_combined", ch=1, sample_rate=sample_rate_hz)

        block1 = build_block_descriptor(sequence_name_ch1, [
            ["anchor", "1", "onceWaitTrig", "maintain", 10],
            ["ch1_combined", str(anchor_free_reps), "repeat", "maintain", 10],
        ])
        awg.write(f"DATA:SEQ {block1}")  # unprefixed -> channel 1

    anchor_ch2 = _const(ANCHOR_SAMPLES, -1.0)
    awg.upload_waveform(anchor_ch2, arb_name="anchor", ch=2, sample_rate=sample_rate_hz)

    # ch2_hold_constant: use gate_off_rep's content for BOTH halves of the
    # combined arb -- CH2's real analog output never changes -- while the
    # marker still toggles via highAtStartGoLow at the same marker_point,
    # so the lock-in's reference is unaffected.
    #
    # ONE combined arb per cycle: on-content (n_reps copies) followed by
    # off-content (n_reps copies), marked "highAtStartGoLow" -- per the
    # Keysight manual, this asserts the marker high at the start of the
    # segment and negates it low at <marker point> (a sample index into
    # the arb, required to be in [4, N-3]). Confirmed on real hardware
    # (tests/rabi_combined_arb_marker_test.ipynb) that this re-fires on
    # EVERY repeat of the segment (not just once across the whole
    # anchor_free_reps-repeat block), giving the exact same alternating
    # low/high reference as the old two-listing "lowAtStart"/"highAtStart"
    # pair, but as ONE table entry regardless of anchor_free_reps --
    # confirmed working up to n_reps=500 real hardware.
    on_content = gate_off_rep if ch2_hold_constant else gate_on_rep
    ch2_combined = np.concatenate([on_content] * n_reps + [gate_off_rep] * n_reps)
    marker_point = len(on_content) * n_reps
    assert 4 <= marker_point <= len(ch2_combined) - 3, (
        f"marker_point={marker_point} outside the manual's required "
        f"[4, {len(ch2_combined) - 3}] range for a {len(ch2_combined)}-"
        f"sample arb -- shouldn't happen at any realistic n_reps/mw_us, "
        f"but check if it does"
    )
    awg.upload_waveform(ch2_combined, arb_name="ch2_combined", ch=2, sample_rate=sample_rate_hz)

    block2 = build_block_descriptor(sequence_name_ch2, [
        ["anchor", "1", "onceWaitTrig", "lowAtStart", 10],
        ["ch2_combined", str(anchor_free_reps), "repeat", "highAtStartGoLow", str(marker_point)],
    ])
    awg.write(f"SOUR2:DATA:SEQ {block2}")

    # CH1: laser/AOM drive.
    awg.write("OUTP1:LOAD 50")
    if ch1_hold_constant:
        # Plain continuous sine -- no ARB/DATA:SEQ at all for CH1, so
        # there's no sequence table for it to advance through and
        # therefore no internal segment-transition event anywhere on this
        # channel, block-synchronous or otherwise. Doesn't need an
        # external trigger (TRIG:SOURce only applies to burst/sweep, not
        # a plain continuous function -- see notes.md); TRIG1:* writes
        # below are skipped since they'd be meaningless here.
        awg.write("SOUR1:FUNC SIN")
        awg.write("SOUR1:FREQ 80e6")
        awg.write(f"SOUR1:VOLT {ch1_vpp}")
        awg.write("SOUR1:VOLT:OFFS 0")
    else:
        awg.write(f"SOUR1:FUNC:ARB:SRAT {sample_rate_hz}")
        awg.write(f'SOUR1:FUNC:ARB "{sequence_name_ch1}"')
        awg.write("SOUR1:FUNC ARB")
        awg.write(f"SOUR1:VOLT {ch1_vpp}")  # NOT FUNC:ARB:PTPeak -- confirmed on
                                              # real hardware that it doesn't
                                              # actually update the channel's
                                              # real amplitude register here
        # CH1's sequence always has a onceWaitTrig anchor now (unified
        # anchor_free_reps applies to both channels regardless of
        # ch2_hold_constant -- see setup_awg_sequences()'s docstring), so
        # it always needs a real trigger source configured.
        awg.write("TRIG1:SOUR EXT")
        awg.write("TRIG1:SLOP POS")
        awg.write("TRIG1:LEV 1.5")
    awg.write("OUTPUT1 ON")

    # CH2: MW gate -> ZYSWA switch control.
    awg.write("OUTP2:LOAD INF")
    awg.write(f"SOUR2:FUNC:ARB:SRAT {sample_rate_hz}")
    awg.write(f'SOUR2:FUNC:ARB "{sequence_name_ch2}"')
    awg.write("SOUR2:FUNC ARB")
    awg.write(f"SOUR2:VOLT {ch2_vpp}")
    awg.write(f"SOUR2:VOLT:OFFS {ch2_offset_v}")
    awg.write("TRIG2:SOUR EXT")
    awg.write("TRIG2:SLOP POS")
    awg.write("TRIG2:LEV 1.5")
    awg.write("OUTPUT2 ON")

    awg.write("OUTPut:SYNC:SOURce CH2")

    # Next candidate for what *RST does that ABOR doesn't (the IMM->EXT
    # trigger-source toggle tried before this was falsified on real
    # hardware -- rabi_new20_test still spiked -- see notes.md).
    # FUNCtion:ARBitrary:SYNChronize resets/aligns the start phase of the
    # currently-selected arb on a channel (normally used after changing
    # sample rate/frequency to re-align coupled channels) -- a real,
    # documented resync primitive, unlike the trigger-source guess. Placed
    # here, after both channels' sequences are freshly selected and armed,
    # to force each point's freshly-loaded sequence to start from a known
    # phase before it ever sees a trigger edge. Not yet confirmed on real
    # hardware.
    awg.write("SOUR1:FUNC:ARB:SYNC")
    awg.write("SOUR2:FUNC:ARB:SYNC")


def _configure_external_trigger(sdg, ref_period_s, margin=100):
    """
    Configures the SDG1062X (sdg1062x.py) as a continuous square wave fast
    enough to keep the AWG's "onceWaitTrig" anchor segments (see
    setup_awg_sequences()) retriggered every loop -- confirmed on real
    hardware that a trigger slower than the sequence's own full cycle
    period (ref_period_s = 2 * n_reps * rep_us) leaves both channels
    frozen at their anchor, holding low, for most of the gap between
    edges instead of running continuously (see notes.md).

    margin is a safety multiplier over the bare minimum rate
    (1 / ref_period_s): this trigger free-runs asynchronously to the
    sequence's own wrap-around moment, so each lap picks up a random extra
    "dead time" of up to one trigger period waiting for the next edge,
    which always lengthens the block's "off"/low portion. A higher margin
    shrinks that residual duty-cycle skew (confirmed on real hardware:
    50x margin left a visible few-percent skew, 100x+ measured 49.18%
    instead of an exact 50%) at the cost of a faster (but still easily
    achievable) SDG1062X frequency.
    """
    freq_hz = margin / ref_period_s
    sdg.write("C1:BSWV WVTP,SQUARE")
    sdg.write(f"C1:BSWV FRQ,{freq_hz}")
    sdg.write("C1:BSWV AMP,5")     # 5 Vpp
    sdg.write("C1:BSWV OFST,2.5")  # 0-5V swing, comfortably above the
                                     # 1.5V TRIG:LEV threshold set in
                                     # setup_awg_sequences()
    sdg.write("C1:OUTP ON")
    return freq_hz


def setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg, input_coupling,
                   auto_sensitivity=False):
    """Same convention as cw_odmr_lock_in.py's setup_lock_in(), minus the
    chop_freq_hz reference-frequency argument -- there's no fixed chop
    frequency here, the reference comes from CH1's block-level marker at
    whatever period the current mw_us/n_reps happen to give."""
    lia.set_reference_external(slope="ttl_rising")
    lia.set_phase_deg(phase_deg)
    lia.set_input_config("a")  # voltage input -- PMT signal already amplified
    lia.set_input_coupling(ac=(input_coupling == "ac"))
    lia.set_time_constant_s(time_constant_s)
    if not auto_sensitivity:
        lia.set_sensitivity_v(sensitivity_v)
    lia.set_filter_slope_db_oct(24)
    # Nothing here ever calls lia.reset() (*RST) at connect time, so any
    # OEXP (offset/expand) left over from a previous session -- another
    # script, a manual front-panel adjustment -- silently persists and
    # shrinks the SR830's usable output range, making a nuisance "output
    # overload" LIAS trip far easier from an otherwise harmless transient
    # (see notes.md -- caught a real output_overload=True, input_overload=
    # False spike this way, with no real input signal condition to explain
    # it). Force a known-clean 0%/1x state on X and Y explicitly rather
    # than assuming factory defaults, and log whatever was there before in
    # case it turns out to matter for that investigation.
    for channel, name in [(1, "X"), (2, "Y")]:
        offset_percent, expand = lia.get_offset_expand(channel)
        if offset_percent != 0.0 or expand != 1:
            print(f"[rabi] setup_lock_in: {name} had offset={offset_percent}%, "
                  f"expand={expand}x from a previous session -- resetting to 0%/1x")
        lia.set_offset_expand(channel, 0.0, expand=1)


def _step_sensitivity_coarser(lia):
    """Same as cw_odmr_lock_in.py's _step_sensitivity_coarser() -- step to
    the next LESS sensitive range, one step at a time, to back out of a
    real-time overload without overshooting via auto_gain()."""
    current_v = lia.get_sensitivity_v()
    idx = SR830.SENSITIVITY_V.index(current_v)
    new_idx = min(idx + 1, len(SR830.SENSITIVITY_V) - 1)
    new_v = SR830.SENSITIVITY_V[new_idx]
    if new_idx != idx:
        lia.set_sensitivity_v(new_v)
    return new_v


def _step_sensitivity_finer(lia):
    """Mirror of _step_sensitivity_coarser() above, for the opposite
    problem: nothing exists in this codebase (rabi.py or
    cw_odmr_lock_in.py) that ever steps sensitivity back DOWN (more
    sensitive) once something has pushed it up -- auto_gain() only runs
    once at the start of a sweep, and _step_sensitivity_coarser() only
    ever coarsens. Once any early transient/overload drives sensitivity to
    a coarse range, it stays pinned there for the rest of the sweep even
    if the real signal has since settled to a small fraction of that
    range's full scale, which under-resolves later readings (closer to
    the SR830's own noise/quantization floor at that range) instead of
    reflecting a genuinely large signal. Steps one range at a time, same
    as the coarsening direction."""
    current_v = lia.get_sensitivity_v()
    idx = SR830.SENSITIVITY_V.index(current_v)
    new_idx = max(idx - 1, 0)
    new_v = SR830.SENSITIVITY_V[new_idx]
    if new_idx != idx:
        lia.set_sensitivity_v(new_v)
    return new_v


def _wait_settle_discarding_transient_overload(lia, settle_s, transient_fraction=0.8):
    """
    Sleep settle_s, but discard the SR830's overload flag partway through
    instead of leaving it to accumulate for the entire wait. LIAS? bits
    latch until read (see read_overload_status()'s docstring), so a plain
    time.sleep(settle_s) followed by a single overload check afterward
    catches ANY excursion during the whole window -- including the
    expected transient right at the start, as the demodulated output
    swings from the PREVIOUS point's steady value toward this one before
    the filter actually converges. That's normal step-response behavior,
    not a real overload of the final, settled reading, but without this it
    reads back as one -- confirmed on real hardware happening on
    essentially every point, since every point has that same transition.

    Waits transient_fraction of settle_s (letting most of that expected
    swing decay), discards whatever overload flag it latched, then waits
    the remainder before returning -- a genuine overload that's still
    present right up to the actual read (not just an artifact of the
    transition) still latches during that remaining window and is still
    caught by the real check that follows this call.
    """
    transient_s = settle_s * transient_fraction
    final_s = settle_s - transient_s
    time.sleep(transient_s)
    lia.read_overload_status()  # discard -- see docstring above
    time.sleep(final_s)


def cmd_run(file_name, **kw):
    """
    Sweep the MW pulse length (tau_mw) at a fixed frequency, recording the
    SR830's X/Y at each point against the AWG's block-chopped reference.

    Recognized key=value overrides (all optional): freq_hz, drive_power_dbm,
    threshold_dbm, mw_start_us, mw_stop_us, mw_step_us, n_reps, laser_us,
    pre_us, post_us, time_constant_s, settle_periods, settle_time_constants,
    sensitivity_v, auto_sensitivity, phase_deg, input_coupling,
    auto_rescale_on_overload, max_rescale_attempts, auto_rescale_on_underload,
    underload_margin, underload_persistence, fixed_sensitivity,
    psu_voltage_v,
    psu_current_limit_a, coil_current_a, coil_voltage_margin,
    interlock_check_interval, interlock_hold_periods, interlock_during_sweep,
    resequence_interval, extra_settle_s, ch1_vpp, ch2_vpp,
    ch2_offset_v, trigger_margin, anchor_free_reps, fixed_external_trigger,
    reflected_power_scan, res_span_hz, coarse_step_hz, fine_span_hz,
    fine_step_hz, res_power_dbm, res_cal_dir, fine_sweep -- see the
    parameter-parsing block below for defaults and notes.md for the
    reasoning behind non-obvious ones.

    If reflected_power_scan=true (default), runs a coarse-then-fine
    reflected-power sweep (HP8673H.resonance_sweep()) centered on freq_hz
    (+/- res_span_hz/2) BEFORE the tau_mw sweep starts, purely as a saved
    diagnostic -- unlike cw_odmr_lock_in.py's use_resonance_sweep, this does
    NOT change freq_hz; it's just a pre-flight check/record of the
    reflected-power profile around the fixed operating frequency, run at a
    low, safe res_power_dbm. Saves {file_name}_resonance_coarse.csv and
    _resonance_fine.csv in the run's data directory. Once the generator is
    then set to the real operating point (freq_hz, drive_power_dbm) and RF
    is turned on for the tau_mw sweep, also prints a single reflected-power
    reading AT that exact operating point -- the coarse/fine sweep above
    only ran at the low res_power_dbm, so this is the first real check at
    the actual power level about to be used.
    """
    freq_hz = float(kw.get("freq_hz", 2.843e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    threshold_dbm = float(kw.get("threshold_dbm", -10.0))
    mw_start_us = float(kw.get("mw_start_us", 0.02))
    mw_stop_us = float(kw.get("mw_stop_us", 5.0))
    mw_step_us = float(kw.get("mw_step_us", 0.02))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    # Raised from 5.0: setup_lock_in() uses a 24 dB/oct (4-pole) filter,
    # and a cascaded n-pole RC filter's step response only reaches
    # ~73.5% settled after 5 time constants (vs. ~99.3% for a single
    # pole) -- 9 TC gets a 4-pole filter to ~97.9% settled instead.
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    auto_rescale_on_overload = str(kw.get("auto_rescale_on_overload", "true")).lower() == "true"
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 3))
    # See _step_sensitivity_finer()'s docstring -- auto_rescale_on_overload
    # only ever coarsens; without this, sensitivity stays pinned wherever
    # an early transient/overload left it for the rest of the sweep, even
    # once the real signal has settled to a small fraction of that range.
    # Checked AFTER each point's reading (not urgent the way overload is),
    # so it takes effect for the NEXT point rather than paying an extra
    # settle_s wait to re-read immediately.
    auto_rescale_on_underload = str(kw.get("auto_rescale_on_underload", "true")).lower() == "true"
    # Only step to the next finer range if R would still use less than
    # this fraction of THAT range's full scale -- keeps a safety margin
    # against immediately overloading again after stepping down, and
    # avoids oscillating back and forth between two adjacent ranges on
    # noise alone.
    underload_margin = float(kw.get("underload_margin", 0.5))
    # Real-hardware testing found underload_margin alone wasn't enough --
    # a real bounce pattern: OVERLOAD coarsens sensitivity, then the very
    # NEXT point's reading (at that new, coarser range) satisfies the
    # underload condition and immediately reverts back to the finer range
    # that just overloaded, which then overloads again next point, etc.
    # This shows up as strong NEGATIVE lag-1 autocorrelation (point-to-
    # point alternation) in the collected data -- confirmed on
    # rabi_new2/3/4_fix_trigger (-0.19 to -0.60), consistent with a
    # near-Nyquist FFT peak that's real, not finite-sample noise (unlike
    # rabi_new/rabi_new1/the noise floor, which showed near-zero
    # autocorrelation despite a similar-looking near-Nyquist peak -- see
    # notes.md). Requiring the underload condition to hold for
    # underload_persistence CONSECUTIVE points before actually stepping
    # finer acts as a low-pass filter on the decision: a one-off reading
    # right after an overload-forced coarsening won't immediately trigger
    # a revert, since it needs the same condition to also hold on
    # subsequent points, which won't happen if the coarser range was
    # genuinely needed.
    underload_persistence = int(kw.get("underload_persistence", 3))
    # Convenience override: skip AGAN and BOTH overload/underload
    # rescaling entirely, using sensitivity_v as-is for the whole sweep --
    # for isolating whether auto-rescaling itself (even with the
    # underload_persistence fix above) is adding noise/artifacts, by
    # comparing against a run with sensitivity held completely fixed
    # throughout. Overrides auto_sensitivity/auto_rescale_on_overload/
    # auto_rescale_on_underload regardless of what they're individually
    # set to -- MUST come after all three are parsed above, or their own
    # parsing lines would silently reset this override (a real bug found
    # on real hardware: fixed_sensitivity=true still rescaled, because
    # this block used to run BEFORE auto_rescale_on_underload's own
    # kw.get() line, which unconditionally overwrote it back to True
    # right after). Pick sensitivity_v manually (e.g. from a previous
    # auto_sensitivity run's logged range) since AGAN won't pick one for
    # you here.
    fixed_sensitivity = str(kw.get("fixed_sensitivity", "false")).lower() == "true"
    if fixed_sensitivity:
        auto_sensitivity = False
        auto_rescale_on_overload = False
        auto_rescale_on_underload = False
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))
    interlock_check_interval = int(kw.get("interlock_check_interval", 5))
    interlock_hold_periods = float(kw.get("interlock_hold_periods", 3.0))
    # The PERIODIC per-point check below (not the pre-flight coarse/fine
    # scan + operating-point check before the sweep starts, which always
    # runs regardless of this flag) turned out to be a real source of the
    # background-artifact spikes: its ~1s GPIB round trip to the HP8673H
    # is comparable to anchor_period_s, so it races with the AWG's own
    # anchor-wrap timing (see notes.md). ABOR-ing after every check
    # (added to remove that race) did NOT fully eliminate the spikes on
    # real hardware, so this lets the periodic check be skipped entirely
    # during the sweep as a further isolation step -- MW is still being
    # driven at whatever power was validated at the pre-flight check, just
    # without periodic re-verification while sweeping tau_mw. Use with
    # care: no periodic reflected-power protection while this is false.
    interlock_during_sweep = str(kw.get("interlock_during_sweep", "true")).lower() == "true"
    # Overrides the module-level RESEQUENCE_INTERVAL default for this run.
    # Bucketing rabi_new14/15/16/17 by distance from the nearest awg.reset()
    # showed points right after a reset are clean (0% hitting the ~1.5e-4
    # rail) while points 1-3 after it get progressively worse (15%/25%/26%)
    # -- ABOR alone (used every other point in setup_awg_sequences()) isn't
    # fully flushing whatever *RST does. Set to 1 to reset every point as a
    # direct test of that theory (slow -- full *RST every point -- but a
    # clean pass/fail check before looking for a cheaper fix). See
    # notes.md.
    resequence_interval = int(kw.get("resequence_interval", RESEQUENCE_INTERVAL))
    # Extra fixed wait added right after setup_awg_sequences() (the ABOR/
    # clear/reupload/FUNC:ARB:SYNC/re-arm), before settle_s's own countdown
    # starts. Validated in debug_repeat_one_point.py's with-reupload mode:
    # 1.0s here eliminated the rare, large (~1.5e-4 V) output_overload rail
    # spikes across several iterations at a normal every-point-reupload
    # cadence -- the everyday few-uV bump was already shown to settle out
    # within one normal settle_s window (no-reupload mode), but the bigger
    # rail spikes evidently needed more margin specifically right after
    # the reconfigure event itself. 0 by default (no behavior change) --
    # see notes.md for the full investigation.
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    # Lowered from 100 -- that large a margin was needed when every
    # single off+on cycle (a few ms) went through the onceWaitTrig
    # anchor, since the trigger's own asynchronous dead time (up to one
    # trigger period) was a meaningful fraction of that short cycle,
    # visibly skewing its duty cycle. With anchor_free_reps now spacing
    # anchor-wraps out to a much longer stretch (see below), that same
    # dead time is a tiny fraction of the much longer period -- a modest
    # margin is "just enough" without needlessly fast triggering.
    trigger_margin = float(kw.get("trigger_margin", 3.0))
    # Since the combined-arb rewrite, anchor_free_reps is a free repeat
    # count (one table entry regardless of size), so it's no longer bound
    # by the old ~250-listing sequence-table ceiling -- it's tuned instead
    # against covering the FULL dwell before a point's read, which is now
    # extra_settle_s + settle_s (not just settle_s) since extra_settle_s
    # adds a wait BEFORE settle_s's own countdown starts. rabi_new11_fix_
    # anchor (n_reps=250, tau_mw 0.02-3.02 us) found the old default of
    # 200 gave an anchor period (0.40-0.70 s) SHORTER than settle_s alone
    # (0.9 s, dominated by settle_time_constants*time_constant_s),
    # guaranteeing at least one anchor-wrap glitch inside every point's
    # window. 500 covered settle_s alone comfortably, but with extra_
    # settle_s=1.0 (validated in debug_repeat_one_point.py -- see
    # notes.md), the total dwell is up to ~1.9 s, which at the shortest
    # tau_mw needs anchor_free_reps > ~945 to still cover it -- 1000
    # confirmed fine on real hardware up to that value in tests/
    # rabi_combined_arb_marker_test.ipynb. If you raise extra_settle_s
    # further, raise this too so anchor_period_s (= ref_period_s *
    # anchor_free_reps) still comfortably exceeds extra_settle_s +
    # settle_s at your sweep's SHORTEST tau_mw -- otherwise the sequence
    # can wrap and need an uncontrolled retrigger mid-window again, the
    # exact problem this parameter exists to avoid. See notes.md's
    # "spurious off-resonance/no-MW-near-sample signal" entry.
    anchor_free_reps = int(kw.get("anchor_free_reps", 1000))
    # Fixes the SDG1062X trigger at a single rate for the WHOLE sweep
    # (derived from mw_start_us, the sweep's shortest rep and therefore
    # its largest bare-minimum trigger requirement -- comfortably fast
    # enough for every other, longer rep too), instead of recomputing it
    # every point from the current mw_us. Set false to restore the old
    # per-point recomputation.
    fixed_external_trigger = str(kw.get("fixed_external_trigger", "true")).lower() == "true"
    reflected_power_scan = str(kw.get("reflected_power_scan", "true")).lower() == "true"
    res_span_hz = float(kw.get("res_span_hz", 100e6))
    coarse_step_hz = float(kw.get("coarse_step_hz", 2e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    res_cal_dir = kw.get("res_cal_dir", None)
    # Skips resonance_sweep()'s fine stage (and its Q/FWHM estimate) --
    # only the coarse sweep runs. Independent of reflected_power_scan
    # (which gates the WHOLE pre-flight sweep, coarse+fine, on or off);
    # this just trims the fine half when you only want a quick coarse
    # sanity check around freq_hz without the fine sweep's extra time.
    fine_sweep = str(kw.get("fine_sweep", "true")).lower() == "true"

    mw_values_us = np.arange(mw_start_us, mw_stop_us + mw_step_us / 2, mw_step_us)

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_rabi.txt"

    with _tee_stdout(log_path):
        print(f"[rabi] tau_mw sweep: {mw_start_us}-{mw_stop_us} us, "
              f"step {mw_step_us} us ({len(mw_values_us)} points), "
              f"n_reps={n_reps}, fixed frequency {freq_hz/1e9:.5f} GHz, "
              f"{drive_power_dbm} dBm")

        print("[rabi] step 1/3: configuring AWG + SDG1062X (external "
              "trigger) + SR830 lock-in")
        # debug=False here just quiets the per-command "cmd => response"
        # print spam -- KS33600A.write() checks SYST:ERR? and raises
        # unconditionally regardless of this flag, so turning it off
        # doesn't reduce error detection at all. NOT the same for SR830
        # below: its write() only queries ERRS? when debug=True, so THAT
        # one is left on -- turning it off would silently disable the
        # lock-in's own error checking, not just its printouts.
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=False)
        sdg = SDG1062X(SDG_RESOURCE, debug=False)
        lia = SR830(SR830_RESOURCE, debug=True)
        setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg, input_coupling,
                      auto_sensitivity=auto_sensitivity)
        print(f"[rabi] step 1/3 done: time constant {time_constant_s*1e3:.1f} ms, "
              f"phase {phase_deg} deg (NOT auto-calibrated -- see module docstring)")

        print("[rabi] step 2/3: connecting to HP8673H + E4403B (interlock) "
              "+ SPD1168X (amplifier supply) + SPD1305X (coil supply)")
        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        ilock_sa = None
        try:
            amp_psu.turn_on(psu_voltage_v, psu_current_limit_a)
            coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
            coil_psu.turn_on(coil_voltage_v, coil_current_a)

            ilock_sa = HP8673H.try_connect_analyzer(SA_RESOURCE)
            if ilock_sa is None:
                gen.trip_interlock("spectrum analyzer not reachable at startup")
                return

            if reflected_power_scan:
                # Hold CH2 static on the sample path (RF2), not chopping --
                # setup_awg_sequences() hasn't run yet at this point (it's
                # inside the sweep loop below), so without this CH2 is in
                # whatever leftover/default state the AWG connection left
                # it in, not deliberately routed anywhere. Same fix as
                # cw_odmr_lock_in.py's resonance sweep needed (see
                # notes.md's ZYSWA section) -- without it, the analyzer
                # would see reflected power from an uncontrolled/wrong
                # switch state instead of the real sample path.
                set_switch_static(awg, route_to_sample=True)
                print(f"[rabi] pre-flight: reflected-power sweep around "
                      f"{freq_hz/1e9:.5f} GHz (+/- {res_span_hz/2/1e6:.1f} MHz), "
                      f"{res_power_dbm} dBm")
                gen.resonance_sweep(
                    ilock_sa, freq_hz - res_span_hz / 2, freq_hz + res_span_hz / 2,
                    coarse_step_hz, fine_span_hz, fine_step_hz, res_power_dbm,
                    output_prefix=f"{run_path}/{file_name}_resonance",
                    cal_dir=res_cal_dir,
                    # Center the fine sweep on the fixed freq_hz we're
                    # actually driving, not wherever the coarse sweep's dip
                    # happened to land -- this is a pre-flight check of the
                    # frequency in use, not a resonance-finding step (see
                    # cmd_run()'s docstring: reflected_power_scan does NOT
                    # change freq_hz).
                    fine_center_hz=freq_hz,
                    run_fine_sweep=fine_sweep,
                )
                print(f"[rabi] pre-flight done: saved "
                      f"{run_path}/{file_name}_resonance_coarse.csv"
                      f"{', _resonance_fine.csv' if fine_sweep else ' (fine_sweep=false -- fine.csv not written)'}")

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            if reflected_power_scan:
                # The coarse/fine sweep above ran at the low, safe
                # res_power_dbm -- this is a separate readout at the ACTUAL
                # operating point (freq_hz, drive_power_dbm) now that the
                # generator is running there for real, right before the
                # tau_mw sweep starts. CH2 is still statically routed to
                # the sample path (set_switch_static() above, not chopping
                # yet -- setup_awg_sequences() hasn't run), and the signal
                # is CW at this point, so a single-sweep read is enough
                # (no need for read_max_hold_reflected_power_dbm()'s
                # pulsed-signal handling, which only matters once the
                # per-point loop below starts gating).
                reflected_at_freq_dbm = HP8673H.read_reflected_power_dbm(ilock_sa, freq_hz)
                if reflected_at_freq_dbm is not None:
                    print(f"[rabi] pre-flight: reflected power at the operating "
                          f"point ({freq_hz/1e9:.5f} GHz, {drive_power_dbm} dBm) = "
                          f"{reflected_at_freq_dbm:.2f} dBm")
                else:
                    print("[rabi] pre-flight: WARNING -- failed to read reflected "
                          "power at the operating point")

                # This was print-only until now -- unlike the periodic
                # in-sweep interlock check below, it never actually
                # enforced threshold_dbm, so a dangerously high reflected
                # power at the REAL operating point (not just the low,
                # safe res_power_dbm used for the coarse/fine sweep above)
                # could sail straight through with nothing but a log line.
                # Enforce it here too, before the tau_mw sweep even
                # starts.
                if reflected_at_freq_dbm is None or reflected_at_freq_dbm > threshold_dbm:
                    reason = (
                        "spectrum analyzer unreachable"
                        if reflected_at_freq_dbm is None else
                        f"reflected power {reflected_at_freq_dbm:.2f} dBm exceeds "
                        f"threshold {threshold_dbm} dBm"
                    )
                    gen.trip_interlock(f"{reason} at the pre-flight operating-point "
                                        f"check ({freq_hz/1e9:.5f} GHz, "
                                        f"{drive_power_dbm} dBm)")
                    return

            if fixed_external_trigger:
                # Worst case (highest bare-minimum trigger requirement) is
                # the sweep's SHORTEST rep, at mw_start_us -- fast enough
                # here means comfortably fast enough for every longer rep
                # later in the sweep too.
                fixed_rep_us = laser_us + pre_us + mw_start_us + post_us
                fixed_anchor_period_s = 2 * n_reps * fixed_rep_us * 1e-6 * anchor_free_reps
                fixed_trigger_freq_hz = _configure_external_trigger(
                    sdg, fixed_anchor_period_s, margin=trigger_margin)
                print(f"[rabi] fixed_external_trigger=true: SDG1062X trigger fixed "
                      f"at {fixed_trigger_freq_hz/1e3:.3f} kHz for the whole sweep "
                      f"(derived from mw_start_us={mw_start_us} us, "
                      f"anchor_free_reps={anchor_free_reps})")

            print(f"[rabi] step 3/3: sweeping tau_mw, threshold {threshold_dbm} dBm")

            x_values = np.full(len(mw_values_us), np.nan)
            y_values = np.full(len(mw_values_us), np.nan)
            reflected_dbm_arr = np.full(len(mw_values_us), np.nan)
            # Diagnostic for the "spikes persist even with the SR830 input
            # terminated" finding (see notes.md) -- rules out the signal
            # path (PMT, cable, ground loops) entirely, pointing at the
            # reference path or something internal to the SR830 instead.
            # Logging FREQ? (the SR830's own measured reference frequency)
            # alongside X/Y lets us check directly whether a spike
            # coincides with an anomalous reference reading, rather than
            # needing a scope on the Sync line.
            ref_freq_hz_arr = np.full(len(mw_values_us), np.nan)
            reference_unlock_arr = np.zeros(len(mw_values_us), dtype=bool)
            n_completed = 0
            tripped = False
            underload_streak = 0

            try:
                for i, mw_us in enumerate(mw_values_us):
                    if i > 0 and i % resequence_interval == 0:
                        print(f"[rabi] point {i + 1}/{len(mw_values_us)}: resetting AWG "
                              f"to clear its sequence table (every "
                              f"{resequence_interval} points)")
                        awg.reset()
                        awg.write("SOUR1:DATA:VOL:CLE")
                        awg.write("SOUR2:DATA:VOL:CLE")

                    rep_us = laser_us + pre_us + mw_us + post_us
                    ref_period_s = 2 * n_reps * rep_us * 1e-6
                    # The reference period can be much shorter than the
                    # lock-in's own RC filter time constant (e.g. at small
                    # n_reps or short tau_mw) -- settle_periods reference
                    # periods alone isn't enough in that case, since the
                    # filter itself hasn't settled yet. Wait whichever is
                    # longer.
                    settle_s = max(settle_periods * ref_period_s,
                                    settle_time_constants * time_constant_s)

                    # With anchor_free_reps > 1, both channels only wrap
                    # back through their anchor once every anchor_free_reps
                    # off+on cycles (see setup_awg_sequences()'s docstring)
                    # -- so the external trigger only needs to stay at/
                    # above THAT longer rate, not the single-cycle rate.
                    # Skipped when fixed_external_trigger is set (default)
                    # -- see the one-time configuration before this loop.
                    if not fixed_external_trigger:
                        anchor_period_s = ref_period_s * anchor_free_reps
                        _configure_external_trigger(sdg, anchor_period_s, margin=trigger_margin)

                    setup_awg_sequences(
                        awg, mw_us, n_reps, laser_us, pre_us, post_us,
                        sequence_name_ch1=f"rabi_ch1_{i}",
                        sequence_name_ch2=f"rabi_ch2_{i}",
                        ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                        anchor_free_reps=anchor_free_reps,
                    )
                    if extra_settle_s > 0:
                        time.sleep(extra_settle_s)

                    # Clears anything latched from BEFORE this point's own
                    # settle window even starts (e.g. left over from the
                    # previous point's tail end, or from reconfiguring the
                    # AWG sequence/external trigger just above) -- paired
                    # with _wait_settle_discarding_transient_overload()
                    # below, which handles the transient that occurs
                    # WITHIN the settle window itself. See that function's
                    # docstring and read_overload_status()'s docstring for
                    # why both discards are needed.
                    lia.read_overload_status()

                    if i == 0 and auto_sensitivity:
                        # A real signal is present now that the AWG is
                        # running the first point -- let AGAN pick a range,
                        # then wait the same settle margin used between
                        # points before trusting/logging it.
                        time.sleep(settle_s)
                        lia.auto_gain()
                        time.sleep(settle_s)
                        actual_sensitivity_v = lia.get_sensitivity_v()
                        print(f"[rabi] auto_sensitivity: AGAN selected "
                              f"{actual_sensitivity_v:.3e} V full scale")
                        sensitivity_v = actual_sensitivity_v

                    if interlock_during_sweep and i % interlock_check_interval == 0:
                        hold_s = interlock_hold_periods * ref_period_s
                        anchor_period_s_for_check = ref_period_s * anchor_free_reps
                        check_start_s = time.monotonic()
                        power_dbm = HP8673H.read_max_hold_reflected_power_dbm(
                            ilock_sa, freq_hz, hold_s)
                        check_elapsed_s = time.monotonic() - check_start_s
                        reflected_dbm_arr[i] = power_dbm if power_dbm is not None else np.nan

                        # Diagnostic for the "spikes near interlock checks"
                        # investigation (see notes.md): if this GPIB round
                        # trip takes longer than anchor_period_s, the AWG's
                        # current anchor_free_reps-repeat run has already
                        # finished and wrapped back to its onceWaitTrig
                        # anchor by the time settle_s starts below -- a
                        # fresh, uncontrolled trigger-wait dead time then
                        # lands inside the settle window instead of settle_s
                        # overlapping an already-steady, mid-run signal.
                        print(f"[rabi] interlock check timing (point {i + 1}/"
                              f"{len(mw_values_us)}): took {check_elapsed_s:.3f} s "
                              f"(nominal hold_s={hold_s:.3f} s) vs. this point's "
                              f"anchor_period_s={anchor_period_s_for_check:.3f} s -- "
                              f"{'EXCEEDS anchor period, AWG likely re-idled' if check_elapsed_s > anchor_period_s_for_check else 'within anchor period'}")

                        if power_dbm is not None:
                            print(f"[rabi] interlock check (point {i + 1}/"
                                  f"{len(mw_values_us)}, tau_mw={mw_us:.3f} us): "
                                  f"reflected power {power_dbm:.2f} dBm "
                                  f"(threshold {threshold_dbm} dBm) -- "
                                  f"{'OK' if power_dbm <= threshold_dbm else 'OVER THRESHOLD'}")

                        if power_dbm is None or power_dbm > threshold_dbm:
                            reason = (
                                "spectrum analyzer unreachable"
                                if power_dbm is None else
                                f"reflected power {power_dbm:.2f} dBm exceeds "
                                f"threshold {threshold_dbm} dBm"
                            )
                            gen.trip_interlock(f"{reason} at tau_mw={mw_us:.3f} us "
                                                f"(point {i + 1}/{len(mw_values_us)})")
                            tripped = True
                            break

                        # The GPIB round trip above takes a real, variable
                        # amount of time (confirmed ~1s on real hardware --
                        # comparable to anchor_period_s itself), so whether
                        # this point's sequence is still mid-run or has
                        # already wrapped back to its onceWaitTrig anchor by
                        # now is a race, not a known state -- sometimes
                        # settle_s below overlaps already-steady signal,
                        # sometimes it needs a fresh, uncontrolled retrigger
                        # partway through. ABOR forces the latter every time
                        # instead of leaving it to chance: the sequence data
                        # is untouched (no reupload needed), just stopped, so
                        # the very next external trigger edge restarts it
                        # from its anchor -- a single, bounded (<= one
                        # trigger period) dead time right at the start of
                        # settle_s, which the transient-discard wait below is
                        # already designed to absorb. Also apply extra_
                        # settle_s here, same as after setup_awg_sequences()
                        # -- this ABOR is a SEPARATE reconfigure event from
                        # that one, so it needs its own margin too (found on
                        # real hardware: with a normal-cadence interlock
                        # check, this was the ONLY point in an otherwise-
                        # clean sweep that still hit the output_overload
                        # rail, since this path went straight into settle_s
                        # with no extra_settle_s buffer at all).
                        awg.write("ABOR")
                        if extra_settle_s > 0:
                            time.sleep(extra_settle_s)

                    _wait_settle_discarding_transient_overload(lia, settle_s)

                    x, y = lia.read_xy()
                    ref_freq_hz = lia.get_frequency_hz()
                    ref_freq_hz_arr[i] = ref_freq_hz
                    expected_ref_freq_hz = 1.0 / ref_period_s
                    # Read unconditionally (not just when auto_rescale_on_
                    # overload is set -- fixed_sensitivity=true disables
                    # that, which would otherwise skip this entirely) so
                    # reference_unlock is always checked, per the
                    # "background artifact isn't on the signal path"
                    # investigation in notes.md. Reused below as the first
                    # rescale-loop iteration's status instead of querying
                    # LIAS? again immediately after (it latches until read).
                    initial_status = lia.read_overload_status()
                    reference_unlock_arr[i] = initial_status["reference_unlock"]
                    print(f"[rabi] point {i + 1}/{len(mw_values_us)}: SR830 reference "
                          f"reads {ref_freq_hz:.6f} Hz (expected {expected_ref_freq_hz:.6f} Hz), "
                          f"reference_unlock={initial_status['reference_unlock']}, "
                          f"input_overload={initial_status['input']}, "
                          f"filter_overload={initial_status['filter']}, "
                          f"output_overload={initial_status['output']}")

                    overloaded_this_point = False
                    if auto_rescale_on_overload:
                        overload_status = initial_status
                        for attempt in range(max_rescale_attempts):
                            if attempt > 0:
                                overload_status = lia.read_overload_status()
                            if not overload_status["any"]:
                                break
                            overloaded_this_point = True
                            old_v = lia.get_sensitivity_v()
                            new_v = _step_sensitivity_coarser(lia)
                            # Changing SENS itself can transiently overload
                            # the input stage as the range switches (a
                            # separate effect from the signal genuinely
                            # being too big for the new range) -- clear
                            # that here so the NEXT loop iteration's check
                            # reflects the new range's real, settled state,
                            # not this switching transient.
                            lia.read_overload_status()
                            print(f"[rabi] OVERLOAD at tau_mw={mw_us:.3f} us "
                                  f"(point {i + 1}/{len(mw_values_us)}): rescaling "
                                  f"sensitivity {old_v:.3e} V -> {new_v:.3e} V full "
                                  f"scale, re-reading (attempt {attempt + 1}/"
                                  f"{max_rescale_attempts})")
                            sensitivity_v = new_v
                            _wait_settle_discarding_transient_overload(lia, settle_s)
                            x, y = lia.read_xy()
                        else:
                            print(f"[rabi] tau_mw={mw_us:.3f} us: still overloading "
                                  f"after {max_rescale_attempts} rescale attempts -- "
                                  f"saving as-is")

                    if overloaded_this_point:
                        # This point just got coarsened out of an overload
                        # -- don't let it count toward underload_persistence
                        # at all (even if it happens to look "underloaded"
                        # relative to the NEW coarser range); that's
                        # exactly the single-point-triggered bounce this
                        # persistence check exists to prevent.
                        underload_streak = 0
                    elif auto_rescale_on_underload:
                        idx = SR830.SENSITIVITY_V.index(sensitivity_v)
                        if idx > 0:
                            r = (x ** 2 + y ** 2) ** 0.5
                            next_v = SR830.SENSITIVITY_V[idx - 1]
                            if r < underload_margin * next_v:
                                underload_streak += 1
                                if underload_streak >= underload_persistence:
                                    old_v = sensitivity_v
                                    new_v = _step_sensitivity_finer(lia)
                                    print(f"[rabi] tau_mw={mw_us:.3f} us (point {i + 1}/"
                                          f"{len(mw_values_us)}): R={r:.3e} V well under "
                                          f"the current {old_v:.3e} V full scale for "
                                          f"{underload_streak} consecutive points -- "
                                          f"stepping sensitivity down to {new_v:.3e} V "
                                          f"full scale for the next point")
                                    sensitivity_v = new_v
                                    underload_streak = 0
                            else:
                                underload_streak = 0
                        else:
                            underload_streak = 0
                    else:
                        underload_streak = 0

                    x_values[i] = x
                    y_values[i] = y
                    n_completed = i + 1
                    print(f"[rabi] point {n_completed}/{len(mw_values_us)}: "
                          f"tau_mw={mw_us:.3f} us, X={x:.6e} V, Y={y:.6e} V")
            except KeyboardInterrupt:
                print("[rabi] stopped by user (Ctrl+C)")

            mw_values_us_trimmed = mw_values_us[:n_completed]
            x_values = x_values[:n_completed]
            y_values = y_values[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]
            ref_freq_hz_arr = ref_freq_hz_arr[:n_completed]
            reference_unlock_arr = reference_unlock_arr[:n_completed]

            if n_completed == 0:
                print("[rabi] step 3/3 FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_rabi_mw_us.npy", mw_values_us_trimmed)
                np.save(f"{run_path}/{file_name}_rabi_ref_freq_hz.npy", ref_freq_hz_arr)
                np.save(f"{run_path}/{file_name}_rabi_reference_unlock.npy", reference_unlock_arr)
                np.save(f"{run_path}/{file_name}_rabi_x.npy", x_values)
                np.save(f"{run_path}/{file_name}_rabi_y.npy", y_values)
                np.save(f"{run_path}/{file_name}_rabi_reflected_dbm.npy", reflected_dbm_arr)
                with open(f"{run_path}/{file_name}_rabi_metadata.txt", "w") as fh:
                    fh.write(f"freq_hz={freq_hz}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                    fh.write(f"n_reps={n_reps}\n")
                    fh.write(f"laser_us={laser_us}\n")
                    fh.write(f"pre_us={pre_us}\n")
                    fh.write(f"post_us={post_us}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"settle_periods={settle_periods}\n")
                    fh.write(f"settle_time_constants={settle_time_constants}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                print(f"[rabi] step 3/3 done"
                      f"{' (PARTIAL -- interlock tripped)' if tripped else ''}: "
                      f"saved {run_path}/{file_name}_rabi_mw_us.npy "
                      f"({n_completed} points), _rabi_x.npy, _rabi_y.npy, "
                      f"_rabi_reflected_dbm.npy, _rabi_ref_freq_hz.npy, "
                      f"_rabi_reference_unlock.npy, _rabi_metadata.txt")
        finally:
            print("[rabi] shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            try:
                amp_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off amplifier supply "
                      f"cleanly ({e})")
            amp_psu.close()
            try:
                coil_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off coil supply cleanly ({e})")
            try:
                coil_psu.go_to_local()
            except Exception:
                pass
            coil_psu.close()
            if ilock_sa is not None:
                ilock_sa.close()
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off SDG1062X output "
                      f"cleanly ({e})")
            try:
                sdg.close()
            except Exception:
                pass

    print("[rabi] done")


def cmd_run_no_mw(file_name, **kw):
    """
    Crosstalk-isolation diagnostic: a SEPARATE, self-contained sweep --
    same AWG sequences, same CH2 gate/switch toggling between mw-on-block
    and mw-off-block reps, same n_reps/tau_mw sweep, same lock-in reads as
    cmd_run() -- but with NO generator, NO spectrum-analyzer interlock, NO
    reflected-power scan/check, and NO amplifier/coil power supplies at
    all. This function never connects to or commands the HP8673H
    generator, SPD1168X amplifier supply, or SPD1305X coil supply in any
    way -- turn the generator's RF output off (or disconnect it) and
    power down both supplies yourself before running this. That's the
    point: we don't want this code silently relying on anything (preset/
    power/frequency/RF-on, or PSU turn_on()) that could re-enable them
    unexpectedly, and we want to test whether the spurious signal (see
    below) depends on either supply being live at all.

    Motivation: real hardware testing found a lock-in signal off the NV
    resonance frequency, and found it persists even with the amplifier's
    output routed into a dummy load instead of near the diamond (see
    notes.md's "spurious off-resonance/no-MW-near-sample signal" entry).
    That signal disappears when the PMT's optical path is physically
    blocked, meaning it's a genuine optical signal, not electrical pickup
    straight into the PMT cable -- but it doesn't need real MW-NV coupling
    either. Confirmed on real hardware to persist even with the
    generator's RF confirmed off (a separate run-no-mw test) -- CH2 was
    still toggling the switch/amplifier chain, so that pointed at CH2's
    switching action itself (AWG crosstalk, or the switch/amplifier
    drawing current even with no RF applied) rather than real RF power.
    This version goes one step further and also removes both power
    supplies from the picture, to check whether the effect depends on the
    amplifier or coil supply being powered at all, or persists even with
    them off too (which would point at something in the AWG/lock-in/PMT
    chain itself, independent of any of the RF-side hardware).

    Recognized key=value overrides (all optional, same meaning/defaults as
    cmd_run()'s unless noted): mw_start_us, mw_stop_us, mw_step_us, n_reps,
    laser_us, pre_us, post_us, time_constant_s, settle_periods,
    settle_time_constants, sensitivity_v, auto_sensitivity, phase_deg,
    input_coupling, auto_rescale_on_overload, max_rescale_attempts,
    auto_rescale_on_underload, underload_margin, underload_persistence,
    fixed_sensitivity, ch1_vpp, ch2_vpp, ch2_offset_v, trigger_margin.
    freq_hz/drive_power_dbm are accepted
    too, but ONLY as labels recorded in the log/metadata (e.g. so the
    saved file documents what frequency the generator was physically
    parked at) -- neither is ever sent to any instrument here. No
    psu_voltage_v/psu_current_limit_a/coil_current_a/coil_voltage_margin
    overrides -- there's no PSU here to apply them to.

    Saves the same {file_name}_rabi_mw_us.npy/_rabi_x.npy/_rabi_y.npy/
    _rabi_reflected_dbm.npy/_rabi_metadata.txt file set as cmd_run(), so it
    loads into rabi_result.ipynb the same way -- _rabi_reflected_dbm.npy
    is all-NaN here (no spectrum analyzer involved) rather than real
    readings. Use a distinct file_name so you don't overwrite a real run.
    """
    _run_no_mw_impl(file_name, ch2_hold_constant=False, **kw)


def cmd_run_ch2_constant(file_name, **kw):
    """
    Crosstalk-isolation diagnostic, second stage: builds on cmd_run_no_mw()
    (no generator, no interlock, no amplifier/coil PSUs -- turn those off/
    disconnect them yourself before running this too) and additionally
    holds CH2's own ANALOG output constant (physically off, never
    switching) via setup_awg_sequences(ch2_hold_constant=True), while the
    marker/reference it derives from ("highAtStartGoLow", routed to
    the lock-in via OUTPut:SYNC:SOURce CH2) still toggles exactly as
    normal -- see that function's docstring for exactly what changes.

    Motivation: cmd_run_no_mw() confirmed a spurious signal persists with
    the generator's RF and both PSUs all off, leaving only the AWG (CH1
    laser/AOM drive, CH2 switch-control/marker) and lock-in active -- see
    notes.md's "spurious off-resonance/no-MW-near-sample signal" entry.
    Since CH2 supplies BOTH the switch control voltage AND the lock-in's
    reference (via the shared marker/Sync BNC), it wasn't yet clear
    whether the effect requires CH2's own analog output to physically
    switch, or only requires the marker/reference to toggle. This command
    tests exactly that: if the spurious signal DISAPPEARS with CH2 held
    perfectly constant (no physical switching at all, just an unchanging
    DC level) while the reference keeps toggling normally, that confirms
    it depends on CH2's own analog switching action specifically. If it
    PERSISTS even with CH2 truly constant, the culprit is something in the
    marker/Sync output circuitry itself (or elsewhere entirely), not CH2's
    analog DAC/switch-driving output.

    Also fixes the SDG1062X external trigger at a single constant
    frequency for the WHOLE sweep, rather than recomputing it every point
    from the current mw_us (see cmd_run()'s normal per-point behavior).
    That per-point recomputation means the trigger frequency -- and
    therefore something that's actually changing over time throughout
    every sweep run so far, constant-CH2 or not -- decreases monotonically
    across the sweep right along with tau_mw (rep_us grows with mw_us, so
    ref_period_s grows, so margin/ref_period_s shrinks). Since a
    monotonic, decaying R vs. tau_mw trend was observed even with CH2
    held constant, this was flagged as a real confound: it wasn't yet
    possible to tell whether that trend reflects something about tau_mw
    itself, or just the trigger frequency happening to be a monotonic
    function of tau_mw in every run collected so far. Fixing the trigger
    frequency here removes that confound FOR THIS COMMAND ONLY (not yet
    applied to cmd_run() or cmd_run_no_mw()/cmd_run_ch1_ch2_constant(),
    which still recompute it every point) -- if the decaying trend
    disappears once the trigger frequency stops varying, that would
    confirm it was a trigger-frequency artifact, not a tau_mw-dependent
    one. The fixed rate is chosen from mw_start_us (the sweep's shortest
    rep, hence its largest bare-minimum trigger requirement) with the
    usual trigger_margin safety factor -- comfortably fast enough for
    every other (longer) rep in the sweep too, since a trigger faster than
    the bare minimum is always safe (see _configure_external_trigger()'s
    docstring on trigger_margin).

    Also accepts anchor_free_reps (default 20 here -- see setup_awg_
    sequences()'s docstring): CH2 still has an onceWaitTrig anchor, since
    its own analog LEVEL matches gate_off_rep's anyway (unlike CH1's
    former anchor, this one isn't visibly "wrong" -- the concern is the
    trigger-wait DEAD TIME delaying exactly when each off/on cycle
    starts). anchor_free_reps sets the repeat_count on CH2's ONE combined
    arb listing, so the trigger-wait glitch recurs only once every
    anchor_free_reps cycles instead of every cycle -- diluting its
    contribution to whatever gets averaged over settle_s. Set
    anchor_free_reps=1 to restore the original every-cycle behavior.
    Applies to CH1 too (via setup_awg_sequences(), which keeps both
    channels wrapping through their anchors together) -- confirmed on
    real hardware that applying it to CH2 alone visibly misaligns CH1
    against the Sync/marker output over time. This default of 20 predates
    the combined-arb fix that removed the old ~250-listing sequence-table
    ceiling (see setup_awg_sequences()'s docstring) -- there's no longer
    a structural reason to keep it this low, it just hasn't been revisited
    since.

    Same key=value overrides as cmd_run_no_mw() (see its docstring) --
    this is a thin wrapper that also sets ch2_hold_constant=True and
    fixed_external_trigger=True on every setup_awg_sequences() call.
    Saves to the same {file_name}_rabi_* files -- use a distinct
    file_name so you don't overwrite another run.
    """
    kw.setdefault("anchor_free_reps", 20)
    _run_no_mw_impl(file_name, ch2_hold_constant=True, ch1_hold_constant=False,
                     fixed_external_trigger=True, **kw)


def cmd_run_ch1_ch2_constant(file_name, **kw):
    """
    Crosstalk-isolation diagnostic, third stage: builds on
    cmd_run_ch2_constant() (no generator, no interlock, no PSUs, CH2's
    analog output already held constant while its marker still toggles)
    and additionally holds CH1 constant too -- via
    setup_awg_sequences(ch1_hold_constant=True), CH1 becomes a plain,
    non-sequenced continuous 80 MHz sine at ch1_vpp instead of running its
    own DATA:SEQ sequence at all. See that function's docstring for why
    this matters even with ch2_hold_constant already on: CH1's OWN
    sequence table still transitioned between two separately-listed "rep"
    segments at the same block boundary as CH2 (both marked "maintain",
    so nothing visibly changed on CH1's output, but the AWG's internal
    sequencer still processed a segment-advance event there) -- that
    couldn't be ruled out as an internal source on its own until now.

    With this on, the ONLY thing anywhere in the AWG still synchronous
    with the block reference is CH2's marker/Sync output -- CH1 is a
    completely free-running, unsequenced tone, decoupled from the block
    structure entirely. If the spurious signal disappears here, the
    mechanism needed CH1's own sequencer transition (not just CH2's
    marker) to occur; if it persists, that further isolates the marker/
    Sync line itself (or something even further upstream) as the sole
    remaining candidate, independent of anything happening on CH1's own
    sequence table.

    Same key=value overrides as cmd_run_no_mw() (see its docstring) --
    this is a thin wrapper that sets both ch2_hold_constant=True and
    ch1_hold_constant=True on every setup_awg_sequences() call. Saves to
    the same {file_name}_rabi_* files -- use a distinct file_name so you
    don't overwrite another run.
    """
    _run_no_mw_impl(file_name, ch2_hold_constant=True, ch1_hold_constant=True, **kw)


def _run_no_mw_impl(file_name, ch2_hold_constant, ch1_hold_constant=False,
                     fixed_external_trigger=False, **kw):
    """Shared implementation behind cmd_run_no_mw(), cmd_run_ch2_constant(),
    and cmd_run_ch1_ch2_constant() -- see their docstrings. Never connects
    to or commands the generator or either PSU.

    fixed_external_trigger=True (see cmd_run_ch2_constant()'s docstring):
    configures the SDG1062X's trigger frequency ONCE, before the sweep
    starts, from the sweep's shortest rep (mw_start_us -- the largest bare
    -minimum trigger requirement, so the resulting fixed rate is always
    fast enough for every other, longer rep too), instead of recomputing
    it every point from the CURRENT mw_us. Removes the trigger frequency
    as a variable that changes over the course of the sweep."""
    freq_hz = float(kw.get("freq_hz", 0.0))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    mw_start_us = float(kw.get("mw_start_us", 0.02))
    mw_stop_us = float(kw.get("mw_stop_us", 5.0))
    mw_step_us = float(kw.get("mw_step_us", 0.02))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    settle_periods = float(kw.get("settle_periods", 5.0))
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    sensitivity_v = float(kw.get("sensitivity_v", 5e-3))
    auto_sensitivity = str(kw.get("auto_sensitivity", "true")).lower() == "true"
    phase_deg = float(kw.get("phase_deg", 0.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    auto_rescale_on_overload = str(kw.get("auto_rescale_on_overload", "true")).lower() == "true"
    max_rescale_attempts = int(kw.get("max_rescale_attempts", 3))
    auto_rescale_on_underload = str(kw.get("auto_rescale_on_underload", "true")).lower() == "true"
    underload_margin = float(kw.get("underload_margin", 0.5))
    # See cmd_run()'s underload_persistence comment -- same overload/
    # underload oscillation fix applied here.
    underload_persistence = int(kw.get("underload_persistence", 3))
    # See cmd_run()'s fixed_sensitivity comment -- same convenience
    # override, and the same ordering requirement (must come after
    # auto_sensitivity/auto_rescale_on_overload/auto_rescale_on_underload
    # are all parsed above).
    fixed_sensitivity = str(kw.get("fixed_sensitivity", "false")).lower() == "true"
    if fixed_sensitivity:
        auto_sensitivity = False
        auto_rescale_on_overload = False
        auto_rescale_on_underload = False
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 100))
    anchor_free_reps = int(kw.get("anchor_free_reps", 1))
    # See cmd_run()'s resequence_interval comment.
    resequence_interval = int(kw.get("resequence_interval", RESEQUENCE_INTERVAL))
    # See cmd_run()'s extra_settle_s comment.
    extra_settle_s = float(kw.get("extra_settle_s", 0.0))

    mw_values_us = np.arange(mw_start_us, mw_stop_us + mw_step_us / 2, mw_step_us)

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_rabi.txt"

    with _tee_stdout(log_path):
        if ch1_hold_constant and ch2_hold_constant:
            tag = "run-ch1-ch2-constant"
        elif ch2_hold_constant:
            tag = "run-ch2-constant"
        else:
            tag = "run-no-mw"
        print(f"[rabi] {tag}: crosstalk-isolation diagnostic -- NO generator, "
              f"NO interlock, NO amplifier/coil PSUs. RF, amp supply, and coil "
              f"supply must all be off/disconnected by you."
              f"{' CH2 held physically constant (no switching) this run.' if ch2_hold_constant else ''}"
              f"{' CH1 held constant (plain continuous 80 MHz sine, no sequence) this run.' if ch1_hold_constant else ''} "
              f"tau_mw sweep: {mw_start_us}-{mw_stop_us} us, step {mw_step_us} us "
              f"({len(mw_values_us)} points), n_reps={n_reps}")

        print("[rabi] configuring AWG + SDG1062X (external trigger) + SR830 "
              "lock-in -- no generator, no interlock, no PSUs")
        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=False)
        sdg = SDG1062X(SDG_RESOURCE, debug=False)
        lia = SR830(SR830_RESOURCE, debug=True)
        setup_lock_in(lia, time_constant_s, sensitivity_v, phase_deg, input_coupling,
                      auto_sensitivity=auto_sensitivity)
        print(f"[rabi] done: time constant {time_constant_s*1e3:.1f} ms, "
              f"phase {phase_deg} deg")

        if fixed_external_trigger:
            # Worst case (highest bare-minimum trigger requirement) is the
            # sweep's SHORTEST rep, at mw_start_us -- fast enough here
            # means comfortably fast enough for every longer rep later in
            # the sweep too (see _configure_external_trigger()'s margin
            # discussion).
            fixed_rep_us = laser_us + pre_us + mw_start_us + post_us
            fixed_ref_period_s = 2 * n_reps * fixed_rep_us * 1e-6
            fixed_trigger_freq_hz = _configure_external_trigger(
                sdg, fixed_ref_period_s, margin=trigger_margin)
            print(f"[rabi] fixed_external_trigger=true: SDG1062X trigger fixed at "
                  f"{fixed_trigger_freq_hz/1e3:.1f} kHz for the whole sweep "
                  f"(derived from mw_start_us={mw_start_us} us)")

        try:
            print(f"[rabi] sweeping tau_mw (no MW/generator/PSUs involved)")

            x_values = np.full(len(mw_values_us), np.nan)
            y_values = np.full(len(mw_values_us), np.nan)
            reflected_dbm_arr = np.full(len(mw_values_us), np.nan)
            n_completed = 0
            underload_streak = 0

            try:
                for i, mw_us in enumerate(mw_values_us):
                    if i > 0 and i % resequence_interval == 0:
                        print(f"[rabi] point {i + 1}/{len(mw_values_us)}: resetting AWG "
                              f"to clear its sequence table (every "
                              f"{resequence_interval} points)")
                        awg.reset()
                        awg.write("SOUR1:DATA:VOL:CLE")
                        awg.write("SOUR2:DATA:VOL:CLE")

                    rep_us = laser_us + pre_us + mw_us + post_us
                    ref_period_s = 2 * n_reps * rep_us * 1e-6
                    settle_s = max(settle_periods * ref_period_s,
                                    settle_time_constants * time_constant_s)

                    if not fixed_external_trigger:
                        _configure_external_trigger(sdg, ref_period_s, margin=trigger_margin)

                    setup_awg_sequences(
                        awg, mw_us, n_reps, laser_us, pre_us, post_us,
                        sequence_name_ch1=f"rabi_ch1_{i}",
                        sequence_name_ch2=f"rabi_ch2_{i}",
                        ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
                        ch2_hold_constant=ch2_hold_constant,
                        ch1_hold_constant=ch1_hold_constant,
                        anchor_free_reps=anchor_free_reps,
                    )
                    if extra_settle_s > 0:
                        time.sleep(extra_settle_s)

                    lia.read_overload_status()

                    if i == 0 and auto_sensitivity:
                        time.sleep(settle_s)
                        lia.auto_gain()
                        time.sleep(settle_s)
                        actual_sensitivity_v = lia.get_sensitivity_v()
                        print(f"[rabi] auto_sensitivity: AGAN selected "
                              f"{actual_sensitivity_v:.3e} V full scale")
                        sensitivity_v = actual_sensitivity_v

                    _wait_settle_discarding_transient_overload(lia, settle_s)

                    x, y = lia.read_xy()

                    overloaded_this_point = False
                    if auto_rescale_on_overload:
                        for attempt in range(max_rescale_attempts):
                            overload_status = lia.read_overload_status()
                            if not overload_status["any"]:
                                break
                            overloaded_this_point = True
                            old_v = lia.get_sensitivity_v()
                            new_v = _step_sensitivity_coarser(lia)
                            lia.read_overload_status()
                            print(f"[rabi] OVERLOAD at tau_mw={mw_us:.3f} us "
                                  f"(point {i + 1}/{len(mw_values_us)}): rescaling "
                                  f"sensitivity {old_v:.3e} V -> {new_v:.3e} V full "
                                  f"scale, re-reading (attempt {attempt + 1}/"
                                  f"{max_rescale_attempts})")
                            sensitivity_v = new_v
                            _wait_settle_discarding_transient_overload(lia, settle_s)
                            x, y = lia.read_xy()
                        else:
                            print(f"[rabi] tau_mw={mw_us:.3f} us: still overloading "
                                  f"after {max_rescale_attempts} rescale attempts -- "
                                  f"saving as-is")

                    if overloaded_this_point:
                        # This point just got coarsened out of an overload
                        # -- don't let it count toward underload_persistence
                        # at all (even if it happens to look "underloaded"
                        # relative to the NEW coarser range); that's
                        # exactly the single-point-triggered bounce this
                        # persistence check exists to prevent.
                        underload_streak = 0
                    elif auto_rescale_on_underload:
                        idx = SR830.SENSITIVITY_V.index(sensitivity_v)
                        if idx > 0:
                            r = (x ** 2 + y ** 2) ** 0.5
                            next_v = SR830.SENSITIVITY_V[idx - 1]
                            if r < underload_margin * next_v:
                                underload_streak += 1
                                if underload_streak >= underload_persistence:
                                    old_v = sensitivity_v
                                    new_v = _step_sensitivity_finer(lia)
                                    print(f"[rabi] tau_mw={mw_us:.3f} us (point {i + 1}/"
                                          f"{len(mw_values_us)}): R={r:.3e} V well under "
                                          f"the current {old_v:.3e} V full scale for "
                                          f"{underload_streak} consecutive points -- "
                                          f"stepping sensitivity down to {new_v:.3e} V "
                                          f"full scale for the next point")
                                    sensitivity_v = new_v
                                    underload_streak = 0
                            else:
                                underload_streak = 0
                        else:
                            underload_streak = 0
                    else:
                        underload_streak = 0

                    x_values[i] = x
                    y_values[i] = y
                    n_completed = i + 1
                    print(f"[rabi] point {n_completed}/{len(mw_values_us)}: "
                          f"tau_mw={mw_us:.3f} us, X={x:.6e} V, Y={y:.6e} V")
            except KeyboardInterrupt:
                print("[rabi] stopped by user (Ctrl+C)")

            mw_values_us_trimmed = mw_values_us[:n_completed]
            x_values = x_values[:n_completed]
            y_values = y_values[:n_completed]
            reflected_dbm_arr = reflected_dbm_arr[:n_completed]

            if n_completed == 0:
                print("[rabi] FAILED: no points completed -- nothing to save")
            else:
                np.save(f"{run_path}/{file_name}_rabi_mw_us.npy", mw_values_us_trimmed)
                np.save(f"{run_path}/{file_name}_rabi_x.npy", x_values)
                np.save(f"{run_path}/{file_name}_rabi_y.npy", y_values)
                np.save(f"{run_path}/{file_name}_rabi_reflected_dbm.npy", reflected_dbm_arr)
                with open(f"{run_path}/{file_name}_rabi_metadata.txt", "w") as fh:
                    fh.write(f"freq_hz={freq_hz}\n")
                    fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                    fh.write(f"n_reps={n_reps}\n")
                    fh.write(f"laser_us={laser_us}\n")
                    fh.write(f"pre_us={pre_us}\n")
                    fh.write(f"post_us={post_us}\n")
                    fh.write(f"time_constant_s={time_constant_s}\n")
                    fh.write(f"settle_periods={settle_periods}\n")
                    fh.write(f"settle_time_constants={settle_time_constants}\n")
                    fh.write(f"sensitivity_v={sensitivity_v}\n")
                    fh.write(f"phase_deg={phase_deg}\n")
                print(f"[rabi] done: saved {run_path}/{file_name}_rabi_mw_us.npy "
                      f"({n_completed} points), _rabi_x.npy, _rabi_y.npy, "
                      f"_rabi_reflected_dbm.npy (all-NaN), _rabi_metadata.txt")
        finally:
            print("[rabi] shutting down")
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off SDG1062X output "
                      f"cleanly ({e})")
            try:
                sdg.close()
            except Exception:
                pass

    print(f"[rabi] {tag} done")


def cmd_calibrate_phase(file_name, **kw):
    """
    Runs the AWG/lock-in continuously at a single, fixed tau_mw and calls
    the SR830's built-in auto-phase (APHS) to null Y against the real
    CH2-sourced block reference -- this is the gap flagged in cmd_run()'s
    docstring (phase_deg is NOT auto-calibrated by cmd_run() itself). Per
    notes.md's phase-calibration guidance, auto_phase() needs a real,
    reasonably strong signal to get a stable reading -- pick mw_us to
    maximize contrast (e.g. wherever a previous cmd_run()'s Rabi data
    showed the biggest X/Y swing), not an arbitrary weak point.

    Prints and saves the resulting phase_deg to
    {file_name}_calibrate_phase.txt -- pass it into a real cmd_run() via
    phase_deg=<value>.

    Recognized key=value overrides (all optional): freq_hz, drive_power_dbm,
    mw_us, n_reps, laser_us, pre_us, post_us, time_constant_s,
    settle_time_constants, input_coupling, ch1_vpp, ch2_vpp, ch2_offset_v,
    trigger_margin, psu_voltage_v, psu_current_limit_a, coil_current_a,
    coil_voltage_margin -- same meaning/defaults as cmd_run()'s.
    """
    freq_hz = float(kw.get("freq_hz", 2.843e9))
    drive_power_dbm = float(kw.get("drive_power_dbm", 0.0))
    mw_us = float(kw.get("mw_us", 1.0))
    n_reps = int(kw.get("n_reps", 250))
    laser_us = float(kw.get("laser_us", 2.0))
    pre_us = float(kw.get("pre_us", 1.0))
    post_us = float(kw.get("post_us", 1.0))
    time_constant_s = float(kw.get("time_constant_s", 0.1))
    # See cmd_run()'s settle_time_constants comment -- 9 TC needed for the
    # 24 dB/oct filter to actually settle (~98%), not 5.
    settle_time_constants = float(kw.get("settle_time_constants", 9.0))
    input_coupling = str(kw.get("input_coupling", "ac"))
    ch1_vpp = float(kw.get("ch1_vpp", 0.632))
    ch2_vpp = float(kw.get("ch2_vpp", 5.0))
    ch2_offset_v = float(kw.get("ch2_offset_v", 2.5))
    trigger_margin = float(kw.get("trigger_margin", 100))
    psu_voltage_v = float(kw.get("psu_voltage_v", 12.0))
    psu_current_limit_a = float(kw.get("psu_current_limit_a", 1.9))
    coil_current_a = float(kw.get("coil_current_a", 2.0))
    coil_voltage_margin = float(kw.get("coil_voltage_margin", 1.5))

    run_path = f"{DATA_DIR}/{file_name}"
    import os
    os.makedirs(run_path, exist_ok=True)
    log_dir = f"{DATA_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    log_path = f"{log_dir}/{file_name}_calibrate_phase.txt"

    with _tee_stdout(log_path):
        print(f"[rabi] phase calibration: tau_mw={mw_us} us, n_reps={n_reps}, "
              f"fixed frequency {freq_hz/1e9:.5f} GHz, {drive_power_dbm} dBm")

        awg = ks33600a.KS33600A(AWG_RESOURCE, debug=True)
        sdg = SDG1062X(SDG_RESOURCE, debug=True)
        lia = SR830(SR830_RESOURCE, debug=True)
        # phase_deg=0 as a starting point -- auto_phase() below finds the
        # real value; auto_sensitivity=True so the initial sensitivity_v
        # doesn't matter.
        setup_lock_in(lia, time_constant_s, sensitivity_v=5e-3, phase_deg=0.0,
                      input_coupling=input_coupling, auto_sensitivity=True)

        gen = HP8673H(GEN_RESOURCE)
        amp_psu = SPD1168X(AMP_PSU_RESOURCE)
        coil_psu = SPD1305X(COIL_PSU_RESOURCE)
        try:
            amp_psu.turn_on(psu_voltage_v, psu_current_limit_a)
            coil_voltage_v = voltage_for_current(coil_current_a) * coil_voltage_margin
            coil_psu.turn_on(coil_voltage_v, coil_current_a)

            gen.preset()
            gen.set_power_dbm(drive_power_dbm)
            gen.set_frequency_hz(freq_hz)
            gen.rf_on()
            time.sleep(1.0)  # let the initial frequency/level settle

            rep_us = laser_us + pre_us + mw_us + post_us
            ref_period_s = 2 * n_reps * rep_us * 1e-6
            settle_s = max(5 * ref_period_s, settle_time_constants * time_constant_s)

            _configure_external_trigger(sdg, ref_period_s, margin=trigger_margin)
            setup_awg_sequences(
                awg, mw_us, n_reps, laser_us, pre_us, post_us,
                sequence_name_ch1="calib_ch1", sequence_name_ch2="calib_ch2",
                ch1_vpp=ch1_vpp, ch2_vpp=ch2_vpp, ch2_offset_v=ch2_offset_v,
            )

            print(f"[rabi] settling {settle_s * 1e3:.0f} ms, then auto-gain + auto-phase")
            time.sleep(settle_s)
            lia.auto_gain()
            time.sleep(settle_s)

            lia.auto_phase()
            time.sleep(settle_s)
            phase_deg = lia.get_phase_deg()
            x, y = lia.read_xy()

            print(f"[rabi] auto-phase result: phase_deg={phase_deg:.3f} deg "
                  f"(X={x:.6e} V, Y={y:.6e} V after nulling)")
            print(f"[rabi] use this with cmd_run() via phase_deg={phase_deg:.3f}")

            with open(f"{run_path}/{file_name}_calibrate_phase.txt", "w") as fh:
                fh.write(f"freq_hz={freq_hz}\n")
                fh.write(f"drive_power_dbm={drive_power_dbm}\n")
                fh.write(f"mw_us={mw_us}\n")
                fh.write(f"n_reps={n_reps}\n")
                fh.write(f"phase_deg={phase_deg}\n")
                fh.write(f"x_after_null={x}\n")
                fh.write(f"y_after_null={y}\n")
        finally:
            print("[rabi] shutting down")
            try:
                gen.rf_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off RF cleanly ({e})")
            try:
                gen.go_to_local()
            except Exception:
                pass
            gen.close()
            try:
                amp_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off amplifier supply "
                      f"cleanly ({e})")
            amp_psu.close()
            try:
                coil_psu.turn_off()
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off coil supply cleanly ({e})")
            try:
                coil_psu.go_to_local()
            except Exception:
                pass
            coil_psu.close()
            try:
                lia.go_to_local()
            except Exception:
                pass
            lia.close()
            try:
                awg.write("OUTPUT1 OFF")
                awg.write("OUTPUT2 OFF")
            except Exception:
                pass
            try:
                awg.close()
            except Exception:
                pass
            try:
                sdg.write("C1:OUTP OFF")
            except Exception as e:
                print(f"[rabi] WARNING: failed to turn off SDG1062X output "
                      f"cleanly ({e})")
            try:
                sdg.close()
            except Exception:
                pass

    print("[rabi] phase calibration done")


RUN_PARAMS_HELP = """
'run' (cmd_run()) key=value overrides, with their current defaults --
also apply to 'run-no-mw'/'run-ch2-constant'/'run-ch1-ch2-constant'
(_run_no_mw_impl()) except where noted, though several defaults differ
there (that impl never touches the generator/PSUs/interlock, so it skips
the psu_*/coil_*/interlock_*/reflected_power_scan/res_*/fine_* group
entirely). See each parameter's inline comment in the source (rg the
name in rabi.py) and notes.md for the reasoning behind non-obvious ones.

Sweep range:
    freq_hz=2.843e9            fixed MW frequency for the whole sweep
    drive_power_dbm=0.0        generator power at freq_hz
    threshold_dbm=-10.0        reflected-power interlock trip threshold
    mw_start_us=0.02           tau_mw sweep start
    mw_stop_us=5.0             tau_mw sweep stop
    mw_step_us=0.02            tau_mw sweep step
    n_reps=250                 on+off reps per point's combined arb

Pulse timing:
    laser_us=2.0               laser/AOM gate duration per rep
    pre_us=1.0                 dead time before the MW pulse
    post_us=1.0                dead time after the MW pulse

Lock-in:
    time_constant_s=0.1        SR830 RC time constant
    settle_periods=5.0         min wait, in reference periods
    settle_time_constants=9.0  min wait, in time constants (usually dominant)
    sensitivity_v=5e-3         SR830 full-scale sensitivity (run-no-mw etc.: 5e-3)
    auto_sensitivity=true      AGAN once at the first point
    phase_deg=0.0              SR830 reference phase
    input_coupling=ac          SR830 input coupling ("ac"/"dc")

Overload/underload auto-rescaling:
    auto_rescale_on_overload=true
    max_rescale_attempts=3
    auto_rescale_on_underload=true
    underload_margin=0.5
    underload_persistence=3    consecutive underload points before stepping finer
    fixed_sensitivity=false    true disables all three rescale flags above,
                               using sensitivity_v as-is for the whole sweep

Amplifier/coil PSUs (run only -- not in run-no-mw/etc.):
    psu_voltage_v=12.0
    psu_current_limit_a=1.9
    coil_current_a=2.0
    coil_voltage_margin=1.5

Interlock (run only):
    interlock_check_interval=5     points between periodic reflected-power checks
    interlock_hold_periods=3.0     MAX HOLD duration, in reference periods
    interlock_during_sweep=true    false skips the PERIODIC check entirely
                                    (the pre-flight check before the sweep
                                    always still runs) -- no protection
                                    while sweeping if false, use with care

AWG sequencing / background-artifact avoidance (see notes.md's "spurious
off-resonance/no-MW-near-sample signal" entry for the full investigation):
    resequence_interval=4 (module RESEQUENCE_INTERVAL)
                                    points between full awg.reset() (*RST)
    extra_settle_s=0.0              extra wait after each point's reupload/
                                     re-arm, before settle_s's own countdown --
                                     1.0 confirmed to eliminate the rare large
                                     (~1.5e-4 V) output_overload rail spikes
    ch1_vpp=0.632
    ch2_vpp=5.0
    ch2_offset_v=2.5
    trigger_margin=3.0 (run-no-mw etc.: 100)
    anchor_free_reps=1000 (run-no-mw etc.: 1, run-ch2-constant: 20)
                                    if you raise extra_settle_s, raise this
                                    too -- anchor_period_s must still exceed
                                    extra_settle_s + settle_s at the sweep's
                                    shortest tau_mw
    fixed_external_trigger=true (run-no-mw etc.: false)

Pre-flight reflected-power scan (run only):
    reflected_power_scan=true
    res_span_hz=100e6
    coarse_step_hz=2e6
    fine_span_hz=20e6
    fine_step_hz=50e3
    res_power_dbm=-40.0
    res_cal_dir=None
    fine_sweep=true                 false skips just the fine stage
""".strip("\n")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print(RUN_PARAMS_HELP)
        raise SystemExit(1)

    command = sys.argv[1]
    file_name = sys.argv[2]
    extra = parse_kv_args(sys.argv[3:])

    if command == "run":
        cmd_run(file_name, **extra)
    elif command == "run-no-mw":
        cmd_run_no_mw(file_name, **extra)
    elif command == "run-ch2-constant":
        cmd_run_ch2_constant(file_name, **extra)
    elif command == "run-ch1-ch2-constant":
        cmd_run_ch1_ch2_constant(file_name, **extra)
    elif command == "calibrate-phase":
        cmd_calibrate_phase(file_name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} "
                          f"(expected 'run', 'run-no-mw', 'run-ch2-constant', "
                          f"'run-ch1-ch2-constant', or 'calibrate-phase')")


if __name__ == "__main__":
    main()
