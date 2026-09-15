#!/usr/bin/env python3
"""Check a bounded local 16-bit mono room capture; output measurements only.

This does not trigger sounds, record microphones or prove message delivery.
Keep the first three seconds quiet, trigger exactly one cue, then stop capture.
"""
import argparse
import json
import math
import struct
import wave


def measure(path, *, frequency, minimum, maximum, baseline=3.0):
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Use 16-bit mono PCM")
        rate = source.getframerate()
        if not 0 < frequency < rate / 2:
            raise ValueError("Frequency must be below Nyquist")
        if source.getnframes() > rate * 60:
            raise ValueError("Capture must be bounded to 60 seconds")
        raw = source.readframes(source.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    width = int(rate * 0.02)
    frames = [samples[i:i + width] for i in range(0, len(samples) - width + 1, width)]
    base_count = math.ceil(baseline / 0.02)
    if base_count < 150 or len(frames) <= base_count:
        raise ValueError("Capture needs three seconds of baseline and a cue window")
    powers = [sum(x * x for x in frame) / width for frame in frames]
    baseline_power = max(sum(powers[:base_count]) / base_count, 1.0)
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)

    def tone_fraction(frame, power):
        a = b = 0.0
        for sample in frame:
            current = sample + coefficient * a - b
            b, a = a, current
        tone_power = max(0.0, a * a + b * b - coefficient * a * b)
        return min(1.0, 2 * tone_power / max(1.0, width * width * power))

    fractions = [tone_fraction(frame, power) for frame, power in zip(frames, powers)]
    matches = [p >= baseline_power * 10 and f >= 0.45 for p, f in zip(powers, fractions)]
    runs = []
    start = None
    for index in range(base_count, len(matches) + 1):
        matched = index < len(matches) and matches[index]
        if matched and start is None:
            start = index
        if not matched and start is not None:
            runs.append((start, index))
            start = None
    candidates = [(a, b) for a, b in runs if (b - a) * 0.02 >= minimum]
    best = max(runs, key=lambda r: r[1] - r[0], default=(base_count, base_count))
    a, b = best
    duration = (b - a) * 0.02
    cue_power = sum(powers[a:b]) / max(1, b - a)
    clipped = sum(abs(x) >= 32760 for frame in frames[a:b] for x in frame) / max(1, (b - a) * width)
    baseline_tone = any(f >= 0.45 and p > 100 for f, p in zip(fractions[:base_count], powers[:base_count]))
    passed = len(candidates) == 1 and minimum <= duration <= maximum and clipped < 0.01 and not baseline_tone
    return {"passed": passed, "frequency_hz": frequency, "onset_s": round(a * 0.02, 3) if b > a else None,
            "duration_s": round(duration, 3), "above_baseline_db": round(10 * math.log10(max(cue_power, 1) / baseline_power), 2),
            "clipped_fraction": round(clipped, 5), "baseline_contains_tone": baseline_tone,
            "matching_cue_count": len(candidates)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture")
    parser.add_argument("--frequency", type=float, required=True)
    parser.add_argument("--minimum", type=float, required=True)
    parser.add_argument("--maximum", type=float, required=True)
    args = parser.parse_args()
    if not 0 < args.minimum <= args.maximum <= 10:
        parser.error("Duration range must be positive, ordered and at most 10 seconds")
    try:
        result = measure(args.capture, frequency=args.frequency, minimum=args.minimum, maximum=args.maximum)
    except (OSError, ValueError, wave.Error):
        parser.exit(2, "Invalid or unreadable bounded PCM capture\n")
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
