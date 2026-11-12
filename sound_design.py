#!/usr/bin/env python3
"""sound_design.py - a free, local soundtrack for the Short (no API, no quota).

  python3 sound_design.py --plan scene_plan_timed.json --timeline timeline.json --out out/soundtrack.wav [--music-file music/bg.mp3]

Makes ONE audio file as long as the video, with
  * a soft background music bed that follows the mood of every place (happy, calm, warm, sad, tense, night)
  * ambient sound for every look: wind and birds, crickets, rain and thunder, river, crackling fire
  * small sound effects that follow what the animals do: shock sting, hurt thud, celebration chime, sad notes,
    fight whacks, crunching while eating, soft steps and galloping
The file is then mixed under the voice by shorts_assemble.py (the voice ducks it automatically).
Laughing or crying voices cannot be made this way; real music can be added with --music-file (an mp3 of your own).
"""
import argparse, array, json, math, os, random, subprocess, sys, wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shorts_core as core

SR = 22050
TAU = 2 * math.pi


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12.0)


MOOD_OF = {"meadow_day": "happy", "forest_day": "happy", "flower_field": "happy", "golden_sunset": "warm", "riverbank_dawn": "calm",
           "misty_morning": "calm", "autumn_gold": "warm", "snow_pines": "sad", "rainy_forest": "sad", "storm_dusk": "tense",
           "ember_sky": "tense", "night_moon": "night"}
AMBIENT_OF = {"meadow_day": "wind_birds", "forest_day": "wind_birds", "flower_field": "wind_birds", "golden_sunset": "crickets",
              "riverbank_dawn": "river_birds", "misty_morning": "wind", "autumn_gold": "wind", "snow_pines": "wind_strong",
              "rainy_forest": "rain", "storm_dusk": "storm", "ember_sky": "fire", "night_moon": "crickets"}
PROG = {   # chords as MIDI notes
    "happy": [(60, 64, 67), (55, 59, 62), (57, 60, 64), (53, 57, 60)],
    "warm": [(53, 57, 60), (60, 64, 67), (50, 53, 57), (58, 62, 65)],
    "calm": [(60, 64, 67, 71), (57, 60, 64, 67), (53, 57, 60, 64), (55, 59, 62, 69)],
    "sad": [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)],
    "tense": [(57, 60, 64), (59, 62, 65), (57, 60, 64), (52, 56, 59)],
    "night": [(52, 55, 59), (48, 52, 55), (55, 59, 62), (50, 54, 57)],
}
BPM = {"happy": 104, "warm": 84, "calm": 72, "sad": 60, "tense": 76, "night": 66}
ARP_STEP = {"happy": 0.5, "warm": 0.5, "calm": 1.0, "sad": 2.0, "tense": 1.0, "night": 2.0}   # in beats
GAIN = {"happy": 0.9, "warm": 0.9, "calm": 0.8, "sad": 0.7, "tense": 0.8, "night": 0.6}


class Mix:
    def __init__(self, seconds):
        self.n = int(seconds * SR) + SR
        self.d = array.array("d", bytes(8 * self.n))

    def add(self, t, samples, gain=1.0):
        i = int(t * SR)
        if i < 0:
            samples = samples[-i:]
            i = 0
        m = min(len(samples), self.n - i)
        d = self.d
        for k in range(m):
            d[i + k] += samples[k] * gain


def env_fade(n, a, r):
    a, r = max(1, int(a * SR)), max(1, int(r * SR))
    return lambda k: min(1.0, k / a, (n - k) / r)


# ---------------------------------------------------------------- music
def pluck(freq, dur=0.9, amp=0.25):
    n = int(dur * SR)
    return [amp * math.sin(TAU * freq * k / SR) * math.exp(-4.2 * k / SR) * (1 - math.exp(-60 * k / SR)) +
            0.35 * amp * math.sin(TAU * 2 * freq * k / SR) * math.exp(-7 * k / SR) for k in range(n)]


def pad(chord, dur, amp):
    n = int(dur * SR)
    f = env_fade(n, 1.2, 1.5)
    out = [0.0] * n
    for note in chord:
        for detune in (0.997, 1.003):
            fr = hz(note - 12) * detune
            for k in range(n):
                out[k] += math.sin(TAU * fr * k / SR + 0.4 * math.sin(TAU * 0.2 * k / SR))
    s = amp / (len(chord) * 2)
    return [out[k] * s * f(k) for k in range(n)]


