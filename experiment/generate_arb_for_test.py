import numpy as np
import generate_arb

def normalize(waveform):
    max_abs = np.max(np.abs(waveform))
    if max_abs > 0:
        waveform /= max_abs
    return waveform


def generate_waveforms_for_freq_test(
    n_segments=1000,
    fs=50e6,
    seed=42,
    f_min=1e6,
    f_max=50e6,
    half_cycle=1e-3
):
    rng = np.random.default_rng(seed)
    dt = 1 / fs
    period = int(half_cycle * fs)
    freqs = rng.integers(f_min, f_max + 1, size=n_segments)

    sine_wave = []
    square_wave = []

    for f in freqs:
        t = np.arange(period) * dt

        sine = np.sin(2 * np.pi * f * t)
        zeros = np.zeros(period)

        sine_wave.append(sine)
        sine_wave.append(zeros)

    sine_wave = np.concatenate(sine_wave)
    # print(sine_wave)

    one = np.ones(period)
    zero = np.zeros(period)

    for _ in range(n_segments):
        square_wave.append(one)
        square_wave.append(zero)

    square_wave = np.concatenate(square_wave)

    print(freqs / 1e6)
    
    t = np.arange(period * 2 * n_segments) * dt
    return t, normalize(sine_wave), square_wave, freqs

def generate_waveforms_for_voltage_test(
    period_ns,
    duration_ns,
    sample_rate_hz,
    ramp_duration_ns=300,
    trigger_width_ns=20,
):
    dt_ns = 1e9 / sample_rate_hz
    t_ns = np.arange(0, duration_ns, dt_ns)
    phase_ns = np.mod(t_ns, period_ns)
    ch1 = np.zeros_like(t_ns)
    active = phase_ns < ramp_duration_ns
    ch1[active] = -1 + 2 * phase_ns[active] / ramp_duration_ns
    ch2 = (phase_ns < trigger_width_ns).astype(float)

    return t_ns, ch1, ch2


if __name__ == "__main__":
    # t, ch1, ch2, freqs = generate_waveforms_for_freq_test(n_segments=10, fs=500e6, half_cycle=10e-6)
    # generate_arb.write_csv("waveforms/test_scope_100.csv", t, ch1, ch2)
    pass