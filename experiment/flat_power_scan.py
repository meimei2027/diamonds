"""
flat_power_scan.py -- power-flattened resonance scan. NOT YET TESTED.

Motivation: a plain frequency sweep across the resonance's FWHM (what
hp8673h.py's resonance_sweep()/fine_sweep() do) holds the GENERATOR's output
power fixed, not the power actually delivered to the resonator. The
amplifier chain's gain isn't flat with frequency, so the forward power
reaching the resonator varies across the sweep even though the generator
setting doesn't -- any shape seen in reflected power vs. frequency is a mix
of the resonator's real response and the chain's own gain ripple.

This script separates those two effects into two steps:

  1. `lookup`: build a fine forward-power lookup table by sweeping the
     generator broadly at one fixed reference power and measuring what the
     coupler's FORWARD port actually sees at each frequency (gain_db(f) =
     measured forward dBm - reference generator dBm). Physical setup: same
     as the "amplifier gain check" step in cw_odmr.ipynb -- amplifier output
     -> isolator -> coupler -> FORWARD port -> spectrum analyzer, NOT
     connected to the resonator.

  2. `scan`: physical setup back to normal (coupler forward port ->
     resonator, reflected port -> spectrum analyzer). Runs a normal
     coarse-then-fine resonance_sweep() to find f0 and FWHM, then computes
     (from the step-1 lookup table) the generator power needed at each
     frequency within the FWHM span so the ACTUAL forward power delivered is
     flat at a chosen target level -- and re-scans just that span with those
     per-frequency power settings.

Usage:
    python flat_power_scan.py lookup <name> [key=value ...]
        Saves data/<name>_lookup.csv (frequency_hz, forward_dbm, gain_db)
        and data/<name>_lookup_meta.txt (ref_power_dbm).

        Recognized key=value overrides (all optional):
          start_hz=2e9         lookup sweep start frequency
          stop_hz=3e9          lookup sweep stop frequency
          step_hz=1e6          lookup sweep step (coarser than the resonance
                                fine sweep is fine -- gain(f) should vary
                                slowly compared to a resonance dip)
          ref_power_dbm=-30.0  generator power used while measuring gain(f)
                                -- must be low enough that the amplifier is
                                still linear (see notes.md for prior gain
                                measurements); the "flat power" step later
                                assumes gain(f) measured here still applies
                                at whatever (possibly different) power it
                                computes per frequency -- true only if the
                                amplifier stays linear across that range too

    python flat_power_scan.py scan <lookup_name> <name> [key=value ...]
        Saves data/<name>_resonance_coarse.csv, _fine.csv (from the normal
        resonance_sweep() step) and data/<name>_flat.csv (frequency_hz,
        generator_power_dbm, reflected_dbm from the flat-power re-scan).

        Recognized key=value overrides (all optional):
          res_start_hz=2e9        resonance sweep start frequency
          res_stop_hz=3e9         resonance sweep stop frequency
          coarse_step_hz=6.7e6    coarse sweep step (track expected FWHM)
          fine_span_hz=20e6       fine sweep span around the coarse dip
          fine_step_hz=50e3       fine sweep step
          res_power_dbm=-40.0     drive power during the resonance sweep
          fwhm_margin=1.0         flat-power scan span = FWHM * this margin
          flat_step_hz=50e3       flat-power scan step
          target_forward_dbm=0.0  forward power the flat scan tries to hold
                                   constant -- NOT calibrated against any
                                   safety limit yet, see caveats below
          max_power_dbm=10.0      clamp on computed generator power -- purely
                                   a placeholder guess, NOT a verified safe
                                   limit for this generator/amplifier
          coupling_db=20.0        the coupler's forward/reflected coupling
                                   factor, used by the interlock threshold
                                   calculation (see
                                   compute_interlock_thresholds_dbm())
          interlock_margin_db=10.0          margin above the reflected power
                                             predicted from the normal
                                             resonance sweep's fine sweep
          interlock_ceiling_margin_db=10.0  margin below the full-reflection
                                             physical ceiling (a hard backstop,
                                             independent of the fine sweep)

        Reflected power is checked against a per-frequency interlock
        threshold at every point of the flat-power scan (see
        compute_interlock_thresholds_dbm()) -- on trip, RF is turned off
        immediately and only the points read so far are saved (to
        data/<name>_flat.csv, same filename, just fewer rows).

CAVEATS (this script is UNTESTED end-to-end):
  - The interlock thresholds are computed from physical reasoning (the
    normal resonance sweep's measured reflected-power curve, scaled to the
    flat scan's power level, plus a full-reflection physical ceiling) but
    have NOT been checked against real hardware readings yet -- unlike
    cw_odmr.py's -10 dBm threshold, which was derived the same way and then
    validated live. Watch reflected power manually the first few runs.
  - The two subcommands need a manual cable move between them (coupler
    forward port -> SA for `lookup`, reflected port -> SA for `scan`) --
    nothing here checks or enforces that the physical setup matches.
  - target_forward_dbm and max_power_dbm defaults are placeholder guesses,
    not derived from any measured safe operating range -- set them
    deliberately, don't just accept the defaults.
  - The lookup table's gain_db(f) is only valid in the amplifier's linear
    regime at ref_power_dbm; if a per-frequency corrected power pushes the
    amplifier into compression at some frequencies but not others, the
    "flat power" assumption silently breaks there. Worth spot-checking a
    few points with check_sa_calibration.py-style direct verification
    before trusting a `scan` run's results.
"""
import os
import sys
import time