def music(mix, t0, t1, mood, rng):
    prog = PROG[mood]
    beat = 60.0 / BPM[mood]
    chord_len = 4 * beat
    t, ci = t0, 0
    while t < t1:
        chord = prog[ci % len(prog)]
        dur = min(chord_len + 1.0, t1 - t + 1.0)
        mix.add(t, pad(chord, dur, 0.5 * GAIN[mood]))
        step = ARP_STEP[mood] * beat
        tt, j = t, 0
        notes = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 24] if mood in ("happy", "warm") else [chord[0] + 12, chord[2] + 12, chord[1] + 24]
        while tt < min(t + chord_len, t1):
            if mood == "tense" and j % 2:
                tt += step
                j += 1
                continue
            if mood == "night" and rng.random() < 0.4:
                tt += step
                j += 1
                continue
            mix.add(tt, pluck(hz(notes[j % len(notes)]), 0.9, 0.22 * GAIN[mood]))
            tt += step
            j += 1
        if mood == "tense":                                   # slow low pulse
            for q in range(int(chord_len / (beat * 2))):
                tp = t + q * beat * 2
                if tp < t1:
                    mix.add(tp, [0.3 * math.sin(TAU * 55 * k / SR) * math.exp(-5 * k / SR) for k in range(int(0.5 * SR))])
        t += chord_len
        ci += 1


# ---------------------------------------------------------------- ambience
def ambience(mix, t0, t1, kind, rng):
    n = int((t1 - t0) * SR)
    f = env_fade(n, 0.8, 0.8)
    out = [0.0] * n
    lp = lp2 = 0.0
    wind = kind in ("wind", "wind_birds", "wind_strong")
    strong = 1.6 if kind == "wind_strong" else 1.0
    for k in range(n):
        x = rng.uniform(-1, 1)
        v = 0.0
        if wind:
            lp += 0.012 * (x - lp)
            v = lp * 5.0 * strong * (0.55 + 0.45 * math.sin(TAU * 0.13 * k / SR + 1.0)) * 0.22
        elif kind in ("rain", "storm"):
            lp += 0.25 * (x - lp)
            v = (x - lp) * (0.16 if kind == "rain" else 0.20)
        elif kind in ("river", "river_birds"):
            lp += 0.15 * (x - lp)
            lp2 += 0.01 * (x - lp2)
            v = (lp - lp2) * 0.35
        elif kind == "fire":
            lp += 0.03 * (x - lp)
            v = lp * 2.0 * 0.15
        out[k] = v * f(k)
    mix.add(t0, out)
    dur = t1 - t0
    if kind in ("wind_birds", "river_birds"):                  # bird chirps
        tt = rng.uniform(0.3, 1.5)
        while tt < dur - 0.3:
            base = rng.uniform(2400, 3800)
            for rep in range(rng.randint(1, 3)):
                L = int(0.09 * SR)
                ch = [0.05 * math.sin(TAU * (base + 700 * math.sin(TAU * 9 * k / SR)) * k / SR) * math.sin(math.pi * k / L) for k in range(L)]
                mix.add(t0 + tt + rep * 0.13, ch)
            tt += rng.uniform(1.2, 4.0)
    if kind == "crickets":
        L = int(dur * SR)
        ch = [0.035 * math.sin(TAU * 4300 * k / SR) * (1.0 if math.sin(TAU * 13 * k / SR) > 0.55 else 0.0) *
              (1.0 if math.sin(TAU * 0.45 * k / SR) > -0.35 else 0.0) * f(k) for k in range(L)]
        mix.add(t0, ch)
    if kind in ("storm",):                                      # thunder
        tt = rng.uniform(2, 6)
        while tt < dur - 3:
            m = int(2.8 * SR)
            lpv, th = 0.0, []
            for k in range(m):
                lpv += 0.025 * (rng.uniform(-1, 1) - lpv)
                th.append(lpv * 9.0 * math.exp(-1.4 * k / SR) * min(1.0, k / (0.08 * SR)) * 0.4)
            mix.add(t0 + tt, th)
            tt += rng.uniform(14, 26)
    if kind == "fire":                                          # crackles
        tt = 0.0
        while tt < dur:
            m = int(0.03 * SR)
            mix.add(t0 + tt, [rng.uniform(-1, 1) * 0.12 * math.exp(-6 * k / m) for k in range(m)])
            tt += rng.uniform(0.05, 0.5)


