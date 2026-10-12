"""Audio format conversion between Gemini and the phone line."""

import numpy as np


def phone_voice_kernel(taps: int = 161, rate: int = 24000) -> np.ndarray:
    """FIR filter for 24 kHz speech going down to an 8 kHz phone line (linear phase, frequency-sampled).

    - flat up to ~3.2 kHz, then a sharp cut: everything above 4 kHz (which would fold back as hiss
      after dropping to 8 kHz) is at least 75 dB down
    - +3 dB "presence" at 2-3.2 kHz: the consonant range a phone line keeps, so speech sounds clear
      instead of muffled
    - -2 dB below 200 Hz: less boom on small phone speakers
    """
    points = [(0, -2), (200, -2), (400, 0), (1200, 0), (2000, 3), (3250, 3), (3800, -90), (rate / 2, -90)]
    freqs = np.linspace(0, rate / 2, 8193)
    gain = 10 ** (np.interp(freqs, [p[0] for p in points], [p[1] for p in points]) / 20)
    impulse = np.fft.fftshift(np.fft.irfft(gain))
    mid = len(gain) - 1
    h = impulse[mid - taps // 2: mid + taps // 2 + 1] * np.blackman(taps)
    # unity gain at 800 Hz, the middle of the voice band
    response = np.abs(np.fft.rfft(h, rate))[800]
    return h / response


class Downsampler:
    """24 kHz PCM16 (Gemini output) -> 8 kHz PCM16 (what FreJun plays).

    Filters before dropping samples so speech doesn't alias (and gets a phone-voice EQ, see
    phone_voice_kernel), and keeps filter state across chunks so there are no clicks at chunk boundaries.
    """

    FACTOR = 3
    KERNEL = phone_voice_kernel()
    TAPS = len(KERNEL)

    def __init__(self):
        self.kernel = self.KERNEL
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