import numpy as np

from hp8673h import HP8673H
from e4403b import E4403B
from cw_odmr import parse_kv_args

# See notes.md -- GPIB bus numbering isn't stable, confirm with
# pyvisa.ResourceManager().list_resources() if these don't match.
GEN_RESOURCE = "GPIB1::19::INSTR"
SA_RESOURCE = "GPIB0::18::INSTR"

DATA_DIR = "data"


def build_power_lookup(name, **kw):
    start_hz = float(kw.get("start_hz", 2.0e9))
    stop_hz = float(kw.get("stop_hz", 3.0e9))
    step_hz = float(kw.get("step_hz", 1e6))
    ref_power_dbm = float(kw.get("ref_power_dbm", -30.0))

    os.makedirs(DATA_DIR, exist_ok=True)

    print("[flat_power_scan] connecting to HP8673H + E4403B")
    gen = HP8673H(GEN_RESOURCE)
    sa = E4403B(SA_RESOURCE)
    try:
        print(f"[flat_power_scan] lookup sweep {start_hz/1e9:.4f}-{stop_hz/1e9:.4f} GHz, "
              f"step {step_hz/1e6:.3f} MHz, ref power {ref_power_dbm} dBm "
              f"-- coupler FORWARD port -> analyzer (NOT the resonator)")
        sa.set_center_span((start_hz + stop_hz) / 2, stop_hz - start_hz)
        freqs_hz, forward_dbm = gen.frequency_sweep(
            sa, start_hz=start_hz, stop_hz=stop_hz, step_hz=step_hz,
            power_dbm=ref_power_dbm,
        )
        gain_db = forward_dbm - ref_power_dbm

        lookup_path = f"{DATA_DIR}/{name}_lookup.csv"
        np.savetxt(
            lookup_path,
            np.column_stack((freqs_hz, forward_dbm, gain_db)),
            delimiter=",", header="frequency_hz,forward_dbm,gain_db", comments="",
        )
        meta_path = f"{DATA_DIR}/{name}_lookup_meta.txt"
        with open(meta_path, "w") as f:
            f.write(f"ref_power_dbm={ref_power_dbm}\n")

        print(f"[flat_power_scan] saved {lookup_path}, {meta_path}")
        print(f"[flat_power_scan] gain range: {gain_db.min():.2f} to {gain_db.max():.2f} dB "
              f"(spread {gain_db.max() - gain_db.min():.2f} dB across the sweep)")
    finally:
        gen.rf_off()
        gen.go_to_local()
        gen.close()
        sa.go_to_local()
        sa.close()


def load_power_lookup(name):
    lookup_path = f"{DATA_DIR}/{name}_lookup.csv"
    meta_path = f"{DATA_DIR}/{name}_lookup_meta.txt"

    table = np.genfromtxt(lookup_path, delimiter=",", names=True)

    ref_power_dbm = None
    with open(meta_path) as f:
        for line in f:
            key, _, value = line.strip().partition("=")
            if key == "ref_power_dbm":
                ref_power_dbm = float(value)
    if ref_power_dbm is None:
        raise ValueError(f"{meta_path} is missing ref_power_dbm")

    return table["frequency_hz"], table["gain_db"], ref_power_dbm