# ---------------------------------------------------------------- effects
def noise(dur, amp, rng, decay=8.0):
    n = int(dur * SR)
    return [rng.uniform(-1, 1) * amp * math.exp(-decay * k / n) for k in range(n)]


def sweep(f0, f1, dur, amp, decay=6.0):
    n = int(dur * SR)
    ph, out = 0.0, []
    for k in range(n):
        fr = f0 + (f1 - f0) * k / n
        ph += TAU * fr / SR
        out.append(amp * math.sin(ph) * math.exp(-decay * k / n))
    return out


def sfx(mix, kind, t, rng, length=0.0):
    if kind == "shock":
        mix.add(t, sweep(900, 400, 0.28, 0.28))
    elif kind == "hurt":
        mix.add(t, sweep(120, 45, 0.3, 0.55))
        mix.add(t, noise(0.06, 0.25, rng, 10))
    elif kind == "celebrate":
        for q, m in enumerate((84, 88, 91, 96)):
            mix.add(t + q * 0.09, pluck(hz(m), 0.6, 0.20))
    elif kind == "sad":
        for q, m in enumerate((64, 62, 60)):
            n = int(0.45 * SR)
            mix.add(t + q * 0.42, [0.14 * math.sin(TAU * hz(m) * k / SR + 2 * math.sin(TAU * 5 * k / SR)) * math.sin(math.pi * k / n) for k in range(n)])
    elif kind in ("fight", "kick"):
        for q in range(int(max(1, min(length, 2.0) / 0.7))):
            mix.add(t + q * 0.7, noise(0.07, 0.45, rng, 9))
            mix.add(t + q * 0.7, sweep(150, 60, 0.18, 0.5))
    elif kind == "eat":
        for q in range(int(min(length, 3.0) / 0.32)):
            mix.add(t + q * 0.32, noise(0.05, 0.16, rng, 8))
    elif kind == "run":
        for q in range(int(length * 3.2)):
            mix.add(t + q * 0.31, sweep(110, 60, 0.12, 0.30, 8))
            mix.add(t + q * 0.31 + 0.12, sweep(100, 55, 0.1, 0.22, 8))
    elif kind == "walk":
        for q in range(int(length * 1.7)):
            mix.add(t + q * 0.59, sweep(95, 60, 0.1, 0.16, 8))


