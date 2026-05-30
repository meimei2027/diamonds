import numpy as np

def generate_modulation(t, edges):
    y = np.zeros_like(t)
    for on, off in zip(edges[0::2], edges[1::2]):
        y[(t >= on) & (t < off)] = 1
    return y

def generate_modulated_sine_wave(t, freq, modulation):
    carrier = np.sin(2 * np.pi * freq * t)
    return carrier * modulation

def normalize(waveform):
    max_abs = np.max(np.abs(waveform))
    if max_abs > 0:
        waveform /= max_abs
    return waveform

def write_csv(filename, t, ch1, ch2=None):
    if ch2 is not None:
        data = np.column_stack((t, ch1, ch2))
        header = "t_seconds,normalized_ch1,normalized_ch2"
    else: 
        data = np.column_stack((t, ch1))
        header = "t_seconds,normalized_ch1"
    np.savetxt(
        filename,
        data,
        delimiter=",",
        header=header,
        comments=""
    )
    print("generated " + filename)

def generate_ks33600a(output_file, carrier_freq_ch1, edges_ch1, edges_ch2, duration, sample_rate=1e9):
    # MAX_SAMPLE_RATE = 1e9
    num_samples = int(sample_rate * duration)
    t = np.arange(num_samples) / sample_rate
    mod1 = generate_modulation(t, edges_ch1)
    waveform1 = normalize(generate_modulated_sine_wave(t, carrier_freq_ch1, mod1))
    mod2 = generate_modulation(t, edges_ch2)
    waveform2 = normalize(mod2)
    write_csv(output_file, t, waveform1, waveform2)

def generate_sdg1062x(output_file, edges_ch, duration, sample_rate=1e9):
    num_samples = int(sample_rate * duration)
    t = np.arange(num_samples) / sample_rate
    mod = generate_modulation(t, edges_ch)
    waveform = normalize(mod)
    write_csv(output_file, t, waveform)

if __name__ == "__main__":
    EXCITATION = 5e-6
    PI = 60e-9
    PI_2 = PI / 2
    READOUT = 300e-9
    AOM_DELAY = 1e-6
    TAU = 60e-9
    MICROWAVE = PI + TAU + PI_2 + TAU + PI
    SINGLE_CYCLE = AOM_DELAY + EXCITATION + MICROWAVE + AOM_DELAY + READOUT
    KS33600A_MAX_SAMPLE_RATE = 1e9
    SDG1062X_MAX_SAMPLE_RATE = 30e6

    # generate_ks33600a("waveforms/test.csv", 44e6, \
    # [0, 100e-9, 300e-9, 1e-6
    # ], [0, 100e-9, 300e-9, 1e-6
    # ], 1e-6, sample_rate=KS33600A_MAX_SAMPLE_RATE)

    generate_ks33600a("waveforms/test.csv", 77e6, \
    [0, \
        EXCITATION, \
        AOM_DELAY + EXCITATION + MICROWAVE, \
        AOM_DELAY + EXCITATION + MICROWAVE + READOUT, \
        SINGLE_CYCLE, \
        SINGLE_CYCLE + EXCITATION, \
        SINGLE_CYCLE + AOM_DELAY + EXCITATION + MICROWAVE, \
        SINGLE_CYCLE + AOM_DELAY + EXCITATION + MICROWAVE + READOUT, \
    ], [0, 1e-6], SINGLE_CYCLE * 2, sample_rate=KS33600A_MAX_SAMPLE_RATE)


