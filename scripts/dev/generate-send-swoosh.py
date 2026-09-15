#!/usr/bin/env python3
"""Render the original Button Box send cue using only Python's standard library."""
import math
import random
import struct
import wave
from pathlib import Path


def render(path):
    rate = 48000
    duration = 1.25
    random_source = random.Random(7)
    samples = []
    low = 0.0
    phase = 0.0
    for index in range(int(rate * duration)):
        t = index / rate
        progress = t / duration
        envelope = math.sin(math.pi * min(t / 0.9, 1.0)) ** 1.8 if t < 0.9 else 0.0
        noise = random_source.uniform(-1, 1)
        low += 0.12 * (noise - low)
        phase += 2 * math.pi * (400 + 2400 * min(t / 0.9, 1.0) ** 1.5) / rate
        airy = low * 0.65 + math.sin(phase) * 0.13
        # A buoyant rising tail makes this distinct from a press or card beep.
        sparkle = 0.0
        for start, pitch in ((0.65, 1046.5), (0.80, 1318.5), (0.95, 1568.0)):
            age = t - start
            if 0 <= age < 0.30:
                shape = min(age / 0.012, 1.0) * math.exp(-age * 13) * min((0.30 - age) / 0.04, 1.0)
                sparkle += 0.17 * shape * math.sin(2 * math.pi * pitch * age)
        samples.append(airy * envelope + sparkle)
    gain = 0.50 / max(abs(value) for value in samples)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", round(value * gain * 32767)) for value in samples))


if __name__ == "__main__":
    render(Path(__file__).resolve().parents[2] / "sounds/feedback/sent-swoosh.wav")
