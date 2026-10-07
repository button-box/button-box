#!/usr/bin/env python3
"""Button Box ringtones, v2 sketches.

Every ringtone is built on the "But-ton Box!" motif (G5, E5, C6: short,
short, long) in the home key of C. The instruments come from the sound world
on the character sheet: wooden marimba, glockenspiel sparkle, a doorbell,
a ukulele pluck, and a light shaker. Everything sits between about 390 Hz
and 4 kHz for the 40 mm speaker, swells in so it never startles, and lands on
the same "Box!" note at the end.

Usage: make_ringtones_v2.py [OUT_DIR] [--shift SEMITONES]
"""
import argparse
import os

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, lfilter

SR = 48000
RNG = np.random.default_rng(7)
SEMI = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}
SHIFT = 0


def hz(name):
    return 440.0 * 2 ** ((SEMI[name[0]] + (int(name[-1]) - 4) * 12 + SHIFT) / 12)


def decay(n, t60):
    return np.exp(-6.91 * np.arange(n) / SR / max(t60, 1e-3))


def attack(sig, ms):
    k = max(1, int(SR * ms / 1000))
    sig[:k] *= np.linspace(0, 1, k)
    return sig


def band(sig, lo, hi):
    b, a = butter(2, [lo / (SR / 2), hi / (SR / 2)], btype="band")
    return lfilter(b, a, sig)


def partials(f, spec, dur, attack_ms=1.5):
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for ratio, amp, t60 in spec:
        if f * ratio < SR / 2 - 2000:
            out += amp * np.sin(2 * np.pi * f * ratio * t) * decay(n, t60)
    return attack(out, attack_ms)


def marimba(note, vel=1.0, dur=1.4):
    f = hz(note)
    t60 = float(np.clip(0.95 * (523.0 / f) ** 0.6, 0.3, 1.3))
    sig = partials(f, [(1, 1.0, t60), (3.93, 0.32, t60 * 0.22), (9.2, 0.07, t60 * 0.07)], dur)
    click = band(RNG.standard_normal(int(SR * 0.006)), 1800, 5000) * 0.05
    sig[: len(click)] += click * decay(len(click), 0.004)
    return sig * vel


def glock(note, vel=1.0, dur=2.0):
    f = hz(note)
    return partials(f, [(1, 1.0, 1.7), (2.76, 0.42, 0.65), (5.40, 0.18, 0.3), (8.93, 0.07, 0.14)], dur, 0.8) * vel


def bell(note, vel=1.0, dur=3.0):
    f = hz(note)
    spec = [(0.5, 0.22, 2.6), (1, 1.0, 2.2), (1.002, 0.3, 2.2), (2, 0.34, 1.1), (2.76, 0.14, 0.6), (4.07, 0.06, 0.3)]
    return partials(f, spec, dur, 2.0) * vel


def pluck(note, vel=1.0, dur=1.2):
    f = hz(note)
    spec = [(k, (1.0 / k) * abs(np.sin(np.pi * k * 0.22)), 0.9 / (1 + 0.7 * (k - 1))) for k in range(1, 11)]
    return partials(f, spec, dur, 2.5) * vel


def knock(vel=1.0):
    n = int(SR * 0.12)
    t = np.arange(n) / SR
    body = (np.sin(2 * np.pi * 780 * t) + 0.5 * np.sin(2 * np.pi * 1650 * t)) * decay(n, 0.05)
    noise = band(RNG.standard_normal(n), 900, 3500) * decay(n, 0.012) * 0.3
    return attack(body + noise, 0.5) * 0.6 * vel


def shaker(vel=1.0):
    n = int(SR * 0.09)
    s = band(RNG.standard_normal(n), 4500, 11000)
    env = np.minimum(np.linspace(0, 1, n) * 6, 1) * decay(n, 0.06)
    return s * env * 0.12 * vel


class Track:
    def __init__(self, seconds):
        self.buf = np.zeros(int(SR * seconds))

    def put(self, t, sig, gain=1.0):
        i = int(SR * t)
        j = min(len(self.buf), i + len(sig))
        self.buf[i:j] += sig[: j - i] * gain

    def finish(self, swell_to=0.6, start_level=0.45, wet=0.14):
        n = len(self.buf)
        ir_n = int(SR * 0.9)
        ir = RNG.standard_normal(ir_n) * decay(ir_n, 0.7)
        b, a = butter(1, 5000 / (SR / 2))
        ir = lfilter(b, a, ir)
        ir /= np.sqrt(np.sum(ir ** 2))
        wet_sig = fftconvolve(self.buf, ir)[:n]
        mix = self.buf + wet * wet_sig
        k = int(n * swell_to)
        swell = np.ones(n)
        swell[:k] = start_level + (1 - start_level) * (np.linspace(0, 1, k) ** 1.5)
        mix *= swell
        mix = np.tanh(mix / (np.max(np.abs(mix)) * 0.9) * 1.1)
        tail = int(SR * 0.04)
        mix[-tail:] *= np.linspace(1, 0, tail)
        return mix / np.max(np.abs(mix)) * 10 ** (-1 / 20)


E8 = 0.19  # one short step of "But-ton"


def motif(tr, t, inst, vel=1.0, land=0.62):
    tr.put(t, inst("G5", vel * 0.85))
    tr.put(t + E8, inst("E5", vel * 0.8))
    tr.put(t + 2 * E8, inst("C6", vel))
    return t + 2 * E8 + land


