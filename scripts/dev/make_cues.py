#!/usr/bin/env python3
"""Button Box cues (beeps), cut from the "But-ton Box!" motif (G5, E5, C6).

Same sound world as the ringtones: wooden marimba with a little piano for
warmth, glockenspiel sparkle for delight, and air for carrying messages.
Tap cues are short; signature cues are the motif at different sizes.

Usage: make_cues.py [OUT_DIR]
"""
import json
import os
import sys

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_ringtones_v2 import SR, RNG, Track, band, glock, marimba, pluck  # noqa: E402
from make_ringtones_piano import piano  # noqa: E402


def mp(note, vel=1.0, dur=0.9):
    """Marimba doubled by a soft piano: the Button Box 'voice' for cues."""
    m = marimba(note, vel, dur)
    p = piano(note, round(0.55 * vel, 2), round(min(dur, 0.6), 2))
    n = max(len(m), len(p))
    out = np.zeros(n)
    out[: len(m)] += m
    out[: len(p)] += 0.45 * p
    return out


def air(seconds, lo=900, hi=5000, rise=True):
    n = int(SR * seconds)
    s = band(RNG.standard_normal(n), lo, hi)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * t) ** 1.5 * (t if rise else (1 - t) + 0.2)
    return s * env * 0.25


def make(seconds, events, wet=0.1):
    tr = Track(seconds)
    for t, sig, g in events:
        tr.put(t, sig, g)
    n = len(tr.buf)
    out = tr.buf.copy()
    ir_n = int(SR * 0.5)
    ir = RNG.standard_normal(ir_n) * np.exp(-6.91 * np.arange(ir_n) / SR / 0.35)
    ir /= np.sqrt(np.sum(ir ** 2))
    from scipy.signal import fftconvolve
    out = out + wet * fftconvolve(out, ir)[:n]
    tail = int(SR * 0.03)
    out[-tail:] *= np.linspace(1, 0, tail)
    return out


