#!/usr/bin/env python3
"""Button Box piano ringtones: original two-hand piano pieces with the
"But-ton Box!" hook (G5, E5, C6), written as note events so the same piece
renders to audio and exports to a standard MIDI file.

Usage: make_ringtones_piano.py [OUT_DIR]
"""
import os
import sys
from functools import lru_cache

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_ringtones_v2 import SR, RNG, Track, band, decay, hz  # noqa: E402

LETTERS = "CDEFGAB"


def step(note, steps):
    """Move a natural note up or down the C major scale."""
    i = LETTERS.index(note[0]) + int(note[-1]) * 7 + steps
    return LETTERS[i % 7] + str(i // 7)


@lru_cache(maxsize=None)
def piano(note, vel, hold):
    """Additive piano: stretched partials, two-stage decay, two strings, hammer, damper."""
    f = hz(note)
    tail = 0.35
    n = int(SR * (hold + tail))
    t = np.arange(n) / SR
    base = float(np.clip(4.5 * (262.0 / f) ** 0.75, 0.9, 7.0))
    out = np.zeros(n)
    for k in range(1, 15):
        fk = k * f * np.sqrt(1 + 0.00035 * k * k)
        if fk > 9000:
            break
        amp = k ** -1.15 * np.exp(-(k - 1) * (1.0 - vel) * 0.45)
        if k == 1 and f < 300:
            amp *= (f / 300.0) ** 2
        t60 = base / (1 + 0.28 * (k - 1))
        env = 0.6 * np.exp(-6.91 * t / (t60 * 0.14)) + 0.4 * np.exp(-6.91 * t / t60)
        tone = np.sin(2 * np.pi * fk * t)
        if k <= 6:
            tone = 0.62 * tone + 0.38 * np.sin(2 * np.pi * fk * 1.0006 * t + 0.7)
        out += amp * env * tone
    damp = np.ones(n)
    h = int(SR * hold)
    damp[h:] = np.exp(-6.91 * np.arange(n - h) / SR / 0.18)
    out *= damp
    a = int(SR * 0.0015)
    out[:a] *= np.linspace(0, 1, a)
    hammer = band(RNG.standard_normal(int(SR * 0.004)), 800, 4000) * 0.03 * vel
    out[: len(hammer)] += hammer
    return out * vel


VO = {"C": ("C4", "G4", "C5", "E5"), "G": ("G3", "D4", "G4", "B4"), "Am": ("A3", "E4", "A4", "C5"),
      "F": ("F3", "C4", "F4", "A4"), "Dm": ("D4", "A4", "D5", "F5")}


def melody_events(tune, vel_by_bar, beats_per_bar, harmony_from=None):
    ev, b = [], 0.0
    for name, d in tune:
        if name:
            bar = int(b // beats_per_bar)
            v = vel_by_bar(bar) + (0.06 if name[-1] == "6" and name[0] in "GAB" else 0)
            ev.append((b, name, d * 0.95, min(v, 0.95), 0))
            if harmony_from is not None and bar >= harmony_from and d >= 0.5:
                ev.append((b, step(name, -2), d * 0.9, v * 0.42, 0))
        b += d
    return ev


def arp8(chords, pattern, vel_by_bar, bpb=4):
    ev = []
    for bar, ch in enumerate(chords):
        parts = ch.split("|")
        for k in range(bpb * 2):
            name = parts[0] if k < bpb or len(parts) == 1 else parts[1]
            ev.append((bar * bpb + k * 0.5, VO[name][pattern[k % len(pattern)]], 0.9, vel_by_bar(bar) * (1 if k % 2 == 0 else 0.82), 1))
    return ev


def stride(chords, vel_by_bar):
    ev = []
    for bar, ch in enumerate(chords):
        parts = ch.split("|")
        for beat in range(4):
            name = parts[0] if beat < 2 or len(parts) == 1 else parts[1]
            v = vel_by_bar(bar)
            if beat % 2 == 0:
                ev.append((bar * 4 + beat, VO[name][0] if beat == 0 else VO[name][1], 0.8, v, 1))
            else:
                for i, nt in enumerate(VO[name][1:]):
                    ev.append((bar * 4 + beat + i * 0.01, nt, 0.35, v * 0.7, 1))
    return ev


def waltz_arp(chords, vel_by_bar):
    ev = []
    for bar, ch in enumerate(chords):
        r, fifth, octv, third = VO[ch]
        for k, nt in enumerate([r, fifth, third, fifth, octv, fifth]):
            ev.append((bar * 3 + k * 0.5, nt, 1.2 if k == 0 else 0.7, vel_by_bar(bar) * (1 if k == 0 else 0.75), 1))
    return ev


def ending(at, rh=("C6", "E6", "G6"), lh=("C4", "G4", "C5", "E5"), vel=0.6):
    ev = [(at + i * 0.06, nt, 3.0, vel * 0.8, 1) for i, nt in enumerate(lh)]
    ev += [(at + 0.25 + i * 0.06, nt, 3.0, vel, 0) for i, nt in enumerate(rh)]
    return ev


def hello_piano():
    tune = [("G5", .5), ("E5", .5), ("C6", 1), (None, .5), ("D6", .5), ("E6", .5), ("C6", .5),
            ("A5", .5), ("C6", .5), ("F6", 1), ("E6", .5), ("D6", .5), ("C6", 1),
            ("B5", .5), ("D6", .5), ("G6", 1), ("F6", .5), ("E6", .5), ("D6", 1),
            ("E6", 1.5), ("C6", .5), ("G5", 2),
            ("G5", .5), ("E5", .5), ("C6", 1), (None, .5), ("D6", .5), ("E6", .5), ("C6", .5),
            ("C6", .5), ("E6", .5), ("A6", 1.5), ("G6", .5), ("E6", 1),
            ("F6", .5), ("E6", .5), ("D6", 1), ("B5", .5), ("C6", .5), ("D6", 1),
            ("G5", .5), ("E5", .5), ("C6", 3)]
    chords = ["C", "F", "G", "C", "C", "Am", "F|G", "C"]
    vel = lambda bar: 0.55 if bar < 4 else 0.72
    ev = melody_events(tune, vel, 4, harmony_from=4) + stride(chords[:7], lambda b: 0.42 if b < 4 else 0.5)
    ev += ending(29.0)
    return 150, 4, ev


def sunshine():
    tune = [("E5", .5), ("G5", .5), ("C6", 1), ("B5", .5), ("C6", .5), ("D6", 1),
            ("D6", 1.5), ("B5", .5), ("G5", 2),
            ("A5", .5), ("C6", .5), ("E6", 1), ("D6", .5), ("C6", .5), ("A5", 1),
            ("A5", .5), ("C6", .5), ("F6", 1.5), ("E6", .5), ("D6", 1),
            ("G5", .5), ("E5", .5), ("C6", 1.5), ("E6", .5), ("G6", 1),
            ("G6", 1.5), ("F6", .5), ("D6", 2),
            ("A6", 1), ("G6", .5), ("F6", .5), ("E6", .5), ("D6", .5), ("C6", 1),
            ("G5", .5), ("E5", .5), ("C6", 3)]
    chords = ["C", "G", "Am", "F", "C", "G", "F", "C"]
    vel = lambda bar: 0.5 if bar < 4 else 0.74
    ev = melody_events(tune, vel, 4, harmony_from=4) + arp8(chords[:7], [0, 1, 2, 1, 3, 1, 2, 1], lambda b: 0.3 if b < 4 else 0.42)
    ev += ending(29.0, lh=("C4", "G4", "C5", "E5"), vel=0.66)
    return 136, 4, ev


def bouncy():
    tune = [("G5", .5), ("E5", .5), ("C6", 1), ("E6", .5), ("D6", .5), ("C6", 1),
            ("G5", .75), ("A5", .75), ("C6", .5), ("E6", .75), ("D6", .75), ("C6", .5),
            ("A5", .5), ("C6", .5), ("F6", 1), ("E6", .5), ("F6", .5), ("A6", 1),
            ("G6", .75), ("E6", .75), ("C6", 1.5), (None, 1),
            ("B5", .5), ("D6", .5), ("G6", 1), ("F6", .5), ("E6", .5), ("D6", 1),
            ("B5", .75), ("C6", .75), ("D6", .5), ("F6", .75), ("E6", .75), ("D6", .5),
            ("E6", .5), ("G6", .5), ("C7", 1), ("B6", .5), ("A6", .5), ("G6", 1),
            ("G5", .5), ("E5", .5), ("C6", 2), (None, 1)]
    chords = ["C", "C", "F", "C", "G", "G", "C|G", "C"]
    vel = lambda bar: 0.6 if bar < 4 else 0.72
    ev = melody_events(tune, vel, 4) + stride(chords[:7], lambda b: 0.45)
    ev += ending(29.0, rh=("E6", "G6", "C7"), vel=0.62)
    return 140, 4, ev


def waltz():
    tune = [("G5", 1), ("E5", 1), ("C6", 1), ("E6", 2), ("C6", 1),
            ("A5", 1), ("C6", 1), ("F6", 1), ("E6", 1), ("D6", 2),
            ("G5", 1), ("E5", 1), ("C6", 1), ("A6", 2), ("G6", 1),
            ("F6", 1), ("E6", 1), ("D6", 1), ("B5", 2), ("D6", 1),
            ("E6", 2), ("G6", 1), ("C7", 2), ("A6", 1),
            ("G6", 1), ("F6", 1), ("D6", 1), ("C6", 3)]
    chords = ["C", "Am", "F", "G", "C", "Am", "Dm", "G", "C", "F", "G", "C"]
    vel = lambda bar: 0.52 if bar < 4 else (0.66 if bar < 8 else 0.74)
    ev = melody_events(tune, vel, 3, harmony_from=8) + waltz_arp(chords[:11], lambda b: 0.32 if b < 8 else 0.4)
    ev += ending(33.0, lh=("C4", "G4", "C5", "E5"), vel=0.6)
    return 160, 3, ev


PIECES = {"hello-piano": ("Hello, piano", hello_piano), "sunshine": ("Sunshine", sunshine),
          "bouncy": ("Bouncy", bouncy), "waltz": ("Waltz", waltz)}


def render(bpm, events):
    beat = 60.0 / bpm
    end = max(b + d for b, _, d, _, _ in events)
    tr = Track(end * beat + 1.2)
    for b, note, d, v, _ in events:
        tr.put(0.08 + b * beat, piano(note, round(v, 2), round(d * beat, 3)))
    return tr.finish(swell_to=0.35, start_level=0.55, wet=0.2)


def write_midi(path, bpm, bpb, events):
    import mido
    tpb = 480
    mid = mido.MidiFile(ticks_per_beat=tpb)
    names = ["Right hand", "Left hand"]
    for track_no in (0, 1):
        tr = mido.MidiTrack()
        mid.tracks.append(tr)
        tr.append(mido.MetaMessage("track_name", name=names[track_no], time=0))
        if track_no == 0:
            tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
            tr.append(mido.MetaMessage("time_signature", numerator=bpb, denominator=4, time=0))
        tr.append(mido.Message("program_change", program=0, channel=track_no, time=0))
        msgs = []
        for b, note, d, v, tk in events:
            if tk != track_no:
                continue
            num = 12 * (int(note[-1]) + 1) + [0, 2, 4, 5, 7, 9, 11][LETTERS.index(note[0])]
            msgs.append((int(b * tpb), 1, mido.Message("note_on", note=num, velocity=int(30 + v * 90), channel=track_no)))
            msgs.append((int((b + d) * tpb), 0, mido.Message("note_off", note=num, velocity=0, channel=track_no)))
        msgs.sort(key=lambda m: (m[0], m[1]))
        last = 0
        for tick, _, msg in msgs:
            tr.append(msg.copy(time=tick - last))
            last = tick
    mid.save(path)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    for key, (label, fn) in PIECES.items():
        bpm, bpb, events = fn()
        sig = render(bpm, events)
        wavfile.write(os.path.join(out_dir, f"ring-{key}.wav"), SR, (sig * 32767).astype(np.int16))
        write_midi(os.path.join(out_dir, f"ring-{key}.mid"), bpm, bpb, events)
        print(f"{key}: {label}, {len(sig) / SR:.1f} s, {len(events)} notes")


if __name__ == "__main__":
    main()
