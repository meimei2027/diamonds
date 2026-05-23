import numpy as np

def generate_modulation(t, edges):
    y = np.zeros_like(t)
    for on, off in zip(edges[0::2], edges[1::2]):
        y[(t >= on) & (t < off)] = 1
    return t, y

def generate_waveform(t, freq, modulation):
    carrier = np.sin(2 * np.pi * freq * t)
    return carrier * modulation

def normalize(waveform):
    max_abs = np.max(np.abs(waveform))
    if max_abs > 0:
        waveform /= max_abs
    return waveform

def write_csv(filename, t, ch1, ch2):
    data = np.column_stack((t, ch1, ch2))
    np.savetxt(
        filename,
        data,
        delimiter=",",
        header="t_seconds,ch1,ch2",
        comments=""
    )
    print("generated " + filename)

def generate(output_file, carrier_freq, edges, duration):
    MAX_SAMPLE_RATE = 1e9
    num_samples = int(MAX_SAMPLE_RATE * duration)
    t = np.arange(num_samples) / MAX_SAMPLE_RATE
    _, mod = generate_modulation(t, edges)
    waveform1 = normalize(generate_waveform(t, carrier_freq, mod))
    waveform2 = np.sin(2 * np.pi * 1e6 * t)

    write_csv(output_file, t, waveform1, waveform2)

if __name__ == "__main__":
    generate("waveforms/test.csv", 77e6, [0e-6, 4e-6], 10e-6)