def cue(mix, kind, t, t_end, rng):
    """Story sounds placed on the shot: a coin jingle when money matters, a boom for a shock, a riser into a twist,
    a heartbeat for fear, a splash, a whoosh, an alarm bell."""
    if kind == "coin":
        for q, m in enumerate((88, 95, 100)):
            mix.add(t + q * 0.11, pluck(hz(m), 0.7, 0.26))
    elif kind == "boom":
        mix.add(t, sweep(90, 28, 0.9, 0.75, 4.0))
        mix.add(t, noise(0.18, 0.35, rng, 7))
    elif kind == "riser":
        length = min(2.2, max(0.8, t_end - t))
        st = max(t, t_end - length)
        n = int(length * SR)
        mix.add(st, [0.22 * (k / n) * math.sin(TAU * (180 + 700 * (k / n) ** 2) * k / SR) for k in range(n)])
        mix.add(st, noise(length, 0.12, rng, 0.5))
    elif kind == "heartbeat":
        for q in range(int(min(6.0, max(1.2, t_end - t)) / 0.8)):
            mix.add(t + q * 0.8, sweep(70, 40, 0.14, 0.6, 9))
            mix.add(t + q * 0.8 + 0.2, sweep(60, 38, 0.12, 0.42, 9))
    elif kind == "splash":
        mix.add(t, noise(0.45, 0.4, rng, 5))
        mix.add(t, sweep(600, 200, 0.3, 0.2))
    elif kind == "whoosh":
        mix.add(t, noise(0.5, 0.22, rng, 3.0))
        mix.add(t, sweep(300, 1200, 0.45, 0.14, 4.0))
    elif kind == "alarm":
        for q in range(4):
            mix.add(t + q * 0.3, pluck(hz(96 if q % 2 == 0 else 91), 0.5, 0.28))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_timed.json")
    ap.add_argument("--timeline", default="timeline.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--music-file", default="", help="optional mp3 of your own, mixed in as the music bed")
    ap.add_argument("--no-music", action="store_true", help="no synthesized music (only ambience and effects)")
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    plan = json.load(open(a.plan, encoding="utf-8"))
    tl = json.load(open(a.timeline, encoding="utf-8"))
    total = float(tl["total_seconds"])
    rng = random.Random(a.seed)
    shots = plan["shots"]
    starts = {t["id"]: (t["start_time"], t["duration"]) for t in tl["shots"]}

    parts = {"music": Mix(total), "amb": Mix(total), "sfx": Mix(total)}
    runs = []                                                   # runs of equal look and equal fire state
    for s in shots:
        st, du = starts[s["id"]]
        fire = "fire" in (s.get("fx") or [])
        if runs and runs[-1]["look"] == s["look"] and runs[-1]["fire"] == fire:
            runs[-1]["t1"] = st + du
        else:
            runs.append({"look": s["look"], "fire": fire, "t0": st, "t1": st + du})
    for idx, r in enumerate(runs):
        mood = "tense" if r["fire"] else MOOD_OF.get(r["look"], "happy")
        amb = "fire" if r["fire"] else AMBIENT_OF.get(r["look"], "wind")
        if not a.no_music and not a.music_file:
            music(parts["music"], r["t0"], r["t1"], mood, rng)
        ambience(parts["amb"], r["t0"], r["t1"], amb, rng)
        if idx > 0 and not a.no_sfx:                            # a whoosh into every new place or time
            cue(parts["sfx"], "whoosh", max(0.0, r["t0"] - 0.15), r["t0"] + 0.3, rng)
        print(f"  {r['t0']:6.1f}-{r['t1']:6.1f}s {r['look']:15s} music: {mood:6s} ambience: {amb}", flush=True)
    last_t = {}
    if not a.no_sfx:
        for s in shots:
            st, du = starts[s["id"]]
            for c in s["characters"]:
                for seg in core.build_schedule(c, du):
                    act = seg["action"]
                    if act in ("idle", "listen", "talk"):
                        continue
                    t = st + seg["t0"]
                    if t - last_t.get((act,), -9) < 0.8 and act not in ("run", "walk"):
                        continue
                    last_t[(act,)] = t
                    sfx(parts["sfx"], act, t, rng, seg["t1"] - seg["t0"])

    if not a.no_sfx:
        for s in shots:
            c = s.get("cue")
            if c:
                st, du = starts[s["id"]]
                cue(parts["sfx"], c, st + 0.05, st + du, rng)
                print(f"  cue {c} in shot {s['id']}", flush=True)

    n = parts["music"].n
    final = array.array("d", bytes(8 * n))
    for name, g in (("music", 0.45), ("amb", 0.5), ("sfx", 1.0)):
        d = parts[name].d
        for k in range(n):
            final[k] += d[k] * g
    peak = max(max(final), -min(final), 1e-6)
    scale = 0.9 / peak
    tmp = a.out + ".tmp.wav"
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    samples = int(total * SR)
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(array.array("h", [int(max(-1.0, min(1.0, final[k] * scale)) * 32767) for k in range(samples)]).tobytes())
    if a.music_file and os.path.exists(a.music_file) and not a.no_music:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-stream_loop", "-1", "-i", a.music_file, "-filter_complex",
                        "[1:a]volume=0.5,aformat=sample_rates=22050:channel_layouts=mono[m];[0:a][m]amix=inputs=2:duration=first:normalize=0",
                        "-ar", str(SR), "-ac", "1", "-t", f"{total:.3f}", a.out], check=True)
        os.remove(tmp)
    else:
        os.replace(tmp, a.out)
    print(f"DONE: {a.out} ({total:.1f} s)")


if __name__ == "__main__":
    main()
