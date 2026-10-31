#!/usr/bin/env python3
"""Button Box melodic ringtones: original tunes with "But-ton Box!" as the hook.

Builds on make_ringtones_v2.py (same instruments, swell, and limiter) and adds
three melodic voices: a whistled lead, a soft electric piano, and a steel pan.
All tunes are original, in C major, and stay between about 350 Hz and 4 kHz.

Usage: make_ringtones_melodic.py [OUT_DIR]
"""
import os
import sys

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_ringtones_v2 as v2  # noqa: E402
from make_ringtones_v2 import SR, RNG, Track, band, decay, glock, hz, marimba, partials, pluck, shaker  # noqa: E402


def epiano(note, vel=1.0, dur=1.8):
    f = hz(note)
    n = int(SR * dur)
    t = np.arange(n) / SR
    index = 0.35 + 1.8 * np.exp(-t / 0.22)
    sig = np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * f * t))
    sig *= decay(n, float(np.clip(1.8 * (523.0 / f) ** 0.5, 0.8, 2.4)))
    sig[: int(SR * 0.002)] *= np.linspace(0, 1, int(SR * 0.002))
    return sig * vel


def steel(note, vel=1.0, dur=1.4):
    f = hz(note)
    spec = [(1, 1.0, 0.95), (2.0, 0.42, 0.65), (2.012, 0.18, 0.6), (3.0, 0.24, 0.42), (4.18, 0.07, 0.18)]
    return partials(f, spec, dur, 4.0) * vel


def lead(track, start, notes, beat, gain=0.55):
    """A whistled, legato lead with glides and gentle vibrato. notes: [(name or None, beats)]."""
    total = sum(d for _, d in notes) * beat + 0.4
    n = int(SR * total)
    freq = np.full(n, 440.0)
    amp = np.zeros(n)
    t = 0.0
    prev = None
    for idx, (name, d) in enumerate(notes):
        i0, i1 = int(SR * t), int(SR * (t + d * beat))
        t += d * beat
        if name is None:
            prev = None
            continue
        fr = hz(name)
        m = i1 - i0
        tt = np.arange(m) / SR
        seg = np.full(m, fr)
        if prev:
            g = min(m, int(SR * 0.035))
            seg[:g] = np.exp(np.linspace(np.log(prev), np.log(fr), g))
        vib = 1 + 0.006 * np.sin(2 * np.pi * 5.4 * tt) * np.clip((tt - 0.16) / 0.25, 0, 1)
        freq[i0:i1] = seg * vib
        nxt = notes[idx + 1][0] if idx + 1 < len(notes) else None
        env = np.ones(m) * np.linspace(1.0, 0.86, m)
        a = min(m, int(SR * 0.022))
        env[:a] *= np.linspace(0.55 if prev else 0.0, 1, a)
        r = min(m, int(SR * (0.03 if nxt else 0.12)))
        env[-r:] *= np.linspace(1, 0.6 if nxt else 0.0, r)
        amp[i0:i1] = env
        prev = fr
    if np.any(freq[: int(SR * 0.05)] == 440.0):
        first = next(hz(nm) for nm, _ in notes if nm)
        freq[freq == 440.0] = first
    phase = np.cumsum(2 * np.pi * freq / SR)
    sig = np.sin(phase) + 0.12 * np.sin(2 * phase) + 0.04 * np.sin(3 * phase)
    breath = band(RNG.standard_normal(n), 1500, 6000) * 0.035
    track.put(start, (sig + breath) * amp, gain)


def melody_on(track, start, notes, beat, inst, vel=1.0):
    t = start
    for name, d in notes:
        if name:
            track.put(t, inst(name, vel))
        t += d * beat


VOICINGS = {"C": ["G4", "C5", "E5", "G5"], "Am": ["A4", "C5", "E5", "A5"],
            "F": ["F4", "A4", "C5", "F5"], "G": ["G4", "B4", "D5", "G5"]}
ROOTS = {"C": "C5", "Am": "A4", "F": "F4", "G": "G4"}


def sing_along():
    """A whistled tune over a rolling piano. The hook leaps up in bar 6."""
    beat = 0.4
    tune = [("G5", .5), ("E5", .5), ("C6", 1.5), ("D6", .5), ("E6", 1),
            ("E6", .5), ("D6", .5), ("C6", .5), ("A5", .5), ("C6", 2),
            ("A5", .5), ("C6", .5), ("F6", 1.5), ("E6", .5), ("D6", 1),
            ("D6", .5), ("C6", .5), ("B5", .5), ("G5", .5), ("D6", 2),
            ("G5", .5), ("E5", .5), ("C6", 1.5), ("D6", .5), ("E6", 1),
            ("E6", .5), ("G6", .5), ("A6", 1.5), ("G6", .5), ("E6", 1),
            ("F6", .5), ("E6", .5), ("D6", 1), ("B5", .5), ("C6", .5), ("D6", 1),
            ("G5", .5), ("E5", .5), ("C6", 3)]
    chords = ["C", "Am", "F", "G", "C", "Am", "F|G", "C"]
    tr = Track(14.2)
    t0 = 0.1
    lead(tr, t0, tune, beat)
    pattern = [0, 2, 1, 2, 3, 2, 1, 2]
    for b, ch in enumerate(chords):
        bt = t0 + b * 4 * beat
        vel = 0.3 if b < 4 else 0.42
        for k in range(8):
            name = ch.split("|")[0 if k < 4 else -1]
            if b == 7 and k > 0:
                break
            tr.put(bt + k * beat / 2, epiano(VOICINGS[name][pattern[k]], vel * (1.0 if k % 2 == 0 else 0.8)))
        for half, name in enumerate(ch.split("|") if "|" in ch else [ch, ch]):
            tr.put(bt + half * 2 * beat, marimba(ROOTS[name], 0.33, 1.4))
        if b >= 4 and b < 7:
            for k in range(8):
                tr.put(bt + k * beat / 2 + 0.01, shaker(0.5 if k % 2 else 0.8))
    melody_on(tr, t0 + 5 * 4 * beat, [("E6", .5), ("G6", .5), ("A6", 3)], beat, glock, 0.22)
    end = t0 + 7 * 4 * beat + beat
    for i, nm in enumerate(["C5", "E5", "G5", "C6"]):
        tr.put(end + i * 0.02, epiano(nm, 0.4, 2.4))
    tr.put(end + 0.05, glock("C7", 0.3))
    return tr.finish(swell_to=0.4, start_level=0.5, wet=0.18)


