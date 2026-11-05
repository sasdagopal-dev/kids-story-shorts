#!/usr/bin/env python3
"""Hindi narration for every shot. Built for the Gemini FREE tier (3 requests per minute).

  python3 tts.py --plan P.json --gender male --engine gemini_only --rate=-8% [--on-fail stop|silent]

Single-voice mode: --calls 1 (default) sends the WHOLE story to Gemini in ONE call, so you get
one consistent voice and use 1 request instead of 24. The audio is then cut into shots at the
pauses between paragraphs. If a call fails on every model, it automatically splits the story into
3, then 6, then more calls. --calls 0 = one call per shot (old way).

Engines:  gemini_only        Gemini only (retries, pacing, tries other Gemini models). No Edge.
          gemini_then_edge   Gemini first, Edge only if every Gemini model fails for a shot.
          edge | auto        Edge (auto adds Gemini if a key exists)

What makes it safe on the free tier:
  * waits --min-interval seconds between calls to the same model (default 24 s = 2.5 per minute)
  * on a "per minute" 429 it waits the time Google asks and tries again
  * on a "per day" 429 it marks that model used up and moves to the next Gemini model
  * a 400 "model tried to generate text" error is retried with a different prompt
  * finished shots are kept in audio/ with a fingerprint, so a re-run (or a cache restore)
    only makes the missing shots
  * with --on-fail stop, every shot is tried, all outputs are written, and then the exit code
    is 1 if any shot has no voice

Outputs: scene_plan_timed.json, timeline.json, subs.srt, tts_summary.md,
         narration_preview.mp3, audio/shot_XX.mp3 (+ shot_XX.json fingerprint)
"""
import argparse, asyncio, base64, hashlib, json, math, os, re, subprocess, sys
import time, urllib.error, urllib.request

LEAD = 0.30
TAIL = 0.55
CHARS_PER_SEC = 12.0

EDGE_VOICES = {"male": "hi-IN-MadhurNeural", "female": "hi-IN-SwaraNeural"}
GEMINI_VOICES = {"male": "Charon", "female": "Kore"}
MODEL_ORDER = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview",
               "gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts",
               "gemini-2.5-pro-preview-tts"]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

TEXT_KEYS = ("narration", "text", "voiceover", "voice_over", "dialogue")
LIST_KEYS = ("shots", "scenes")
QUALITY = {"fast": (640, 360, 8), "balanced": (960, 540, 16), "high": (1280, 720, 24)}

SLEEP = time.sleep
LAST_CALL = {}      # model -> time of its last call
EXHAUSTED = set()   # models whose daily quota (or access) is used up in this run
MODELS_USED = {}    # model -> number of shots it voiced


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class Fatal(Exception):
    pass


def find_shots(plan):
    if isinstance(plan, list):
        return plan
    for k in LIST_KEYS:
        if isinstance(plan.get(k), list):
            return plan[k]
    sys.exit(f"Plan has no list called one of {LIST_KEYS}. Keys found: {list(plan)}")


def get_text(shot):
    for k in TEXT_KEYS:
        v = shot.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", path])
    return float(out.decode().strip())


# ---------------------------------------------------------------- Edge
async def _edge(text, voice, rate, path):
    import edge_tts
    last = None
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)
            if os.path.getsize(path) > 1000:
                return
        except Exception as e:
            last = e
        await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"Edge TTS failed: {last}")


def edge_synth(text, a, path):
    asyncio.run(_edge(text, EDGE_VOICES[a.gender], a.rate, path))


