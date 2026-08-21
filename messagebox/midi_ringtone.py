#!/usr/bin/env python3
"""Render a MIDI file into a Message Box ringtone WAV (music-box timbre).

Polyphonic: every note is synthesized at its onset with a percussive decay
envelope and mixed additively. Output is trimmed to --seconds, given a gentle
swell at the start (no startling) and a fade at the end.

Usage: midi_ringtone.py IN.mid OUT.wav [--seconds 20] [--swell 8]
"""
import argparse
import math
import struct
import wave

import mido

SR = 48000
TIMBRE = [(1, 1.0), (2, 0.35), (4, 0.12)]  # music-box: fundamental + soft harmonics


def render(mid_path, seconds, swell_s, fade_s=2.0, start_s=0.0):
    mid = mido.MidiFile(mid_path)
    # collect (onset_s, midi_note, velocity) across all tracks
    notes = []
    t = 0.0
    for msg in mid:  # merged playback order, .time is delta seconds
        t += msg.time
        if t < start_s:
            continue
        if t > start_s + seconds:
            break
        if msg.type == "note_on" and msg.velocity > 0:
            notes.append((t - start_s, msg.note, msg.velocity))
    if not notes:
        raise SystemExit("no notes found in %.0fs..%.0fs" % (start_s, start_s + seconds))

    n_total = int(SR * seconds)
    buf = [0.0] * n_total
    for onset, note, vel in notes:
        f = 440.0 * 2 ** ((note - 69) / 12)
        amp = (vel / 127) ** 1.5  # perceptual-ish velocity curve
        dur = 1.4  # music-box notes ring ~1.4s
        start = int(onset * SR)
        for i in range(min(int(dur * SR), n_total - start)):
            ts = i / SR
            env = math.exp(-3.0 * ts / dur)
            buf[start + i] += amp * env * sum(
                w * math.sin(2 * math.pi * f * h * ts) for h, w in TIMBRE)

    peak = max(abs(s) for s in buf) or 1.0
    out = []
    for i, s in enumerate(buf):
        t = i / SR
        g = 1.0
        if t < swell_s:                       # swell 25% -> 100%
            g *= 0.25 + 0.75 * t / swell_s
        if t > seconds - fade_s:              # fade out
            g *= max(0.0, (seconds - t) / fade_s)
        out.append(s / peak * 0.9 * g)
    return out


def write(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mid")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--swell", type=float, default=8)
    ap.add_argument("--start", type=float, default=0)
    args = ap.parse_args()
    write(args.out, render(args.mid, args.seconds, args.swell, start_s=args.start))
    print(f"wrote {args.out}")