def required_power_dbm(lookup_freqs_hz, lookup_gain_db, query_freqs_hz,
                        target_forward_dbm, max_power_dbm=10.0):
    """
    Interpolate gain_db(f) from the lookup table at query_freqs_hz and
    compute the generator power needed at each frequency so the actual
    forward power delivered equals target_forward_dbm. Clips (with a
    warning) to max_power_dbm -- see the module docstring: this is a
    placeholder guess, not a verified safe limit.
    """
    gain_at_query_db = np.interp(query_freqs_hz, lookup_freqs_hz, lookup_gain_db)
    powers_dbm = target_forward_dbm - gain_at_query_db

    over = powers_dbm > max_power_dbm
    if np.any(over):
        print(f"[flat_power_scan] WARNING: {int(over.sum())} of {len(powers_dbm)} point(s) "
              f"would need > {max_power_dbm} dBm generator power to hit "
              f"{target_forward_dbm} dBm forward -- clipping to {max_power_dbm} dBm "
              f"(flat-power target will NOT be met at those frequencies)")
        powers_dbm = np.clip(powers_dbm, None, max_power_dbm)

    return powers_dbm


def compute_interlock_thresholds_dbm(scan_freqs_hz, flat_powers_dbm,
                                      fine_freqs_hz, fine_power_dbm, res_power_dbm,
                                      target_forward_dbm, coupling_db=20.0,
                                      margin_db=10.0, ceiling_margin_db=10.0):
    """
    Per-frequency reflected-power interlock threshold for the flat-power
    scan. The scan already reads reflected power at every point it visits
    (that's what flat_power_sweep() records), so no extra measurement is
    needed -- this just computes what to compare each reading against.

    Combines two independent estimates and uses whichever is stricter
    (lower) at each frequency:

      1. Measured-response-based: the normal resonance_sweep() step already
         measured reflected power vs. frequency (fine_freqs_hz/
         fine_power_dbm) at res_power_dbm. Reflected power is linear in
         incident power for a passive resonator, so scale that curve (in dB,
         i.e. a direct offset) up to whatever generator power the flat scan
         actually uses at each frequency (flat_powers_dbm), then add
         margin_db as the "this is unexpected" buffer above the predicted
         normal reading.

      2. Full-reflection physical ceiling: reflected power cannot exceed
         forward power. The flat scan is built to make the coupler's
         FORWARD port read target_forward_dbm at every frequency (that's the
         whole point of the lookup table) -- if the coupler's forward and
         reflected ports have the same coupling_db (a dual directional
         coupler normally does), then in the worst case (100% reflection,
         e.g. the resonance has drifted away or the load is disconnected)
         the REFLECTED port would read about the same as the forward port:
         target_forward_dbm. ceiling_margin_db is subtracted from that as a
         safety margin. Unlike estimate 1 (which depends on the resonance
         not having moved since the fine sweep), this is a hard physical
         bound, so it acts as a backstop if estimate 1 is somehow optimistic.

    Neither estimate has been checked against real hardware yet -- see the
    module docstring's caveats.
    """
    predicted_reflected_dbm = (
        np.interp(scan_freqs_hz, fine_freqs_hz, fine_power_dbm)
        + (flat_powers_dbm - res_power_dbm)
    )
    measured_based_dbm = predicted_reflected_dbm + margin_db

    full_reflection_ceiling_dbm = target_forward_dbm - ceiling_margin_db

    return np.minimum(measured_based_dbm, full_reflection_ceiling_dbm)