def carousel():
    """A bright waltz: oom-pah-pah under a glockenspiel and marimba tune."""
    beat = 0.357
    tune = [("G5", 1), ("E5", 1), ("C6", 1), ("E6", 2), ("D6", 1),
            ("B5", 1), ("D6", 1), ("G6", 1), ("D6", 3),
            ("A5", 1), ("C6", 1), ("F6", 1), ("E6", 2), ("C6", 1),
            ("B5", 1), ("D6", 1), ("F6", 1), ("E6", 2), ("C6", 1),
            ("G5", 1), ("E5", 1), ("C6", 1), ("A6", 2), ("F6", 1),
            ("G6", 1), ("F6", 1), ("D6", 1), ("C6", 3)]
    chords = ["C", "C", "G", "G", "F", "C", "G", "C", "C", "F", "G", "C"]
    tr = Track(13.6)
    t0 = 0.1
    melody_on(tr, t0, tune, beat, glock, 0.55)
    melody_on(tr, t0, tune, beat, marimba, 0.6)
    for b, ch in enumerate(chords):
        bt = t0 + b * 3 * beat
        tr.put(bt, marimba(ROOTS[ch], 0.4, 1.0))
        if b < len(chords) - 1:
            for k in (1, 2):
                for i, nm in enumerate(VOICINGS[ch][1:]):
                    tr.put(bt + k * beat + i * 0.008, pluck(nm, 0.18, 0.5))
    end = t0 + 11 * 3 * beat
    tr.put(end, glock("E6", 0.25))
    tr.put(end + 0.07, glock("G6", 0.25))
    tr.put(end + 0.14, glock("C7", 0.3))
    return tr.finish(swell_to=0.4, start_level=0.5)


def island():
    """A steel-pan calypso with offbeat strums and a shaker."""
    beat = 0.395
    bar1 = [("G5", .75), ("E5", .75), ("C6", .5), (None, .5), ("E6", .5), ("D6", .5), ("C6", .5)]
    bar2 = [("A5", .75), ("C6", .75), ("F6", .5), (None, .5), ("E6", .5), ("D6", .5), ("C6", .5)]
    bar3 = [("B5", .75), ("D6", .75), ("G6", .5), (None, .5), ("F6", .5), ("E6", .5), ("D6", .5)]
    bar4 = [("E6", .75), ("C6", .75), ("G5", 1.5), (None, 1)]
    bar7 = [("B5", .75), ("D6", .75), ("G6", .5), (None, .5), ("F6", .5), ("D6", .5), ("B5", .5)]
    bar8 = [("G5", .5), ("E5", .5), ("C6", 2), (None, 1)]
    tune = bar1 + bar2 + bar3 + bar4 + bar1 + bar2 + bar7 + bar8
    chords = ["C", "F", "G", "C", "C", "F", "G", "C"]
    tr = Track(13.4)
    t0 = 0.1
    melody_on(tr, t0, tune, beat, steel, 0.75)
    melody_on(tr, t0, tune, beat, marimba, 0.25)
    for b, ch in enumerate(chords):
        bt = t0 + b * 4 * beat
        for k, off in enumerate([0, 1.5, 2, 3.5]):
            if b == 7 and off > 2:
                break
            tr.put(bt + off * beat, marimba(ROOTS[ch], 0.38 if k % 2 == 0 else 0.28, 0.9))
        for k in range(4):
            if b == 7 and k > 1:
                break
            for i, nm in enumerate(VOICINGS[ch][1:]):
                tr.put(bt + (k + 0.5) * beat + i * 0.01, pluck(nm, 0.16, 0.35))
        if b >= 2:
            for k in range(8):
                if b == 7 and k > 3:
                    break
                tr.put(bt + k * beat / 2 + 0.01, shaker(0.45 if k % 2 else 0.75))
    end = t0 + 7 * 4 * beat + beat
    tr.put(end, steel("E6", 0.4))
    tr.put(end + 0.05, steel("G6", 0.35))
    return tr.finish(swell_to=0.4, start_level=0.5, wet=0.16)


RINGTONES = {"singalong": ("Sing-along", sing_along), "carousel": ("Carousel", carousel), "island": ("Island", island)}


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    for key, (label, fn) in RINGTONES.items():
        sig = fn()
        path = os.path.join(out_dir, f"ring-{key}.wav")
        wavfile.write(path, SR, (sig * 32767).astype(np.int16))
        print(f"{path}: {label}, {len(sig) / SR:.1f} s")


if __name__ == "__main__":
    main()
