"""Change mono PCM16 tempo with waveform-similarity overlap-add."""


def stretch_pcm(pcm, sample_rate, speed):
    if speed == 1.0:
        return pcm

    import numpy as np

    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    size = max(1, round(len(samples) / speed))
    grain = min(30 * sample_rate // 1000, len(samples), size)
    if grain <= 2:
        return np.resize(samples, size).astype('<i2').tobytes()
    overlap = min(10 * sample_rate // 1000, grain // 2)
    hop = grain - overlap
    search = 8 * sample_rate // 1000
    fade = np.linspace(0., 1., overlap, dtype=np.float32)
    output = np.empty(size + grain, dtype=np.float32)
    output[:grain] = samples[:grain]
    for position in range(hop, size - overlap, hop):
        # Shift waveform grains within a small search window, then crossfade
        # matching phases. Samples within each grain retain their original rate.
        reference = output[position:position + overlap]
        expected = min(round(position * speed), len(samples) - grain)
        low = max(0, expected - search)
        high = min(expected + search, len(samples) - grain)
        window = samples[low:high + overlap]
        squared = np.cumsum(np.r_[0., window.astype(np.float64) ** 2])
        energy = squared[overlap:] - squared[:-overlap]
        norm = (np.dot(reference, reference) * energy) ** 0.5
        if np.any(norm > 0):
            scores = np.correlate(window, reference)
            scores = np.divide(scores, norm, out=np.zeros_like(norm), where=norm > 0)
            scores -= 0.002 * np.abs(np.arange(low, high + 1) - expected) / search
            chosen = low + int(np.argmax(scores))
        else:
            chosen = expected
        output[position:position + overlap] = reference * (1 - fade) + samples[chosen:chosen + overlap] * fade
        output[position + overlap:position + grain] = samples[chosen + overlap:chosen + grain]
    if size > grain:
        tail = size - grain
        output[tail:tail + overlap] = output[tail:tail + overlap] * (1 - fade) + samples[-grain:-grain + overlap] * fade
        output[tail + overlap:size] = samples[-(grain - overlap):]
    return np.rint(output[:size]).astype('<i2').tobytes()