def flat_power_sweep(gen, sa, freqs_hz, powers_dbm, thresholds_dbm=None,
                      settle_s=0.15, initial_settle_s=1.0):
    """
    Like HP8673H.frequency_sweep(), but power_dbm varies per frequency point
    (from a pre-computed array) instead of being held fixed -- so the actual
    forward power delivered stays flat across frequency despite the
    amplifier chain's own gain not being flat. See frequency_sweep()'s
    docstring (hp8673h.py) for why settle_s must stay nonzero and why the
    first jump needs its own longer initial_settle_s -- both apply here
    unchanged.

    thresholds_dbm, if given, is a per-point reflected-power interlock
    threshold (same length as freqs_hz/powers_dbm, see
    compute_interlock_thresholds_dbm()) -- checked against each reflected-
    power reading as soon as it's taken. On trip: immediately turns off RF
    (gen.trip_interlock()) and stops, returning only the points read so far.

    Returns (freqs_hz_used, powers_dbm_used, readings_dbm, tripped) --
    the first three are truncated to whatever was actually read if tripped
    is True.
    """
    assert len(freqs_hz) == len(powers_dbm)
    if thresholds_dbm is not None:
        assert len(thresholds_dbm) == len(freqs_hz)

    gen.preset()
    gen.set_power_dbm(powers_dbm[0])
    gen.set_frequency_hz(freqs_hz[0])
    gen.rf_on()
    time.sleep(initial_settle_s)

    sa.write("TRAC1:MODE WRITE")
    sa.write("AVER:STATE OFF")
    sa.write("INIT:CONT OFF")
    sa.write("CALC:MARK1:MODE POS")

    readings_dbm = np.empty(len(freqs_hz))
    tripped = False
    n_taken = len(freqs_hz)
    for i, (f, p) in enumerate(zip(freqs_hz, powers_dbm)):
        gen.set_power_dbm(p)
        gen.set_frequency_hz(f)
        if settle_s:
            time.sleep(settle_s)
        sa.write("INIT:IMM")
        sa.query("*OPC?")
        sa.write(f"CALC:MARK1:X {f}")
        reading_dbm = float(sa.query("CALC:MARK1:Y?"))
        readings_dbm[i] = reading_dbm

        if thresholds_dbm is not None and reading_dbm > thresholds_dbm[i]:
            gen.trip_interlock(
                f"reflected power {reading_dbm:.2f} dBm exceeds threshold "
                f"{thresholds_dbm[i]:.2f} dBm at {f/1e9:.5f} GHz "
                f"(point {i + 1}/{len(freqs_hz)})"
            )
            tripped = True
            n_taken = i + 1
            break

    if not tripped:
        gen.rf_off()

    return freqs_hz[:n_taken], powers_dbm[:n_taken], readings_dbm[:n_taken], tripped


