#!/usr/bin/env python3
"""voice_maker.py - narration for the whole story with Gemini TTS, and the exact timeline.

  python3 voice_maker.py --plan plans/episode_x.json --gender male --fps 15 --quality high

Voice modes (--mode):
  auto       (default) ONE call for the whole story. After cutting it into shots at the pauses, every cut
             is checked. If any cut is doubtful, those shots are voiced again in smaller groups (about 4
             calls), and if still doubtful one call per shot. So you get the smooth single voice when it is
             reliable, and perfect alignment when it is not.
  single / chunks / per_shot   force one level.
Free-tier safety: waits --min-interval seconds between calls to one model (3 per minute = 22 s), obeys
retry-after, treats "per day" limits by switching to the next Gemini model, retries the odd
"model tried to generate text" error with another prompt, keeps finished shots (re-run = only missing).

The timeline makes the video EXACTLY --target seconds (default 150): the voice speed is changed by at
most +-8% and the pauses are stretched, never more. If the story is too short or too long for that, it
stops with a clear message so the director can write it again.

Writes: scene_plan_timed.json, timeline.json, voice_report.md, audio/shot_XX.mp3
"""
import argparse, asyncio, base64, hashlib, json, math, os, re, subprocess, sys, time, urllib.error, urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL_ORDER = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts",
               "gemini-3.8-flash-lite-tts", "gemini-2.5-pro-preview-tts"]
GEMINI_VOICES = {"male": "Charon", "female": "Kore"}
EDGE_VOICES = {("English", "male"): "en-IN-PrabhatNeural", ("English", "female"): "en-IN-NeerjaNeural",
               ("Hindi", "male"): "hi-IN-MadhurNeural", ("Hindi", "female"): "hi-IN-SwaraNeural"}
QUALITY = {"fast": (540, 960, 8), "balanced": (720, 1280, 16), "high": (720, 1280, 32), "ultra": (1080, 1920, 28)}
LEAD = 0.25

SLEEP = time.sleep
LAST_CALL = {}
DEAD = set()


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class Fatal(Exception):
    pass


# ------------------------------------------------------------------ audio helpers
def probe(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", path]).decode().strip())


def trim_edges(src, dst):
    """Cut the silence at the start and end (keeps a short natural margin)."""
    af = ("silenceremove=start_periods=1:start_silence=0.08:start_threshold=-45dB,areverse,"
          "silenceremove=start_periods=1:start_silence=0.12:start_threshold=-45dB,areverse")
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", af,
                           "-ar", "24000", "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "2", dst])
    if os.path.getsize(dst) < 1000 or probe(dst) < 0.3:       # trimming removed everything: keep the original
        subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "24000", "-ac", "1",
                               "-codec:a", "libmp3lame", "-q:a", "2", dst])


