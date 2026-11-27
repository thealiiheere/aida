"""Converting between the phone's audio and 16-bit PCM.

Twilio carries 8 kHz G.711 mu-law; VoiceLab speaks 24 kHz and listens to 16 kHz
16-bit mono PCM. Python's audioop is gone from 3.13, so the conversions are done here.
"""

import io
import math
import sys
import wave
from array import array

PHONE_RATE = 8000

# --- mu-law -------------------------------------------------------------------

_BIAS = 0x84
_CLIP = 32635


def _decode(code: int) -> int:
    code = ~code & 0xFF
    sign, exponent, mantissa = code & 0x80, (code >> 4) & 0x07, code & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def _encode(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), _CLIP) + _BIAS
    exponent = max(sample.bit_length() - 8, 0)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_DECODED = [_decode(code) for code in range(256)]
# Indexed by sample + 32768.
_ENCODED = bytes(_encode(sample) for sample in range(-32768, 32768))


def mulaw_to_pcm(audio: bytes) -> array:
    return array("h", (_DECODED[code] for code in audio))


def pcm_to_mulaw(samples) -> bytes:
    return bytes(_ENCODED[sample + 32768] for sample in samples)


# --- Sample rates -------------------------------------------------------------


def _halfband() -> list[float]:
    """The odd taps of a 15-tap half-band low-pass (Hamming-windowed sinc).

    A half-band filter's even taps are zero apart from the centre one, so halving the
    rate only needs these four multiplications per sample.
    """
    taps = [
        math.sin(math.pi * k / 2) / (math.pi * k) * (0.54 + 0.46 * math.cos(math.pi * k / 7))
        for k in (1, 3, 5, 7)
    ]
    scale = 0.25 / sum(taps)  # each side adds up to 0.25: unity gain at 0 Hz
    return [t * scale for t in taps]


_TAPS = _halfband()


def halve_rate(samples: array) -> array:
    """16 kHz to 8 kHz, filtering out what 8 kHz can't carry so it doesn't alias."""
    padded = [0] * 7 + list(samples) + [0] * 7
    h1, h3, h5, h7 = _TAPS
    out = array("h")
    for i in range(7, len(padded) - 7, 2):
        value = 0.5 * padded[i] + (
            h1 * (padded[i - 1] + padded[i + 1])
            + h3 * (padded[i - 3] + padded[i + 3])
            + h5 * (padded[i - 5] + padded[i + 5])
            + h7 * (padded[i - 7] + padded[i + 7])
        )
        out.append(max(-32768, min(32767, round(value))))
    return out


def double_rate(samples: array) -> array:
    """8 kHz to 16 kHz: the original samples, with the midpoint between each pair."""
    out = array("h")
    for i, sample in enumerate(samples):
        after = samples[i + 1] if i + 1 < len(samples) else sample
        out.append(sample)
        out.append((sample + after) // 2)
    return out


def resample(samples: array, rate: int, to: int) -> array:
    """Any other rate, by linear interpolation. Fine for speech, not for music."""
    if rate == to or not samples:
        return samples
    count = max(1, round(len(samples) * to / rate))
    step = (len(samples) - 1) / max(count - 1, 1)
    out = array("h")
    for i in range(count):
        position = i * step
        left = int(position)
        right = min(left + 1, len(samples) - 1)
        fraction = position - left
        out.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
    return out


def _lowpass(count: int, cutoff: float) -> list[float]:
    """A Hamming-windowed sinc low-pass; `cutoff` is a fraction of the sample rate."""
    middle = (count - 1) / 2
    taps = []
    for n in range(count):
        x = n - middle
        sinc = 2 * cutoff if x == 0 else math.sin(2 * math.pi * cutoff * x) / (math.pi * x)
        taps.append(sinc * (0.54 - 0.46 * math.cos(2 * math.pi * n / (count - 1))))
    total = sum(taps)
    return [t / total for t in taps]


class PhoneDownsampler:
    """Streamed 16-bit PCM, at a multiple of 8 kHz, as 8 kHz mu-law.

    Chunks may split a sample or the filter's window; what is left over is kept for
    the next chunk, so the joins can't be heard.
    """

    def __init__(self, rate: int):
        if rate % PHONE_RATE:
            raise ValueError(f"can't bring {rate} Hz down to {PHONE_RATE} Hz")
        self._factor = rate // PHONE_RATE
        # Everything above 3.6 kHz would alias once there are only 8000 samples a second.
        self._taps = _lowpass(31, 3600 / rate) if self._factor > 1 else [1.0]
        self._history = [0] * (len(self._taps) - 1)
        self._phase = 0
        self._odd = b""

    def feed(self, pcm: bytes) -> bytes:
        data = self._odd + pcm
        cut = len(data) - len(data) % 2
        data, self._odd = data[:cut], data[cut:]
        samples = array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        window = self._history + samples.tolist()
        taps, reach = self._taps, len(self._taps) - 1
        out = []
        i = self._phase + reach
        while i < len(window):
            value = sum(t * window[i - k] for k, t in enumerate(taps))
            out.append(max(-32768, min(32767, round(value))))
            i += self._factor
        self._phase = i - len(window)
        self._history = window[len(window) - reach :] if reach else []
        return pcm_to_mulaw(out)


def level(audio: bytes) -> float:
    """How loud a stretch of 8 kHz mu-law is: the root mean square of its samples."""
    if not audio:
        return 0.0
    return math.sqrt(sum(_DECODED[code] ** 2 for code in audio) / len(audio))


# --- What VoiceLab sends and expects -----------------------------------------


def wav_to_phone(data: bytes) -> bytes:
    """A WAV file as 8 kHz mu-law."""
    with wave.open(io.BytesIO(data)) as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit audio, got {wav.getsampwidth() * 8}-bit")
        channels, rate = wav.getnchannels(), wav.getframerate()
        samples = array("h", wav.readframes(wav.getnframes()))
    if channels > 1:
        samples = samples[::channels]  # the first channel is plenty for a phone
    if rate == 2 * PHONE_RATE:
        samples = halve_rate(samples)
    else:
        samples = resample(samples, rate, PHONE_RATE)
    return pcm_to_mulaw(samples)


def phone_to_pcm16k(audio: bytes) -> bytes:
    """8 kHz mu-law as 16 kHz little-endian 16-bit PCM."""
    samples = double_rate(mulaw_to_pcm(audio))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()
