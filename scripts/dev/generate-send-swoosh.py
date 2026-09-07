#!/usr/bin/env python3
"""Render the original Button Box send cue using only Python's standard library."""
import math
import random
import struct
import wave
from pathlib import Path


def render(path):
    rate = 48000
    duration = 0.48
    random_source = random.Random(7)
    samples = []
    low = 0.0
    phase = 0.0
    for index in range(int(rate * duration)):
        t = index / rate
        progress = t / duration
        envelope = math.sin(math.pi * progress) ** 1.8
        noise = random_source.uniform(-1, 1)
        low += 0.12 * (noise - low)
        phase += 2 * math.pi * (500 + 2100 * progress ** 1.5) / rate
        airy = low * 0.75 + math.sin(phase) * 0.06
        samples.append(airy * envelope)
    gain = 0.35 / max(abs(value) for value in samples)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", round(value * gain * 32767)) for value in samples))


if __name__ == "__main__":
    render(Path(__file__).resolve().parents[2] / "sounds/feedback/sent-swoosh.wav")