def find_silences(path, noise="-35dB", dmin=0.2):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", f"silencedetect=noise={noise}:d={dmin}",
                        "-f", "null", "-"], capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", p.stderr)]
    ends = [(float(x), float(y)) for x, y in re.findall(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", p.stderr)]
    return [(max(st, 0.0), en, ln) for st, (en, ln) in zip(starts, ends)]


def choose_cuts(dur, silences, counts):
    """counts = characters per shot in the group. Returns (cuts, quality) quality = list of 'ok' or a reason."""
    n = len(counts)
    if n <= 1:
        return [], []
    total_sil = sum(s[2] for s in silences)
    speech_total = max(dur - total_sil, 0.1)
    tot = float(sum(counts))
    cands, before = [], 0.0
    for st, en, ln in silences:
        cands.append({"t": (st + en) / 2, "len": ln, "sp": (st + en) / 2 - before - ln / 2})
        before += ln
    window = max(1.8, 0.25 * speech_total / n)
    cuts, quality, prev, cum = [], [], 0.0, 0.0
    for k in range(1, n):
        cum += counts[k - 1]
        exp_sp = speech_total * cum / tot
        best, bs = None, None
        for c in cands:
            if c["t"] <= prev + 0.8 or abs(c["sp"] - exp_sp) > window:
                continue
            score = abs(c["sp"] - exp_sp) - 6.0 * min(c["len"], 1.0)
            if bs is None or score < bs:
                best, bs = c, score
        if best is None:
            quality.append("no pause near the expected place")
            t = max(prev + 0.8, min(dur - 0.8 * (n - k), dur * cum / tot))
            cuts.append(t)
            prev = t
        else:
            quality.append("ok" if best["len"] >= 0.30 else f"weak pause {best['len']:.2f}s")
            cuts.append(best["t"])
            prev = best["t"]
    return cuts, quality


def cut_audio(src, start, end, dst):
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                           "-ar", "24000", "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "2", dst])


# ------------------------------------------------------------------ Gemini TTS
def gcall(model, body, key):
    req = urllib.request.Request(f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        wait = None
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
        if m:
            wait = float(m.group(1))
        retry = e.code in (408, 429, 500, 502, 503, 504) or (e.code == 400 and "generate text" in text)
        raise GErr(f"HTTP {e.code}: {text[:250]}", e.code, retry, wait, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GErr(f"network: {e}", None, True, None, "")


def model_chain(key, override):
    if override:
        return [m.strip() for m in override.split(",") if m.strip()]
    seen = []
    try:
        req = urllib.request.Request(f"{BASE}/models?pageSize=200", headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            seen = [m["name"].split("/")[-1] for m in json.load(r).get("models", []) if "tts" in m["name"].lower()]
    except Exception as e:
        print(f"  could not list Gemini models: {e}", file=sys.stderr)
    if not seen:
        return MODEL_ORDER[:4]
    chain = [m for m in MODEL_ORDER if m in seen]
    return chain + [m for m in seen if m not in chain]


def prompts(text, language, multi):
    if language == "Hindi":
        style = "Read this Hindi story slowly and warmly, like a storyteller"
    else:
        style = "Read this story aloud warmly and expressively, like a children's storyteller, at a calm natural pace"
    if multi:
        return [f"{style}, with a clear short pause between paragraphs:\n\n{text}",
                f"Say with a short pause between paragraphs:\n\n{text}", text]
    return [f"{style}: {text}", f"Say: {text}", text]


def synth(text, voice, language, multi, chain, a, path):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise Fatal("GEMINI_API_KEY is empty. Add it under Settings > Secrets > Actions.")
    last = None
    tries = min(a.retries, 3) if multi else a.retries
    for model in chain:
        if model in DEAD:
            continue
        for attempt in range(1, tries + 1):
            body = {"contents": [{"parts": [{"text": prompts(text, language, multi)[(attempt - 1) % 3]}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
            gap = a.min_interval - (time.time() - LAST_CALL.get(model, 0))
            if gap > 0:
                SLEEP(gap)
            try:
                out = gcall(model, body, key)
                LAST_CALL[model] = time.time()
                cands = out.get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
                if not inline:
                    raise GErr("no audio in the answer", 200, True)
                pcm = base64.b64decode(inline["data"])
                if len(pcm) < 4000:
                    raise GErr("audio too short", 200, True)
                rate = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                raw = path + ".pcm"
                open(raw, "wb").write(pcm)
                subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", rate.group(1) if rate else "24000",
                                       "-ac", "1", "-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "24000",
                                       "-codec:a", "libmp3lame", "-q:a", "2", path])
                os.remove(raw)
                words = max(1, len(text.split()))
                cps = len(text) / max(probe(path), 0.1)
                if multi and not (4.0 <= cps <= 30.0):
                    raise GErr(f"audio length looks wrong ({cps:.1f} characters per second)", 200, True)
                return model
            except GErr as e:
                LAST_CALL[model] = time.time()
                last = e
                t = (e.text or str(e))
                if "API key not valid" in t or e.code in (401, 403):
                    raise Fatal(f"Gemini rejected the key or access: {str(e)[:200]}")
                if e.code == 404 or (e.code == 429 and re.search(r"per ?day", t, re.I)):
                    DEAD.add(model)
                    print(f"  {model}: not usable now (404 or daily limit), next model", file=sys.stderr)
                    break
                if not e.retryable:
                    break
                wait = min(90, max(e.wait or 0, a.min_interval) + 2) if e.code == 429 else min(60, 4 * attempt * attempt)
                print(f"  {model}: attempt {attempt}/{tries} failed ({str(e)[:90]}), waiting {wait:.0f}s", file=sys.stderr)
                if attempt < tries:
                    SLEEP(wait)
    raise RuntimeError(f"all Gemini models failed: {last}")


async def _edge(text, voice, path):
    import edge_tts
    last = None
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, voice, rate="-4%").save(path)
            if os.path.getsize(path) > 1000:
                return
        except Exception as e:
            last = e
        await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"Edge TTS failed: {last}")


# ------------------------------------------------------------------ plan helpers
def fingerprint(text, voice, language):
    return hashlib.sha1(f"{text}|{voice}|{language}".encode("utf-8")).hexdigest()


def chunk(items, g):
    g = max(1, min(g, len(items)))
    total = sum(len(t) for _, t in items)
    target, groups, cur, acc = total / g, [], [], 0
    for it in items:
        cur.append(it)
        acc += len(it[1])
        if acc >= target and len(groups) < g - 1:
            groups.append(cur)
            cur, acc = [], 0
    if cur:
        groups.append(cur)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--gender", default="male", choices=["male", "female"])
    ap.add_argument("--voice", default="")
    ap.add_argument("--mode", default="auto", choices=["auto", "single", "chunks", "per_shot"])
    ap.add_argument("--target", type=float, default=0.0, help="video length in seconds (default: from the plan, 150)")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", default="high", choices=list(QUALITY))
    ap.add_argument("--models", default="")
    ap.add_argument("--min-interval", dest="min_interval", type=float, default=22.0)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--edge-fallback", action="store_true", help="use the Microsoft voice for a shot Gemini cannot voice")
    ap.add_argument("--no-reuse", dest="reuse", action="store_false")
    ap.add_argument("--audio-dir", default="audio")
    ap.add_argument("--out-plan", default="scene_plan_timed.json")
    ap.add_argument("--timeline", default="timeline.json")
    ap.add_argument("--report", default="voice_report.md")
    a = ap.parse_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    shots = plan["shots"]
    language = plan.get("language", "English")
    target = a.target or float(plan.get("target_seconds", 150))
    voice = a.voice or GEMINI_VOICES[a.gender]
    os.makedirs(a.audio_dir, exist_ok=True)
    n = len(shots)

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("FAIL: GEMINI_API_KEY is empty. Add it under Settings > Secrets > Actions.")
    chain = model_chain(key, a.models)
    print("Gemini TTS model order:", ", ".join(chain), "| voice:", voice, "| mode:", a.mode)

    def path_of(i):
        return os.path.join(a.audio_dir, f"shot_{i:02d}.mp3")

    def meta_of(i):
        return os.path.join(a.audio_dir, f"shot_{i:02d}.json")

    engines, pending, notes = {}, [], []
    for i, s in enumerate(shots, start=1):
        text = s["narration"].strip()
        ok = False
        if a.reuse and os.path.exists(path_of(i)) and os.path.exists(meta_of(i)):
            try:
                m = json.load(open(meta_of(i)))
                ok = m.get("fp") == fingerprint(text, voice, language)
                if ok:
                    engines[i] = m.get("engine", "kept") + " (kept)"
            except Exception:
                ok = False
        if not ok:
            pending.append((i, text))

    def commit(i, text, engine):
        json.dump({"fp": fingerprint(text, voice, language), "engine": engine}, open(meta_of(i), "w"))
        engines[i] = engine

    # ---- voice levels
    n_chunks = max(1, math.ceil(len(pending) / 5)) if pending else 1
    levels = {"auto": [1, n_chunks, len(pending)], "single": [1], "chunks": [n_chunks], "per_shot": [len(pending)]}[a.mode]
    levels = [g for k, g in enumerate(levels) if g >= 1 and g not in levels[:k]]
    for g in levels:
        if not pending:
            break
        groups = chunk(pending, g)
        print(f"Voice level: {len(pending)} shots in {len(groups)} call(s)", flush=True)
        failed = []
        for gi, grp in enumerate(groups, start=1):
            texts = [t for _, t in grp]
            gpath = os.path.join(a.audio_dir, f"group_{gi:02d}_of_{len(groups)}.mp3")
            try:
                model = synth("\n\n".join(texts), voice, language, len(grp) > 1, chain, a, gpath)
            except Fatal as e:
                sys.exit(f"FAIL: {e}")
            except Exception as e:
                print(f"  group {gi}/{len(groups)}: {str(e)[:140]}", flush=True)
                failed.extend(grp)
                continue
            if len(grp) == 1:
                i, text = grp[0]
                trim_edges(gpath, path_of(i))
                commit(i, text, f"gemini:{model} [1 shot/call]")
                continue
            dur = probe(gpath)
            cuts, quality = choose_cuts(dur, find_silences(gpath), [len(t) for t in texts])
            if any(q != "ok" for q in quality):
                bad = [f"{grp[k][0]}|{grp[k + 1][0]}: {q}" for k, q in enumerate(quality) if q != "ok"]
                notes.append(f"{len(groups)}-call level, call {gi}: doubtful cuts ({'; '.join(bad)}), voiced again in smaller groups")
                print("  doubtful cuts:", "; ".join(bad), flush=True)
                failed.extend(grp)
                continue
            bounds = [0.0] + cuts + [dur]
            for (i, text), st, en in zip(grp, bounds[:-1], bounds[1:]):
                tmp = path_of(i) + ".cut.mp3"
                cut_audio(gpath, st, en, tmp)
                trim_edges(tmp, path_of(i))
                os.remove(tmp)
                commit(i, text, f"gemini:{model} [{len(groups)} call{'s' if len(groups) > 1 else ''}]")
        pending = failed

    if pending and a.edge_fallback:
        print(f"Using the Microsoft voice for shots {[i for i, _ in pending]}", flush=True)
        evoice = EDGE_VOICES.get((language, a.gender), "en-IN-PrabhatNeural")
        still = []
        for i, text in pending:
            try:
                tmp = path_of(i) + ".edge.mp3"
                asyncio.run(_edge(text, evoice, tmp))
                trim_edges(tmp, path_of(i))
                os.remove(tmp)
                commit(i, text, f"edge:{evoice}")
            except Exception as e:
                print(f"  shot {i}: Edge failed too: {e}")
                still.append((i, text))
        pending = still

    missing = [i for i, _ in pending]
    if missing:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(f"### Voice report\n\n**MISSING VOICE FOR SHOTS {missing}**\n\nFinished shots are kept. Run again later "
                    f"(daily limits reset) and only these shots are made.\n\n" + "\n".join(f"- {x}" for x in notes) + "\n")
        sys.exit(f"FAIL: no voice for shots {missing}. Run again later, the finished shots are kept.")

    # ---- exact timing: the video is exactly `target` seconds
    voice_s = [probe(path_of(i)) for i in range(1, n + 1)]
    L = sum(voice_s)
    pad_lo, pad_hi = 0.25, 1.4                  # tail pause per shot (s); the lead is fixed
    base = n * LEAD
    f = 1.0
    need = target - base                        # time for speech plus tails
    if L + n * pad_lo > need * 1.0001:          # voice too long: speed up
        f = min(1.08, (L) / (need - n * pad_lo))
    elif L + n * pad_hi < need * 0.9999:        # voice too short: slow down
        f = max(0.92, L / (need - n * pad_hi))
    speech = L / f
    tail_total = need - speech
    tail = tail_total / n
    if not (pad_lo - 1e-6 <= tail <= pad_hi + 1e-6):
        direction = "long" if tail < pad_lo else "short"
        raise SystemExit(f"FAIL: the narration is too {direction} for {target:.0f} s: voice {L:.1f} s (speed x{f:.2f}) leaves "
                         f"{tail:.2f} s pause per shot, allowed {pad_lo}-{pad_hi}. Write the story again "
                         f"{'shorter' if direction == 'long' else 'longer'} (the director does this automatically).")
    w, h, samples = QUALITY[a.quality]
    cum, tl_shots = 0.0, []
    total_frames = int(round(target * a.fps))
    for i, s in enumerate(shots, start=1):
        d = LEAD + voice_s[i - 1] / f + tail
        start_f = int(round(cum * a.fps))
        cum += d
        end_f = total_frames if i == n else int(round(cum * a.fps))
        dur = (end_f - start_f) / a.fps
        s.update({"audio": path_of(i), "voice_seconds": round(voice_s[i - 1], 3), "lead": LEAD, "duration": round(dur, 3),
                  "start_time": round(start_f / a.fps, 3), "start_frame": start_f, "end_frame": end_f})
        tl_shots.append({"id": s["id"], "audio": path_of(i), "voice_seconds": round(voice_s[i - 1], 3), "lead": LEAD,
                         "duration": round(dur, 3), "start_time": round(start_f / a.fps, 3), "start_frame": start_f,
                         "end_frame": end_f, "thumb": bool(s.get("thumb"))})
    plan.update({"fps": a.fps, "total_frames": total_frames, "total_seconds": target, "voice_tempo": round(f, 4)})
    json.dump(plan, open(a.out_plan, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"fps": a.fps, "width": w, "height": h, "samples": samples, "aspect": "vertical",
               "total_frames": total_frames, "total_seconds": target, "tempo": round(f, 4), "lead": LEAD, "shots": tl_shots},
              open(a.timeline, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(a.report, "w", encoding="utf-8") as fh:
        fh.write(f"### Voice report\n\n**ALL {n} SHOTS HAVE A VOICE. VIDEO LENGTH = {target:.1f} s ({total_frames} frames at {a.fps} fps)**\n\n")
        fh.write(f"Voice: {voice} | mode: {a.mode} | speech {L:.1f} s, speed x{f:.3f}, pause after each shot {tail:.2f} s, "
                 f"picture {w}x{h}, {samples} samples\n\n")
        if notes:
            fh.write("Notes:\n" + "\n".join(f"- {x}" for x in notes) + "\n\n")
        fh.write("| Shot | Voice engine | Voice s | Shot s |\n|---|---|---|---|\n")
        for i, s in enumerate(shots, start=1):
            fh.write(f"| {i} | {engines.get(i, '?')} | {voice_s[i - 1]:.1f} | {s['duration']:.1f} |\n")
    print(f"Done: {n} shots, speech {L:.1f}s x{f:.3f}, total {target:.1f}s, {total_frames} frames")


if __name__ == "__main__":
    main()