def run_scan(lookup_name, name, **kw):
    res_start_hz = float(kw.get("res_start_hz", 2.0e9))
    res_stop_hz = float(kw.get("res_stop_hz", 3.0e9))
    coarse_step_hz = float(kw.get("coarse_step_hz", 6.7e6))
    fine_span_hz = float(kw.get("fine_span_hz", 20e6))
    fine_step_hz = float(kw.get("fine_step_hz", 50e3))
    res_power_dbm = float(kw.get("res_power_dbm", -40.0))
    fwhm_margin = float(kw.get("fwhm_margin", 1.0))
    flat_step_hz = float(kw.get("flat_step_hz", 50e3))
    target_forward_dbm = float(kw.get("target_forward_dbm", 0.0))
    max_power_dbm = float(kw.get("max_power_dbm", 10.0))
    coupling_db = float(kw.get("coupling_db", 20.0))
    interlock_margin_db = float(kw.get("interlock_margin_db", 10.0))
    interlock_ceiling_margin_db = float(kw.get("interlock_ceiling_margin_db", 10.0))

    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"[flat_power_scan] loading power lookup table '{lookup_name}'")
    lookup_freqs_hz, lookup_gain_db, ref_power_dbm = load_power_lookup(lookup_name)
    print(f"[flat_power_scan] lookup covers {lookup_freqs_hz.min()/1e9:.4f}-"
          f"{lookup_freqs_hz.max()/1e9:.4f} GHz, measured at {ref_power_dbm} dBm reference")

    print("[flat_power_scan] connecting to HP8673H + E4403B")
    gen = HP8673H(GEN_RESOURCE)
    sa = E4403B(SA_RESOURCE)
    try:
        print(f"[flat_power_scan] step 1/2: normal resonance sweep "
              f"({res_start_hz/1e9:.4f}-{res_stop_hz/1e9:.4f} GHz, {res_power_dbm} dBm) "
              f"-- coupler REFLECTED port -> analyzer")
        result = gen.resonance_sweep(
            sa, res_start_hz, res_stop_hz, coarse_step_hz, fine_span_hz, fine_step_hz,
            res_power_dbm, output_prefix=f"{DATA_DIR}/{name}_resonance",
        )
        f0_hz = result["f0_hz"]
        fwhm_hz = result["fwhm_hz"]
        print(f"[flat_power_scan] step 1/2 done: f0 = {f0_hz/1e9:.5f} GHz, "
              f"FWHM = {fwhm_hz/1e6:.3f} MHz, Q ~= {result['Q']:.0f}")

        half_span_hz = fwhm_hz / 2 * fwhm_margin
        scan_start_hz = f0_hz - half_span_hz
        scan_stop_hz = f0_hz + half_span_hz
        if scan_start_hz < lookup_freqs_hz.min() or scan_stop_hz > lookup_freqs_hz.max():
            print(f"[flat_power_scan] WARNING: scan range "
                  f"{scan_start_hz/1e9:.4f}-{scan_stop_hz/1e9:.4f} GHz extends beyond "
                  f"the lookup table's {lookup_freqs_hz.min()/1e9:.4f}-"
                  f"{lookup_freqs_hz.max()/1e9:.4f} GHz coverage -- extrapolated gain "
                  f"values there will be unreliable")

        flat_freqs_hz = np.arange(scan_start_hz, scan_stop_hz + flat_step_hz / 2, flat_step_hz)
        flat_powers_dbm = required_power_dbm(
            lookup_freqs_hz, lookup_gain_db, flat_freqs_hz, target_forward_dbm,
            max_power_dbm=max_power_dbm,
        )
        thresholds_dbm = compute_interlock_thresholds_dbm(
            flat_freqs_hz, flat_powers_dbm, result["fine_freqs_hz"], result["fine_power_dbm"],
            res_power_dbm, target_forward_dbm, coupling_db=coupling_db,
            margin_db=interlock_margin_db, ceiling_margin_db=interlock_ceiling_margin_db,
        )

        print(f"[flat_power_scan] step 2/2: flat-power scan over "
              f"{scan_start_hz/1e9:.5f}-{scan_stop_hz/1e9:.5f} GHz (FWHM x {fwhm_margin}), "
              f"targeting {target_forward_dbm} dBm forward power -- generator power will "
              f"vary {flat_powers_dbm.min():.2f} to {flat_powers_dbm.max():.2f} dBm across "
              f"the scan, interlock threshold {thresholds_dbm.min():.2f} to "
              f"{thresholds_dbm.max():.2f} dBm")
        sa.set_center_span((scan_start_hz + scan_stop_hz) / 2, scan_stop_hz - scan_start_hz)
        used_freqs_hz, used_powers_dbm, reflected_dbm, tripped = flat_power_sweep(
            gen, sa, flat_freqs_hz, flat_powers_dbm, thresholds_dbm=thresholds_dbm,
        )

        flat_path = f"{DATA_DIR}/{name}_flat.csv"
        np.savetxt(
            flat_path,
            np.column_stack((used_freqs_hz, used_powers_dbm, reflected_dbm)),
            delimiter=",", header="frequency_hz,generator_power_dbm,reflected_dbm",
            comments="",
        )
        if tripped:
            print(f"[flat_power_scan] step 2/2: INTERLOCK TRIPPED -- only "
                  f"{len(used_freqs_hz)} of {len(flat_freqs_hz)} points completed")
        print(f"[flat_power_scan] step 2/2 done: saved {flat_path}"
              f"{' (PARTIAL)' if tripped else ''}")
    finally:
        gen.rf_off()
        gen.go_to_local()
        gen.close()
        sa.go_to_local()
        sa.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]

    if command == "lookup":
        if len(sys.argv) < 3:
            raise SystemExit("usage: python flat_power_scan.py lookup <name> [key=value ...]")
        name = sys.argv[2]
        extra = parse_kv_args(sys.argv[3:])
        build_power_lookup(name, **extra)
    elif command == "scan":
        if len(sys.argv) < 4:
            raise SystemExit(
                "usage: python flat_power_scan.py scan <lookup_name> <name> [key=value ...]"
            )
        lookup_name = sys.argv[2]
        name = sys.argv[3]
        extra = parse_kv_args(sys.argv[4:])
        run_scan(lookup_name, name, **extra)
    else:
        raise SystemExit(f"unknown command {command!r} (expected 'lookup' or 'scan')")


if __name__ == "__main__":
    main()