def hello():
    """Marimba call and answer: "But-ton Box!", then "it's me!" landing on C."""
    tr = Track(9.2)
    bar = 2.6
    for rep in range(3):
        t0 = 0.05 + rep * bar
        motif(tr, t0, marimba)
        tr.put(t0, marimba("C5", 0.35, 1.6))
        a = t0 + 1.25
        tr.put(a, marimba("D6", 0.8))
        tr.put(a + E8, marimba("E6", 0.85))
        tr.put(a + 2 * E8, marimba("C6", 1.0))
        tr.put(a, marimba("G4", 0.3, 1.6))
        tr.put(a + 2 * E8 + 0.03, glock("C7", 0.22 + 0.08 * rep))
        if rep >= 1:
            for k in range(10):
                tr.put(t0 + k * E8 + 0.02, shaker(0.6 if k % 2 else 1.0))
        if rep == 2:
            tr.put(a + 2 * E8, marimba("G5", 0.5))
            tr.put(a + 2 * E8, marimba("E5", 0.45))
    end = 0.05 + 3 * bar
    tr.put(end, glock("E6", 0.3))
    tr.put(end + 0.06, glock("G6", 0.28))
    tr.put(end + 0.12, glock("C7", 0.32))
    return tr.finish()


def doorbell():
    """Two friendly knocks, then the motif as a ringing doorbell."""
    tr = Track(9.4)
    tr.put(0.05, knock(0.8))
    tr.put(0.32, knock(1.0))
    for rep in range(2):
        t0 = 0.8 + rep * 3.6
        tr.put(t0, bell("G5", 0.8))
        tr.put(t0 + 0.42, bell("E5", 0.8))
        tr.put(t0 + 0.84, bell("C6", 1.0))
        tr.put(t0 + 0.84, glock("C7", 0.18))
        tr.put(t0 + 1.9, bell("G5", 0.55))
        tr.put(t0 + 2.15, bell("E5", 0.55))
        tr.put(t0 + 2.4, bell("C6", 0.8))
        tr.put(t0 + 2.4, bell("E6", 0.45))
        if rep == 0:
            tr.put(t0 + 3.2, knock(0.6))
            tr.put(t0 + 3.4, knock(0.75))
    tr.put(8.0, glock("G6", 0.2))
    tr.put(8.08, glock("C7", 0.25))
    return tr.finish(swell_to=0.5, start_level=0.55)


def ukulele():
    """A bouncy strum under the motif: C, G, C, with a down-up rhythm."""
    tr = Track(9.0)
    chords = {"C": ["G5", "C5", "E5", "C6"], "G": ["G5", "B5", "D6", "G5"]}
    pattern = [0, 0.38, 0.57, 0.95, 1.14, 1.52]
    bar = 1.9
    seq = ["C", "G", "C", "G"]
    for b, name in enumerate(seq):
        t0 = 0.05 + b * bar
        for k, p in enumerate(pattern):
            up = k % 2 == 1
            notes = chords[name][::-1] if up else chords[name]
            for i, nt in enumerate(notes):
                tr.put(t0 + p + i * 0.012, pluck(nt, 0.32 if up else 0.42, 0.7))
        if b % 2 == 0:
            motif(tr, t0, marimba, 0.95)
        else:
            tr.put(t0 + 0.2, marimba("D6", 0.75))
            tr.put(t0 + 0.2 + E8, marimba("B5", 0.7))
            tr.put(t0 + 0.2 + 2 * E8, marimba("G5", 0.8))
    end = 0.05 + 4 * bar
    motif(tr, end, marimba, 1.0)
    for i, nt in enumerate(["C5", "E5", "G5", "C6"]):
        tr.put(end + 2 * E8 + i * 0.015, pluck(nt, 0.4, 1.2))
    return tr.finish(swell_to=0.45)


def sparkle():
    """Glockenspiel runs up to the motif, like opening a box of lights."""
    tr = Track(8.6)
    for rep in range(3):
        t0 = 0.05 + rep * 2.6
        for i, nt in enumerate(["C6", "E6", "G6", "C7"]):
            tr.put(t0 + i * 0.09, glock(nt, 0.35 + 0.05 * i))
        m = t0 + 0.5
        tr.put(m, glock("G6", 0.7))
        tr.put(m, marimba("G5", 0.55))
        tr.put(m + E8, glock("E6", 0.65))
        tr.put(m + E8, marimba("E5", 0.5))
        tr.put(m + 2 * E8, glock("C7", 0.8))
        tr.put(m + 2 * E8, marimba("C6", 0.7))
        tr.put(m + 2 * E8, marimba("C5", 0.3, 1.6))
        tr.put(m + 1.2, glock("E7", 0.18))
        tr.put(m + 1.35, glock("G6", 0.15))
    return tr.finish(swell_to=0.55, wet=0.2)


RINGTONES = {
    "hello": ("Hello", hello),
    "doorbell": ("Doorbell", doorbell),
    "ukulele": ("Ukulele", ukulele),
    "sparkle": ("Sparkle", sparkle),
}


def main():
    global SHIFT
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", nargs="?", default=".")
    parser.add_argument("--shift", type=int, default=0, help="color accent, in half steps")
    args = parser.parse_args()
    SHIFT = args.shift
    os.makedirs(args.out_dir, exist_ok=True)
    for key, (label, fn) in RINGTONES.items():
        sig = fn()
        path = os.path.join(args.out_dir, f"ring-{key}.wav")
        wavfile.write(path, SR, (sig * 32767).astype(np.int16))
        print(f"{path}: {label}, {len(sig) / SR:.1f} s")


if __name__ == "__main__":
    main()
