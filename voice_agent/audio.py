"""Audio format conversion between Gemini and the phone line."""

import numpy as np


class Downsampler:
    """24 kHz PCM16 (Gemini output) -> 8 kHz PCM16 (what FreJun plays).

    Low-pass filters before dropping samples so speech doesn't alias, and keeps
    filter state across chunks so there are no clicks at chunk boundaries.
    """

    FACTOR = 3
    TAPS = 63

    def __init__(self):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        cutoff = 3400 / 24000  # just under the 4 kHz Nyquist of 8 kHz audio
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(self.TAPS)
        self.kernel = h / h.sum()
        self.reset()

    def reset(self):
        self.history = np.zeros(self.TAPS - 1)
        self.phase = 0

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        if not len(x):
            return b""
        buf = np.concatenate([self.history, x])
        y = np.convolve(buf, self.kernel, mode="valid")
        out = y[self.phase :: self.FACTOR]
        self.phase = (self.phase - len(y)) % self.FACTOR
        self.history = buf[-(self.TAPS - 1) :]
        return np.clip(np.round(out), -32768, 32767).astype(np.int16).tobytes()
