import numpy as np
import generate_arb

def generate_waveforms(
    n_segments=1000,
    fs=50e6,
    seed=42,
    f_min=1e6,
    f_max=50e6,
    half_cycle=1e-3
):
    rng = np.random.default_rng(seed)

    dt = 1 / fs
    period = int(half_cycle * fs)  # samples per 1 µs

    # random integer frequencies (Hz)
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
    print(sine_wave)

    one = np.ones(period)
    zero = np.zeros(period)

    for _ in range(n_segments):
        square_wave.append(one)
        square_wave.append(zero)

    square_wave = np.concatenate(square_wave)

    print(freqs / 1e6)
    
    t = np.arange(period * 2 * n_segments) * dt
    return t, sine_wave, square_wave, freqs


if __name__ == "__main__":
    # t, ch1, ch2, freqs = generate_waveforms(n_segments=10, fs=100e6)
    t, ch1, ch2, freqs = generate_waveforms(n_segments=10, fs=100e6)

    generate_arb.write_csv("waveforms/test_scope_100.csv", t, ch1, ch2)