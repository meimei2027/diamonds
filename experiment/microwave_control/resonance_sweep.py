"""
Coarse-then-fine resonance sweep.

Locates a resonance dip in reflected power by first doing a coarse sweep
across a wide range to find the approximate location, then a fine sweep
zoomed in around that dip for a precise frequency/depth/Q measurement.

See notes.md for the settling-time and calibration gotchas this script's
defaults account for -- in particular, the coarse sweep step size should be
no coarser than about FWHM/5 for the resonance you expect, or you risk
stepping right over a narrow dip entirely.
"""
import argparse
import numpy as np

from hp8673h import HP8673H
from e4403b import E4403B
from frequency_sweep import frequency_sweep


def coarse_sweep(gen, sa, start_hz, stop_hz, step_hz, power_dbm):
    """Wide sweep to locate the approximate resonance frequency."""
    sa.set_center_span((start_hz + stop_hz) / 2, stop_hz - start_hz)
    return frequency_sweep(
        gen, sa, start_hz=start_hz, stop_hz=stop_hz, step_hz=step_hz,
        power_dbm=power_dbm,
    )


def fine_sweep(gen, sa, center_hz, span_hz, step_hz, power_dbm):
    """Narrow, dense sweep centered on a coarse dip for precise measurement."""
    start_hz = center_hz - span_hz / 2
    stop_hz = center_hz + span_hz / 2
    sa.set_center_span(center_hz, span_hz)
    return frequency_sweep(
        gen, sa, start_hz=start_hz, stop_hz=stop_hz, step_hz=step_hz,
        power_dbm=power_dbm, settle_s=0.15,
    )


def find_dip(freqs_hz, power_dbm):
    idx = np.argmin(power_dbm)
    return freqs_hz[idx], power_dbm[idx]


def estimate_q(freqs_hz, power_dbm, smooth_window=9):
    """
    Loaded-Q estimate from a reflection notch: half-power points found in
    linear power (not simply "3 dB above the dip" in dB), FWHM = width
    between them, Q = f0 / FWHM. A light moving average smooths
    point-to-point jitter at the bottom of the notch before finding the
    crossings.
    """
    kernel = np.ones(smooth_window) / smooth_window
    smoothed = np.convolve(power_dbm, kernel, mode='same')

    dip_idx = np.argmin(smoothed)
    f0 = freqs_hz[dip_idx]
    dip_dbm = smoothed[dip_idx]

    edge_n = max(1, len(freqs_hz) // 10)
    baseline_dbm = np.median(np.concatenate([smoothed[:edge_n], smoothed[-edge_n:]]))

    baseline_lin = 10 ** (baseline_dbm / 10)
    dip_lin = 10 ** (dip_dbm / 10)
    half_lin = (baseline_lin + dip_lin) / 2
    half_dbm = 10 * np.log10(half_lin)

    left = dip_idx
    while left > 0 and smoothed[left] < half_dbm:
        left -= 1
    right = dip_idx
    while right < len(smoothed) - 1 and smoothed[right] < half_dbm:
        right += 1

    fwhm_hz = freqs_hz[right] - freqs_hz[left]
    q = f0 / fwhm_hz if fwhm_hz > 0 else float("inf")

    return {"f0_hz": f0, "dip_dbm": dip_dbm, "baseline_dbm": baseline_dbm,
            "fwhm_hz": fwhm_hz, "Q": q}


def run(gen_resource, sa_resource, start_hz, stop_hz, coarse_step_hz,
        fine_span_hz, fine_step_hz, power_dbm, output_prefix):
    gen = HP8673H(gen_resource)
    sa = E4403B(sa_resource)

    print(f"[resonance_sweep] coarse sweep {start_hz/1e9:.4f}-{stop_hz/1e9:.4f} GHz, "
          f"step {coarse_step_hz/1e6:.2f} MHz, {power_dbm} dBm")
    coarse_freqs_hz, coarse_power_dbm = coarse_sweep(
        gen, sa, start_hz, stop_hz, coarse_step_hz, power_dbm)
    np.savetxt(
        f"{output_prefix}_coarse.csv",
        np.column_stack((coarse_freqs_hz, coarse_power_dbm)),
        delimiter=",", header="frequency_hz,power_dbm", comments="",
    )

    dip_freq_hz, dip_power_dbm = find_dip(coarse_freqs_hz, coarse_power_dbm)
    print(f"[resonance_sweep] coarse dip: {dip_freq_hz/1e9:.4f} GHz, {dip_power_dbm:.1f} dBm")

    print(f"[resonance_sweep] fine sweep around {dip_freq_hz/1e9:.4f} GHz, "
          f"span {fine_span_hz/1e6:.2f} MHz, step {fine_step_hz/1e3:.1f} kHz")
    fine_freqs_hz, fine_power_dbm = fine_sweep(
        gen, sa, dip_freq_hz, fine_span_hz, fine_step_hz, power_dbm)
    np.savetxt(
        f"{output_prefix}_fine.csv",
        np.column_stack((fine_freqs_hz, fine_power_dbm)),
        delimiter=",", header="frequency_hz,power_dbm", comments="",
    )

    gen.rf_off()
    gen.go_to_local()
    gen.close()
    sa.go_to_local()
    sa.close()

    result = estimate_q(fine_freqs_hz, fine_power_dbm)
    result["coarse_freqs_hz"] = coarse_freqs_hz
    result["coarse_power_dbm"] = coarse_power_dbm
    result["fine_freqs_hz"] = fine_freqs_hz
    result["fine_power_dbm"] = fine_power_dbm

    depth_db = result["baseline_dbm"] - result["dip_dbm"]
    print(f"[resonance_sweep] f0 = {result['f0_hz']/1e9:.5f} GHz, "
          f"depth = {depth_db:.1f} dB, FWHM = {result['fwhm_hz']/1e6:.2f} MHz, "
          f"Q ~= {result['Q']:.0f}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gen-resource", default="GPIB1::19::INSTR")
    parser.add_argument("--sa-resource", default="GPIB0::18::INSTR")
    parser.add_argument("--start-hz", type=float, default=2.0e9)
    parser.add_argument("--stop-hz", type=float, default=3.0e9)
    parser.add_argument("--coarse-step-hz", type=float, default=6.7e6,
                         help="default matches FWHM/5 for a Q~75 resonance -- "
                              "tune to your expected Q (see notes.md)")
    parser.add_argument("--fine-span-hz", type=float, default=20e6)
    parser.add_argument("--fine-step-hz", type=float, default=50e3)
    parser.add_argument("--power-dbm", type=float, default=0.0)
    parser.add_argument("--output-prefix", default="data/resonance_sweep")
    args = parser.parse_args()

    run(args.gen_resource, args.sa_resource, args.start_hz, args.stop_hz,
        args.coarse_step_hz, args.fine_span_hz, args.fine_step_hz,
        args.power_dbm, args.output_prefix)


if __name__ == "__main__":
    main()
