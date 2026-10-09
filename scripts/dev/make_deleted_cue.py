#!/usr/bin/env python3
"""Generate only the deletion cue, preserving the approved bundled cues."""

import array
import hashlib
import json
import math
import sys
import wave
from pathlib import Path

RATE = 48000
SECONDS = 0.4
MEANING = "Deleted. Two soft descending wooden notes, without a voice."


def loudest_window(samples):
    window = RATE // 10
    return max(math.sqrt(sum(x * x for x in samples[i:i + window]) / window)
               for i in range(0, len(samples) - window + 1, window // 4))


def generate(directory):
    with wave.open(str(directory / "cue-oops.wav"), "rb") as source:
        reference = array.array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        reference.byteswap()
    samples = [0.0] * round(RATE * SECONDS)
    # G5 -> E5 follows the cue family's motif. Rounded attacks and a light
    # second harmonic soften the wooden timbre without adding a sharp click.
    for start, frequency, gain in ((0, 783.9909, 1.0), (0.17, 659.2551, 0.9)):
        for index in range(round(start * RATE), len(samples)):
            t = index / RATE - start
            envelope = (1 - math.exp(-t / 0.008)) * math.exp(-t / 0.065)
            samples[index] += gain * envelope * (
                math.sin(2 * math.pi * frequency * t)
                + 0.18 * math.sin(4 * math.pi * frequency * t))
    for index in range(RATE // 50):
        samples[-1 - index] *= index / (RATE // 50)
    scale = loudest_window(reference) / loudest_window(samples)
    pcm = array.array("h", (round(value * scale) for value in samples))
    if sys.byteorder != "little":
        pcm.byteswap()
    target = directory / "cue-deleted.wav"
    with wave.open(str(target), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", ""))
        output.writeframes(pcm.tobytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["cues"] = [row for row in manifest["cues"] if row["file"] != target.name]
    manifest["cues"].append({"file": target.name, "seconds": SECONDS,
                             "meaning": MEANING, "sha256": digest})
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
    catalog_path = directory / "cues.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["deleted"] = {"desc": MEANING, "seconds": SECONDS}
    catalog_path.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    print(f"{digest}  {target.name}")


if __name__ == "__main__":
    generate(Path(sys.argv[1]) if len(sys.argv) > 1
             else Path(__file__).resolve().parents[2] / "sounds/cues")