# ---------------------------------------------------------------- Gemini
def gcall(model, body, key):
    req = urllib.request.Request(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        wait = None
        ra = e.headers.get("Retry-After")
        if ra and ra.isdigit():
            wait = int(ra)
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
        if m:
            wait = float(m.group(1))
        retryable = e.code in (408, 429, 500, 502, 503, 504) or (
            e.code == 400 and "generate text" in text)
        raise GErr(f"HTTP {e.code}: {text[:250]}", e.code, retryable, wait, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GErr(f"network: {e}", None, True, None, "")


def list_tts_models(key):
    try:
        req = urllib.request.Request(f"{GEMINI_BASE}/models?pageSize=200",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        return [m["name"].split("/")[-1] for m in data.get("models", [])
                if "tts" in m["name"].lower()]
    except Exception as e:
        print(f"  could not list Gemini models: {e}", file=sys.stderr)
        return []


def build_chain(a, key):
    if a.gemini_models:
        return [m.strip() for m in a.gemini_models.split(",") if m.strip()]
    seen = list_tts_models(key)
    if not seen:
        return list(MODEL_ORDER[:4])
    chain = [m for m in MODEL_ORDER if m in seen]
    chain += [m for m in seen if m not in chain]
    return chain


def _pace_hint(rate):
    m = re.match(r"^([+-]?\d+)%$", (rate or "").strip())
    n = int(m.group(1)) if m else 0
    if n <= -10:
        return "very slowly"
    if n < 0:
        return "slowly"
    if n > 5:
        return "a little faster than normal"
    return "at a calm, natural pace"


def prompts(text, a, multi=False):
    pace = _pace_hint(a.rate)
    if multi:
        return [f"Say {pace} and warmly, like a storyteller, with a clear short pause between paragraphs:\n\n{text}",
                f"Say with a short pause between paragraphs:\n\n{text}",
                text]
    return [f"Say {pace} and warmly, like a storyteller: {text}",
            f"Say: {text}",
            text]


def is_daily(err):
    t = (err.text or str(err)).lower()
    return "perday" in t or "per day" in t or "daily" in t


def pace_wait(model, interval):
    gap = interval - (time.time() - LAST_CALL.get(model, 0))
    if gap > 0:
        SLEEP(gap)


def gemini_synth(text, a, path, chain, multi=False):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise Fatal("GEMINI_API_KEY is not set")
    voice = a.gemini_voice or GEMINI_VOICES[a.gender]
    last = None
    for model in chain:
        if model in EXHAUSTED:
            continue
        tries = min(a.retries, 3) if multi else a.retries
        for attempt in range(1, tries + 1):
            prompt = prompts(text, a, multi)[(attempt - 1) % 3]
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
            pace_wait(model, a.min_interval)
            try:
                out = gcall(model, body, key)
                LAST_CALL[model] = time.time()
                cands = out.get("candidates") or []
                parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
                inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
                if not inline:
                    raise GErr("no audio in the answer", 200, True)
                pcm = base64.b64decode(inline["data"])
                if len(pcm) < 4000:
                    raise GErr("audio too short", 200, True)
                m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                rate = int(m.group(1)) if m else 24000
                raw = path + ".pcm"
                with open(raw, "wb") as f:
                    f.write(pcm)
                subprocess.check_call([
                    "ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
                    "-ac", "1", "-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "24000", "-codec:a", "libmp3lame", path])
                os.remove(raw)
                if os.path.getsize(path) < 1000:
                    raise GErr("converted audio too small", 200, True)
                if multi:
                    cps = len(text) / max(probe(path), 0.1)
                    if not (5.0 <= cps <= 30.0):
                        raise GErr(f"audio length looks wrong ({cps:.1f} characters per second)", 200, True)
                return model
            except GErr as e:
                LAST_CALL[model] = time.time()
                last = e
                body_text = (e.text or str(e))
                if "API key not valid" in body_text or "API_KEY_INVALID" in body_text:
                    raise Fatal("Gemini says the API key is not valid. Update the GEMINI_API_KEY secret.")
                if e.code in (401, 403):
                    raise Fatal(f"Gemini rejected the key or access: {str(e)[:200]}")
                if e.code == 404:
                    EXHAUSTED.add(model)
                    print(f"  {model}: not found, skipping it", file=sys.stderr)
                    break
                if e.code == 429 and is_daily(e):
                    EXHAUSTED.add(model)
                    print(f"  {model}: daily quota used up, moving to the next model", file=sys.stderr)
                    break
                if not e.retryable:
                    print(f"  {model}: {str(e)[:200]} (not retryable here, next model)", file=sys.stderr)
                    break
                wait = min(90, max(e.wait or 0, a.min_interval) + 2) if e.code == 429 \
                    else min(60, 4 * attempt * attempt)
                print(f"  {model}: attempt {attempt}/{tries} failed ({str(e)[:90]}), "
                      f"waiting {wait:.0f}s", file=sys.stderr)
                if attempt < tries:
                    SLEEP(wait)
    raise RuntimeError(f"all Gemini models failed: {last}")


# ---------------------------------------------------------------- choose engine
def make_audio(text, a, path, chain, skip_gemini=False):
    has_key = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    order = {
        "edge": ["edge"],
        "gemini_only": ["gemini"],
        "gemini": ["gemini"],
        "auto": ["edge"] + (["gemini"] if has_key else []),
        "gemini_then_edge": (["gemini"] if has_key else []) + ["edge"],
    }[a.engine]
    if skip_gemini:
        order = [n for n in order if n != "gemini"]
    for name in order:
        try:
            if name == "edge":
                edge_synth(text, a, path)
                used = "edge"
            else:
                used = "gemini:" + gemini_synth(text, a, path, chain)
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return used
        except Fatal:
            raise
        except Exception as e:
            print(f"  {name} failed: {e}", file=sys.stderr)
    return None


def shot_fingerprint(text, a):
    voice = a.gemini_voice or GEMINI_VOICES[a.gender]
    fam = "edge" if a.engine == "edge" else "gemini"
    return hashlib.sha1(f"{text}|{a.gender}|{voice}|{fam}".encode("utf-8")).hexdigest()


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def split_lines(text, max_chars=46):
    parts = [p.strip() for p in re.split(r"(?<=[।\.\?!,])\s+", text) if p.strip()]
    merged = []
    for p in parts:
        if merged and (len(merged[-1]) < 14 or len(merged[-1]) + len(p) + 1 <= max_chars):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged



# ---------------------------------------------------------------- single-voice (group) mode
def find_silences(path, noise="-35dB", dmin=0.25):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af",
                        f"silencedetect=noise={noise}:d={dmin}", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", p.stderr)]
    ends = [(float(x), float(y)) for x, y in re.findall(
        r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", p.stderr)]
    out = []
    for st, (en, ln) in zip(starts, ends):
        out.append((max(st, 0.0), en, ln))
    return out


def choose_cuts(dur, silences, counts):
    """counts = characters per narrated shot in the group. Returns n-1 cut times + info."""
    n = len(counts)
    if n <= 1:
        return [], []
    total_chars = float(sum(counts))
    total_sil = sum(s[2] for s in silences)
    speech_total = max(dur - total_sil, 0.1)
    cands = []
    before = 0.0
    for st, en, ln in silences:
        mid = (st + en) / 2
        cands.append({"t": mid, "len": ln, "sp": mid - before - ln / 2})
        before += ln
    cuts, info, prev, cum = [], [], 0.0, 0.0
    for k in range(1, n):
        cum += counts[k - 1]
        exp_sp = speech_total * cum / total_chars
        best, best_score = None, None
        for c in cands:
            if c["t"] <= prev + 0.8:
                continue
            score = abs(c["sp"] - exp_sp) - 4.0 * min(c["len"], 1.0)
            window = max(3.0, 0.4 * dur / n)
            if abs(c["sp"] - exp_sp) > window:
                continue
            if best is None or score < best_score:
                best, best_score = c, score
        if best is None:
            t = max(prev + 0.8, min(dur - 0.8 * (n - k), dur * cum / total_chars))
            cuts.append(t)
            info.append((t, 0.0, "no pause found, cut by text length"))
            prev = t
        else:
            cuts.append(best["t"])
            note = "ok" if best["len"] >= 0.3 else "weak pause"
            info.append((best["t"], best["len"], note))
            prev = best["t"]
    return cuts, info


def cut_audio(src, start, end, dst):
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ss", f"{start:.3f}",
                           "-to", f"{end:.3f}", "-codec:a", "libmp3lame", "-ar", "24000", dst])


def chunk_by_chars(items, g):
    """items = [(index, text)], split into g contiguous groups with similar characters."""
    g = max(1, min(g, len(items)))
    total = sum(len(t) for _, t in items)
    target = total / g
    groups, cur, acc = [], [], 0
    for it in items:
        cur.append(it)
        acc += len(it[1])
        if acc >= target and len(groups) < g - 1:
            groups.append(cur)
            cur, acc = [], 0
    if cur:
        groups.append(cur)
    return groups


def make_group_voice(a, shots, chain):
    """Voice many shots with few Gemini calls. Returns (done {shot_index: engine}, failed [indices], notes)."""
    items = []
    for i, s in enumerate(shots, start=1):
        text = get_text(s)
        path = os.path.join("audio", f"shot_{i:02d}.mp3")
        meta = os.path.join("audio", f"shot_{i:02d}.json")
        if not text:
            continue
        ok = False
        if a.reuse and os.path.exists(path) and os.path.exists(meta) and os.path.getsize(path) > 1000:
            try:
                ok = json.load(open(meta)).get("fp") == shot_fingerprint(text, a)
            except Exception:
                ok = False
        if not ok:
            items.append((i, text))
    done, notes = {}, []
    remaining = items
    levels = []
    for g in [a.calls, 3, 6, 12, len(items)]:
        if g >= 1 and (not levels or g > levels[-1]):
            levels.append(min(g, max(len(items), 1)))
    levels = sorted(set(levels))
    for g in levels:
        if not remaining:
            break
        groups = chunk_by_chars(remaining, g)
        print(f"Single-voice mode: {len(remaining)} shots in {len(groups)} call(s)", flush=True)
        next_remaining = []
        for gi, grp in enumerate(groups, start=1):
            texts = [t for _, t in grp]
            gpath = os.path.join("audio", f"group_{gi:02d}_of_{len(groups)}.mp3")
            joined = "\n\n".join(texts)
            try:
                model = gemini_synth(joined, a, gpath, chain, multi=len(grp) > 1)
            except Fatal:
                raise
            except Exception as e:
                print(f"  group {gi}/{len(groups)} failed on every model: {str(e)[:150]}", flush=True)
                next_remaining.extend(grp)
                continue
            dur = probe(gpath)
            sil = find_silences(gpath)
            cuts, info = choose_cuts(dur, sil, [len(t) for t in texts])
            bounds = [0.0] + cuts + [dur]
            for (idx, text), st, en in zip(grp, bounds[:-1], bounds[1:]):
                dst = os.path.join("audio", f"shot_{idx:02d}.mp3")
                if len(grp) == 1:
                    cut_audio(gpath, 0.0, dur, dst)
                else:
                    cut_audio(gpath, st, en, dst)
                label = f"gemini:{model} [{len(groups)} call{'s' if len(groups) > 1 else ''}]"
                json.dump({"fp": shot_fingerprint(text, a), "engine": label}, open(os.path.join("audio", f"shot_{idx:02d}.json"), "w"))
                done[idx] = label
                MODELS_USED[model] = MODELS_USED.get(model, 0) + 1
            weak = [f"shot {grp[k][0]}|{grp[k + 1][0]}: {inf[2]} (pause {inf[1]:.2f}s at {inf[0]:.1f}s)"
                    for k, inf in enumerate(info) if inf[2] != "ok"]
            notes.append(f"Call {gi}/{len(groups)}: {dur:.1f}s of audio for {len(grp)} shots, "
                         f"{len(info) - len(weak)} clean cuts, {len(weak)} weak cuts")
            for w in weak:
                notes.append("  - " + w)
        remaining = next_remaining
    return done, [i for i, _ in remaining], notes


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--gender", default="male", choices=["male", "female"])
    ap.add_argument("--engine", default="gemini_only",
                    choices=["auto", "edge", "gemini", "gemini_only", "gemini_then_edge"])
    ap.add_argument("--gemini-voice", dest="gemini_voice", default="")
    ap.add_argument("--gemini-models", dest="gemini_models", default="",
                    help="comma list; empty = the TTS models your key can see")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--min-interval", dest="min_interval", type=float, default=24.0,
                    help="seconds between calls to one model (free tier: 3 per minute)")
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--calls", type=int, default=1,
                    help="Gemini calls for the whole story (1 = one voice, one call; 0 = one call per shot)")
    ap.add_argument("--no-reuse", dest="reuse", action="store_false",
                    help="make every shot again even if audio exists")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--on-fail", dest="on_fail", default="stop", choices=["stop", "silent"])
    ap.add_argument("--no-audio", dest="no_audio", action="store_true")
    ap.add_argument("--out-plan", default="scene_plan_timed.json")
    ap.add_argument("--quality", default="balanced", choices=list(QUALITY))
    ap.add_argument("--aspect", default="landscape", choices=["landscape", "vertical"])
    a = ap.parse_args()

    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    shots = find_shots(plan)
    os.makedirs("audio", exist_ok=True)

    chain = []
    uses_gemini = a.engine in ("gemini_only", "gemini", "gemini_then_edge", "auto")
    if not a.no_audio and uses_gemini and os.environ.get("GEMINI_API_KEY", "").strip():
        chain = build_chain(a, os.environ["GEMINI_API_KEY"].strip())
        print("Gemini model order:", ", ".join(chain))
    elif not a.no_audio and a.engine in ("gemini_only", "gemini"):
        sys.exit("FAIL: GEMINI_API_KEY is empty. Add it under Settings > Secrets > Actions.")

    GROUP_DONE, GROUP_FAILED, GROUP_NOTES = {}, set(), []
    if (not a.no_audio and chain and a.calls >= 1
            and a.engine in ("gemini_only", "gemini", "gemini_then_edge", "auto")):
        try:
            GROUP_DONE, gfail, GROUP_NOTES = make_group_voice(a, shots, chain)
        except Fatal as e:
            sys.exit(f"FAIL: {e}")
        GROUP_FAILED = set(gfail)

    rows, files, failed = [], [], []
    tl_shots, srt, srt_idx = [], [], 1
    t0, frame = 0.0, 0
    for i, s in enumerate(shots, start=1):
        s.setdefault("id", i)
        text = get_text(s)
        path = os.path.join("audio", f"shot_{i:02d}.mp3")
        meta = os.path.join("audio", f"shot_{i:02d}.json")
        engine, vdur = None, 0.0

        if text and not a.no_audio:
            fp = shot_fingerprint(text, a)
            reused = False
            if a.reuse and os.path.exists(path) and os.path.exists(meta) and os.path.getsize(path) > 1000:
                try:
                    reused = json.load(open(meta)).get("fp") == fp
                    if reused:
                        engine = json.load(open(meta)).get("engine", "reused")
                        if i not in GROUP_DONE:
                            engine += " (kept)"
                except Exception:
                    reused = False
            if not reused:
                print(f"Shot {i}/{len(shots)}: making voice ({len(text)} chars)", flush=True)
                try:
                    engine = make_audio(text, a, path, chain, skip_gemini=(i in GROUP_FAILED))
                except Fatal as e:
                    sys.exit(f"FAIL: {e}")
                if engine:
                    json.dump({"fp": fp, "engine": engine}, open(meta, "w"))
                    if engine.startswith("gemini:"):
                        MODELS_USED[engine[7:]] = MODELS_USED.get(engine[7:], 0) + 1
            if engine:
                vdur = probe(path)
                files.append(path)
            else:
                failed.append(i)
                print(f"Shot {i}: NO VOICE (all engines failed)", flush=True)

        if not engine:
            vdur = len(text) / CHARS_PER_SEC if text else 0.0
            path = None

        floor = max(float(s.get("min_seconds", 0) or 0), float(s.get("duration", 0) or 0))
        dur = max(vdur + LEAD + TAIL if text else 0.0, floor, 1.0)
        n = int(math.ceil(dur * a.fps))
        dur = n / a.fps

        s["text"] = s.get("text", text) if "text" in s or not text else text
        s["audio"] = path
        s["voice_seconds"] = round(vdur, 3)
        s["duration"] = round(dur, 3)
        s["start_time"] = round(t0, 3)
        s["end_time"] = round(t0 + dur, 3)
        s["start_frame"] = frame
        s["end_frame"] = frame + n
        s["lead"] = LEAD
        rows.append((i, s.get("title", ""), engine or ("NO VOICE" if text else "no text"), vdur, dur, n))
        tl_shots.append({
            "id": s["id"], "title": s.get("title", ""), "audio": path,
            "voice_seconds": round(vdur, 3), "duration": round(dur, 3),
            "start_time": round(t0, 3), "start_frame": frame, "end_frame": frame + n,
            "thumb": bool((s.get("camera") or {}).get("thumb")),
        })
        lines = split_lines(text) if text else []
        total_chars = sum(len(x) for x in lines) or 1
        cur = t0 + LEAD
        for ln in lines:
            span = vdur * len(ln) / total_chars
            srt.append(f"{srt_idx}\n{srt_time(cur)} --> {srt_time(cur + span)}\n{ln}\n")
            srt_idx += 1
            cur += span
        t0 += dur
        frame += n

    if isinstance(plan, dict):
        plan["fps"] = a.fps
        plan["total_frames"] = frame
        plan["total_seconds"] = round(t0, 2)
        plan["voice_mode"] = "silent" if a.no_audio else "voice"
    with open(a.out_plan, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    w, h, samples = QUALITY[a.quality]
    if a.aspect == "vertical":
        w, h = h, w
    timeline = {"fps": a.fps, "width": w, "height": h, "samples": samples,
                "aspect": a.aspect, "total_frames": frame,
                "total_seconds": round(t0, 2), "lead": LEAD, "shots": tl_shots}
    with open("timeline.json", "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=1)
    with open("subs.srt", "w", encoding="utf-8") as f:
        f.write("\n".join(srt))

    if files:
        with open("preview_list.txt", "w") as f:
            for p in files:
                f.write(f"file '{os.path.abspath(p)}'\n")
        subprocess.call(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                         "-i", "preview_list.txt", "-c:a", "libmp3lame", "narration_preview.mp3"])
        os.remove("preview_list.txt")

    n_voice = sum(1 for r in rows if r[2] not in ("NO VOICE", "no text"))
    with open("tts_summary.md", "w", encoding="utf-8") as f:
        status = "ALL SHOTS HAVE A VOICE" if not failed else f"MISSING VOICE FOR SHOTS: {failed}"
        f.write(f"### Narration summary\n\n**{status}**\n\n")
        f.write(f"Voice: {a.gender} | engine: {a.engine} | rate: {a.rate}\n\n")
        f.write(f"Shots with voice: {n_voice} of {len(shots)}\n\n")
        if MODELS_USED:
            f.write("Gemini models used this run: " + ", ".join(f"{k} x{v}" for k, v in MODELS_USED.items()) + "\n\n")
        if EXHAUSTED:
            f.write("Models used up or unavailable: " + ", ".join(sorted(EXHAUSTED)) + "\n\n")
        if GROUP_NOTES:
            f.write("Single-voice cutting:\n\n" + "\n".join(GROUP_NOTES) + "\n\n")
        if failed:
            f.write("Run the workflow again later. Finished shots are kept, so only the missing ones are made.\n\n")
        f.write("| Shot | Title | Voice engine | Voice sec | Shot sec | Frames |\n|---|---|---|---|---|---|\n")
        for i, title, eng, v, d, n in rows:
            f.write(f"| {i} | {title} | {eng} | {v:.1f} | {d:.1f} | {n} |\n")
        f.write(f"\nTotal: {t0:.1f}s, {frame} frames at {a.fps} fps\n")
        if t0 > 175:
            f.write("\n**WARNING: longer than 175 s. Shorten the narration to stay a YouTube Short (max 3 min).**\n")

    print(f"Done. {len(shots)} shots, {t0:.1f}s, {frame} frames @ {a.fps}fps, {len(failed)} without voice")
    if failed and a.on_fail == "stop":
        sys.exit(f"FAIL: no voice for shots {failed}. Run again later to finish them.")


if __name__ == "__main__":
    main()
