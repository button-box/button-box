#!/usr/bin/env python3
"""Synthesize candidate ringtones for the Message Box (new-message alert).

Pure-python WAV synth: sine + a couple of harmonics with a percussive decay
envelope — music-box / chiptune character. Each tone is ~9-10s so it carries
to other rooms. Output defaults to /opt/messagebox/ringtones/ring<N>.wav.
"""
import math
import os
import struct
import wave

SR = 48000
OUT_DIR = "/opt/messagebox/ringtones"

# note name -> semitone offset from A4
NOTES = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}

def freq(name):
    # e.g. "C5", "G#5" not needed — majors only
    octave = int(name[-1])
    semi = NOTES[name[:-1]] + (octave - 4) * 12
    return 440.0 * 2 ** (semi / 12)

def synth(seq, timbre, gap=0.0, amp=0.85):
    """seq: list of (note|None, seconds). timbre: list of (harmonic, weight)."""
    samples = []
    for note, dur in seq:
        n = int(SR * dur)
        if note is None:
            samples.extend([0.0] * n)
            continue
        f = freq(note)
        for i in range(n):
            t = i / SR
            env = math.exp(-3.5 * t / dur)  # percussive decay per note
            v = sum(w * math.sin(2 * math.pi * f * h * t) for h, w in timbre)
            samples.append(amp * env * v)
        samples.extend([0.0] * int(SR * gap))
    peak = max(abs(s) for s in samples) or 1.0
    return [s / peak * amp for s in samples]

def write(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))

MUSIC_BOX = [(1, 1.0), (2, 0.35), (4, 0.12)]      # glassy, gentle
CHIPTUNE = [(1, 1.0), (3, 0.5), (5, 0.25)]        # square-ish, video-gamey
BELL = [(1, 1.0), (2.76, 0.4), (5.4, 0.15)]       # inharmonic partials = bell

def ring1():
    """Music-box twinkle — Frère Jacques opening, dreamy."""
    phrase = [("C5", .45), ("D5", .45), ("E5", .45), ("C5", .45),
              ("C5", .45), ("D5", .45), ("E5", .45), ("C5", .45),
              ("E5", .45), ("F5", .45), ("G5", .9),
              ("E5", .45), ("F5", .45), ("G5", .9), (None, .5)]
    return synth(phrase * 2, MUSIC_BOX)

def ring2():
    """Chiptune bounce — playful major arpeggio up and down, fast."""
    up = [("C5", .18), ("E5", .18), ("G5", .18), ("C6", .3)]
    down = [("G5", .18), ("E5", .18), ("C5", .3), (None, .35)]
    return synth((up + down) * 6, CHIPTUNE)

def ring3():
    """Ding-dong doorbell — big lazy two-note chime, carries far.

    Swells from ~25% to full volume across the whole tone so it never
    startles — gentle first ding, loud enough for other rooms by the end.
    """
    phrase = [("E5", 1.2), ("C5", 1.6), (None, .4),
              ("G5", 1.2), ("E5", 1.6), (None, .6)]
    samples = synth(phrase * 3, BELL)
    n = len(samples)
    return [s * (0.25 + 0.75 * i / n) for i, s in enumerate(samples)]

def ring4():
    """Cuckoo clock — cheeky falling third, answered an octave up."""
    phrase = [("G5", .35), ("E5", .6), (None, .35),
              ("G5", .35), ("E5", .6), (None, .5),
              ("C6", .35), ("A5", .6), (None, .7)]
    return synth(phrase * 3, MUSIC_BOX)

if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for i, fn in enumerate([ring1, ring2, ring3, ring4], 1):
        path = os.path.join(OUT_DIR, f"ring{i}.wav")
        write(path, fn())
        print(f"{path}: {fn.__doc__.splitlines()[0]}")
