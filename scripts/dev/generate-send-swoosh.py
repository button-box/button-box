#!/usr/bin/env python3
"""Render a three-second paper-plane send cue with no sampled audio."""
import math
import random
import struct
import wave
from pathlib import Path


def bump(t, start, length):
    progress = (t - start) / length
    return math.sin(math.pi * progress) ** 1.2 if 0 < progress < 1 else 0.0


def bell(t, start, pitch):
    age = t - start
    if not 0 <= age < 0.65:
        return 0.0
    envelope = min(age / 0.014, 1) * math.exp(-age * 5)
    envelope *= min((0.65 - age) / 0.1, 1)
    return envelope * math.sin(2 * math.pi * pitch * age)


def render(path):
    rate = 48000
    duration = 3.0
    random_source = random.Random(190)
    samples = []
    fast = slow = phase = 0.0
    for index in range(int(rate * duration)):
        t = index / rate
        progress = min(t / 2.5, 1.0)
        noise = random_source.uniform(-1, 1)
        # Limit the air's bass and harsh upper band for a small USB speaker.
        cutoff = 700 + 1600 * math.sin(math.pi * progress / 2) ** 2
        fast += (1 - math.exp(-2 * math.pi * cutoff / rate)) * (noise - fast)
        slow += (1 - math.exp(-2 * math.pi * 280 / rate)) * (noise - slow)
        envelope = 0.25 * bump(t, 0, 0.55) + bump(t, 0.15, 2.65)
        phase += 2 * math.pi * (410 + 1220 * progress ** 2.3) / rate
        body = 0.085 * math.sin(phase) * bump(t, 0.2, 2.5)
        finish = (0.115 * bell(t, 2.02, 1046.5)
                  + 0.12 * bell(t, 2.2, 1318.5)
                  + 0.09 * bell(t, 2.35, 1568))
        fade = min(t / 0.025, 1) * max(0, min((duration - t - 1 / rate) / 0.06, 1))
        samples.append(((fast - slow) * envelope * 0.9 + body + finish) * fade)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    gain = min(0.14 / rms, 0.50 / max(abs(value) for value in samples))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", round(value * gain * 32767)) for value in samples))


if __name__ == "__main__":
    render(Path(__file__).resolve().parents[2] / "sounds/feedback/sent-swoosh.wav")