CUES = {
    # Tap: short, no words.
    "press": ("A high five. One bright wooden tap on the motif's home note.",
              lambda: make(0.40, [(0.0, mp("C6", 1.0, 0.5), 1), (0.0, glock("C7", 0.12, 0.4), 1)])),
    "card": ("Picking someone up. A quick upward flick, different from the press.",
             lambda: make(0.45, [(0.0, pluck("G5", 0.7, 0.4), 1), (0.07, mp("C6", 0.9, 0.4), 1), (0.07, glock("G6", 0.18, 0.4), 1)])),
    "rec_go": ("Go! A tiny bright tick right after “one”.",
               lambda: make(0.25, [(0.0, glock("C7", 0.6, 0.3), 1), (0.0, mp("C6", 0.5, 0.25), 1)], wet=0.05)),
    "rec_limit": ("Wrap it up. Two soft taps, five seconds before the end.",
                  lambda: make(0.55, [(0.0, mp("E6", 0.45, 0.25), 1), (0.22, mp("E6", 0.35, 0.3), 1)])),
    "msg_start": ("Unwrapping. A quick sparkle up, then the family voice.",
                  lambda: make(0.6, [(0.0, glock("C6", 0.35, 0.5), 1), (0.07, glock("E6", 0.4, 0.5), 1), (0.14, glock("G6", 0.45, 0.5), 1), (0.21, mp("C6", 0.6, 0.4), 1)])),
    "msg_end": ("That's all. The “Box!” note, warm and settled.",
                lambda: make(0.75, [(0.0, mp("E6", 0.5, 0.6), 1), (0.12, mp("C6", 0.65, 0.7), 1), (0.12, mp("G5", 0.35, 0.7), 1)])),
    "oops": ("A playful shrug. A bouncy “uh-oh”, two notes down. As loud as the press.",
             lambda: make(0.6, [(0.0, mp("A5", 0.9, 0.35), 1), (0.0, pluck("A5", 0.4, 0.3), 1), (0.2, mp("F5", 0.85, 0.45), 1), (0.2, pluck("F5", 0.4, 0.4), 1)])),
    "still_trying": ("Still trying. Two gentle knocks on the same note.",
                     lambda: make(0.6, [(0.0, mp("G5", 0.5, 0.3), 1), (0.25, mp("G5", 0.42, 0.3), 1)])),
    "offline": ("I lost the internet. One soft fall, played once.",
                lambda: make(0.75, [(0.0, mp("E6", 0.55, 0.4), 1), (0.22, mp("C6", 0.45, 0.5), 1), (0.22, mp("A5", 0.3, 0.5), 1)])),
    # Signature: the motif at different sizes.
    "ready": ("I'm awake. A quick “But-ton Box!”",
              lambda: make(0.95, [(0.0, mp("G5", 0.75, 0.3), 1), (0.15, mp("E5", 0.7, 0.3), 1), (0.3, mp("C6", 0.9, 0.6), 1), (0.32, glock("C7", 0.18, 0.6), 1)])),
    "connected": ("I'm here! The airport hug: the motif, its answer, and a sparkle.",
                  lambda: make(1.9, [(0.0, mp("G5", 0.8, 0.3), 1), (0.17, mp("E5", 0.75, 0.3), 1), (0.34, mp("C6", 0.95, 0.5), 1),
                                     (0.72, mp("D6", 0.8, 0.3), 1), (0.89, mp("E6", 0.85, 0.3), 1), (1.06, mp("C6", 1.0, 0.8), 1),
                                     (1.06, mp("G5", 0.45, 0.8), 1), (1.06, mp("E5", 0.4, 0.8), 1),
                                     (1.1, glock("E6", 0.25, 0.8), 1), (1.16, glock("G6", 0.25, 0.8), 1), (1.22, glock("C7", 0.3, 0.8), 1)])),
    "all_set": ("All set! The biggest moment. The connected sound with a full chord at the end.",
                lambda: make(2.6, [(0.0, mp("G5", 0.8, 0.3), 1), (0.17, mp("E5", 0.75, 0.3), 1), (0.34, mp("C6", 0.95, 0.5), 1),
                                   (0.72, mp("D6", 0.8, 0.3), 1), (0.89, mp("E6", 0.85, 0.3), 1), (1.06, mp("G6", 0.9, 0.4), 1),
                                   (1.3, mp("C6", 1.0, 1.2), 1), (1.3, piano("C5", 0.5, 1.2), 1), (1.33, piano("E5", 0.45, 1.2), 1), (1.36, piano("G5", 0.45, 1.2), 1),
                                   (1.32, glock("C7", 0.3, 1.0), 1), (1.4, glock("E7", 0.2, 1.0), 1)], wet=0.16)),
    "sent": ("I'll take it to them. A short whoosh that lifts off into the motif's high notes.",
             lambda: make(1.3, [(0.0, air(0.75), 1), (0.45, mp("C6", 0.6, 0.3), 1), (0.57, mp("E6", 0.65, 0.3), 1), (0.69, mp("G6", 0.75, 0.6), 1), (0.72, glock("C7", 0.22, 0.6), 1)], wet=0.14)),
    "listened": ("They heard it! The happiest small sound: a sparkle and the “Box!” chord.",
                 lambda: make(1.2, [(0.0, glock("G6", 0.35, 0.6), 1), (0.08, glock("C7", 0.4, 0.6), 1), (0.16, glock("E7", 0.3, 0.6), 1),
                                    (0.2, mp("C6", 0.8, 0.8), 1), (0.2, mp("E6", 0.55, 0.8), 1), (0.2, mp("G5", 0.4, 0.8), 1)], wet=0.16)),
    "card_saved": ("Card saved. The pick-up flick, then a happy “yes”.",
                   lambda: make(0.9, [(0.0, pluck("G5", 0.7, 0.4), 1), (0.07, mp("C6", 0.85, 0.4), 1), (0.3, mp("E6", 0.8, 0.5), 1), (0.3, glock("C7", 0.22, 0.5), 1)])),
}


def level(sig, target_db=-13.0, ceiling_db=-1.0):
    """Match the loudest 100 ms to one RMS target so no cue is quieter than another, then cap the peak."""
    w = int(SR * 0.1)
    loud = max(np.sqrt(np.mean(sig[i:i + w] ** 2)) for i in range(0, max(1, len(sig) - w), w // 4))
    sig = sig * (10 ** (target_db / 20) / loud)
    c = 10 ** (ceiling_db / 20)
    return np.where(np.abs(sig) > 0.7, np.sign(sig) * (0.7 + (c - 0.7) * np.tanh((np.abs(sig) - 0.7) / (c - 0.7))), sig)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    info = {}
    for key, (desc, fn) in CUES.items():
        sig = level(fn())
        wavfile.write(os.path.join(out_dir, f"cue-{key}.wav"), SR, (sig * 32767).astype(np.int16))
        info[key] = {"desc": desc, "seconds": round(len(sig) / SR, 2)}
        print(f"{key:13s} {len(sig) / SR:.2f} s")
    with open(os.path.join(out_dir, "cues.json"), "w") as fh:
        json.dump(info, fh, indent=1)


if __name__ == "__main__":
    main()
