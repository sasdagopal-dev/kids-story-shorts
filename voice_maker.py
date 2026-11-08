#!/usr/bin/env python3
"""voice_maker.py (v5) - one clean voice per shot, a different voice for every talking animal,
and the video length follows the real voice (no fixed length, no more "too long / too short" failures).

  python3 voice_maker.py --plan plans/episode_x.json --gender male --mode auto --fps 15 --quality high

Writes: scene_plan_timed.json, timeline.json, audio/shot_NN.wav, voice_report.md

What is different from the old version
* every shot is voiced on its own (no big groups that must be cut apart afterwards)
* the style hint is only a tiny "Say ...:" prefix; every shot is checked against its expected reading time, and
  if the voice speaks much longer (it read the instruction aloud) the shot is voiced again WITHOUT any instruction
* the same TTS model is used for the whole video, so the voice does not change between shots
* quoted lines are spoken by the animal that says them (voice_name in the cast, default voices below)
* the ending (lesson, subscribe line, disclaimer) is part of the last shots and is spoken like everything else
* silence at the start and end of every clip is trimmed, then a fixed pause is added: the video length is
  the sum of the shots, whatever it turns out to be
"""
import argparse, base64, hashlib, json, math, os, re, shutil, statistics, subprocess, sys, time, urllib.error, urllib.request, wave

BASE = "https://generativelanguage.googleapis.com/v1beta"
MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts",
          "gemini-3.8-flash-lite-tts", "gemini-2.5-pro-preview-tts"]
QUALITY = {"fast": (540, 960, 8), "balanced": (720, 1280, 16), "high": (720, 1280, 32), "ultra": (1080, 1920, 28)}
NARRATOR = {"male": "Charon", "female": "Kore"}
DEFAULT_VOICES = {"milo": "Puck", "ragnar": "Gacrux", "drakon": "Algenib", "lakshmi": "Sulafat", "bholu": "Fenrir", "veer": "Orus"}
SPARE = ["Zephyr", "Iapetus", "Umbriel", "Achird", "Leda", "Enceladus"]
QUOTE_RE = re.compile(r'["\u201c](.+?)["\u201d]')
SLEEP = time.sleep


class TTSError(Exception):
    def __init__(self, msg, code=None, wait=None, text=""):
        super().__init__(msg)
        self.code, self.wait, self.text = code, wait, text


# ------------------------------------------------------------------ helpers
def units_of(text, language):
    if language == "Hindi":
        return len(re.sub(r"[\s\W]", "", text))
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\u2019\-]*", text))


def pauses_in(text):
    return sum(text.count(c) for c in ".!?\u2026\u0964")


def write_wav(path, pcm, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def trim_silence(src, dst):
    af = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,areverse,"
          "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,areverse")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", af, "-ar", "24000", "-ac", "1", dst], check=True)
        if wav_seconds(dst) < 0.2:
            raise RuntimeError("trimmed to nothing")
    except Exception:
        shutil.copy(src, dst)


# ------------------------------------------------------------------ Gemini TTS
def tts_request(model, text, speech_cfg, key):
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech_cfg}}
    req = urllib.request.Request(f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "replace")
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', t)
        raise TTSError(f"HTTP {e.code}: {t[:200]}", e.code, float(m.group(1)) if m else None, t)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise TTSError(f"network: {e}", None)
    for cand in out.get("candidates") or []:
        for p in (cand.get("content") or {}).get("parts") or []:
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                mt = inl.get("mimeType") or inl.get("mime_type") or ""
                m = re.search(r"rate=(\d+)", mt)
                return base64.b64decode(inl["data"]), int(m.group(1)) if m else 24000
    raise TTSError("no audio in the answer", 200)


def speak(text, speech_cfg, key, state):
    """Returns (pcm, rate, model). Keeps the same model for the whole video; switches only if it is not usable."""
    last = None
    order = [m for m in ([state["model"]] if state["model"] else []) + state["models"] if m not in state["dead"]]
    seen = set()
    for model in [m for m in order if not (m in seen or seen.add(m))]:
        for attempt in range(1, 4):
            try:
                pcm, rate = tts_request(model, text, speech_cfg, key)
                state["model"] = model
                return pcm, rate, model
            except TTSError as e:
                last = e
                t = (e.text or str(e)).lower()
                if e.code in (401, 403) or "api key not valid" in t:
                    sys.exit(f"FAIL: Gemini rejected the key or access: {str(e)[:200]}")
                if e.code == 404 or (e.code == 429 and ("perday" in t or "per day" in t)):
                    state["dead"].add(model)
                    print(f"  {model}: not usable now, trying the next model", flush=True)
                    break
                wait = min(90, (e.wait or 0) + 3) if e.code == 429 else 6 * attempt
                print(f"  {model}: attempt {attempt}/3 failed ({str(e)[:90]}), waiting {wait:.0f}s", flush=True)
                if attempt < 3:
                    SLEEP(wait)
    raise RuntimeError(f"no TTS model answered: {last}")


def single_cfg(voice):
    return {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}


def multi_cfg(pairs):
    return {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
        {"speaker": label, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}} for label, voice in pairs]}}


# ------------------------------------------------------------------ what is spoken, by whom
def segments(shot):
    """[(speaker_id, text)] in reading order. Quoted lines belong to the animal that says them."""
    text = shot["narration"].strip()
    ms = list(QUOTE_RE.finditer(text))
    ids = None
    if ms and shot.get("quotes") and len(shot["quotes"]) == len(ms):
        ids = [q["id"] for q in shot["quotes"]]
    elif ms and shot.get("speakers") and len(shot["speakers"]) == len(ms):
        ids = [q["id"] for q in shot["speakers"]]
    if not ms or ids is None:
        return [("narrator", text)]
    segs, pos = [], 0
    for i, m in enumerate(ms):
        pre = text[pos:m.start()].strip()
        if pre:
            segs.append(("narrator", pre))
        segs.append((ids[i], m.group(1).strip()))
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        segs.append(("narrator", tail))
    merged = []
    for spk, t in segs:
        if merged and merged[-1][0] == spk:
            merged[-1] = (spk, merged[-1][1] + " " + t)
        else:
            merged.append((spk, t))
    return merged


def style_phrase(shot, idx, n):
    tone = (shot.get("tone") or "").strip().lower()
    if idx == n - 1:
        tone = tone or "cheerful, friendly"
    if tone and re.fullmatch(r"[a-z ,\-]{3,40}", tone):
        return f"in a {tone} voice"
    return "in a clear, lively storytelling voice"


# ------------------------------------------------------------------ one shot
def voice_shot(idx, n, shot, ctx):
    language, voices, names, key, state = ctx["language"], ctx["voices"], ctx["names"], ctx["key"], ctx["state"]
    segs = segments(shot)
    full = " ".join(t for _, t in segs)
    exp = units_of(full, language) / ctx["rate"] + 0.2 + 0.15 * pauses_in(full)
    cache_key = hashlib.sha1(json.dumps([segs, style_phrase(shot, idx, n), [voices[s] for s, _ in segs]], ensure_ascii=False).encode()).hexdigest()[:10]
    raw_path = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}_{cache_key}.raw.wav")
    final = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}.wav")
    if os.path.exists(raw_path):
        trim_silence(raw_path, final)
        return final, wav_seconds(final), "cached", 0
    if ctx["fake"]:
        secs = exp
        rate = 24000
        pcm = b"".join(int(8000 * math.sin(2 * math.pi * 220 * k / rate)).to_bytes(2, "little", signed=True) for k in range(int(rate * secs)))
        write_wav(raw_path, pcm, rate)
        trim_silence(raw_path, final)
        return final, wav_seconds(final), "fake", 0

    voices_used = []
    for spk, _ in segs:
        if spk not in voices_used:
            voices_used.append(spk)
    calls = 0

    def one_call(group, plain):
        """group = [(speaker, text)] using at most 2 voices."""
        nonlocal calls
        vs = []
        for spk, _ in group:
            if spk not in vs:
                vs.append(spk)
        if len(vs) == 1:
            text = " ".join(t for _, t in group)
            prompt = text if plain else f"Say {style_phrase(shot, idx, n)}: {text}"
            cfg = single_cfg(voices[vs[0]])
        else:
            a, b = vs[0], vs[1]
            la, lb = names[a], names[b]
            prompt = f"TTS the following conversation between {la} and {lb}:\n" + "\n".join(f"{names[s]}: {t}" for s, t in group)
            cfg = multi_cfg([(la, voices[a]), (lb, voices[b])])
        calls += 1
        pcm, rate, model = speak(prompt, cfg, key, state)
        SLEEP(ctx["sleep"])
        return pcm, rate, model

    def render(plain):
        if len(voices_used) <= 2:
            return one_call(segs, plain)
        parts, model, rate = [], None, 24000
        for spk, t in segs:                                   # three voices: one call per speaker run
            pcm, rate, model = one_call([(spk, t)], plain)
            parts.append(pcm)
            parts.append(b"\x00\x00" * int(rate * 0.12))
        return b"".join(parts), rate, model

    best = None
    for plain in (False, True):
        pcm, rate, model = render(plain)
        write_wav(raw_path, pcm, rate)
        trim_silence(raw_path, final)
        dur = wav_seconds(final)
        best = (dur, model)
        if dur > exp * 1.3 + 0.5:
            print(f"  shot {shot['id']}: {dur:.1f}s but about {exp:.1f}s expected, probably an instruction was read aloud; voicing again without any instruction", flush=True)
            continue
        if dur < exp * 0.5 - 0.3:
            print(f"  shot {shot['id']}: {dur:.1f}s but about {exp:.1f}s expected, probably cut short; voicing again", flush=True)
            continue
        break
    return final, wav_seconds(final), best[1], calls


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--gender", default="male")
    ap.add_argument("--mode", default="auto")           # accepted for the old workflow, not needed any more
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", default="high")
    ap.add_argument("--voice", default="", help="narrator voice name (default Charon for male, Kore for female)")
    ap.add_argument("--models", default=os.environ.get("TTS_MODELS", ""))
    ap.add_argument("--pause", type=float, default=0.8, help="silence after every shot (seconds)")
    ap.add_argument("--end-pause", type=float, default=1.2, help="silence after the last shot")
    ap.add_argument("--lead", type=float, default=0.25)
    ap.add_argument("--min-shot", type=float, default=3.5)
    ap.add_argument("--audio-dir", default="audio")
    ap.add_argument("--sleep", type=float, default=2.0, help="pause between TTS calls (free tier limits)")
    ap.add_argument("--fake", action="store_true", help="test without Gemini: beeps of the expected length")
    a, _unused = ap.parse_known_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    language = plan.get("language", "English")
    shots = plan["shots"]
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and not a.fake:
        sys.exit("FAIL: GEMINI_API_KEY is empty.")
    os.makedirs(a.audio_dir, exist_ok=True)

    narrator = a.voice or NARRATOR.get(a.gender, "Charon")
    voices, names = {"narrator": narrator}, {"narrator": "Narrator"}
    spare = list(SPARE)
    for c in plan.get("cast", []):
        v = c.get("voice_name") or DEFAULT_VOICES.get(c["id"]) or (spare.pop(0) if spare else "Puck")
        if v == narrator:
            v = spare.pop(0) if spare else v
        voices[c["id"]] = v
        names[c["id"]] = re.sub(r"[^A-Za-z0-9]", "", c.get("name", c["id"])) or c["id"]
    for sh in shots:
        for spk, _ in segments(sh):
            if spk not in voices:
                voices[spk] = spare.pop(0) if spare else "Puck"
                names[spk] = re.sub(r"[^A-Za-z0-9]", "", spk) or "Animal"
    models = [m.strip() for m in a.models.split(",") if m.strip()] or MODELS
    print("TTS models:", ", ".join(models), "| narrator:", narrator, "| voices:", ", ".join(f"{k}={v}" for k, v in voices.items() if k != "narrator"), flush=True)

    ctx = {"language": language, "voices": voices, "names": names, "key": key, "dir": a.audio_dir, "fake": a.fake, "sleep": a.sleep,
           "rate": 2.5 if language != "Hindi" else 11.0, "state": {"model": "", "models": models, "dead": set()}}
    results, rates = [], []
    for i, sh in enumerate(shots):
        print(f"Voice shot {sh['id']}/{len(shots)}", flush=True)
        path, secs, model, calls = voice_shot(i, len(shots), sh, ctx)
        full = " ".join(t for _, t in segments(sh))
        obs = units_of(full, language) / max(0.5, secs - 0.15 * pauses_in(full) - 0.2)
        if 0.5 * ctx["rate"] < obs < 2.0 * ctx["rate"]:
            rates.append(obs)
            if len(rates) >= 3:
                ctx["rate"] = statistics.median(rates)
        results.append((path, secs, model, calls))

    # ---- timeline: the video is exactly as long as the voice needs
    fps = a.fps
    w, h, samples = QUALITY.get(a.quality, QUALITY["high"])
    durs = []
    for i, (_, secs, _, _) in enumerate(results):
        pause = a.end_pause if i == len(results) - 1 else a.pause
        durs.append(max(a.min_shot, a.lead + secs + pause))
    bounds, acc = [], 0.0
    for dsec in durs:
        bounds.append((int(round(acc * fps)), int(round((acc + dsec) * fps))))
        acc += dsec
    total_frames = bounds[-1][1]
    tl_shots = []
    for sh, (path, secs, model, calls), (sf, ef) in zip(shots, results, bounds):
        dur = (ef - sf) / fps
        sh.update({"duration": round(dur, 3), "start_frame": sf, "end_frame": ef, "start_time": round(sf / fps, 3)})
        tl_shots.append({"id": sh["id"], "duration": round(dur, 3), "start_frame": sf, "end_frame": ef,
                         "start_time": round(sf / fps, 3), "lead": a.lead, "voice_seconds": round(secs, 3),
                         "audio": path, "thumb": bool(sh.get("thumb"))})
    total = total_frames / float(fps)
    plan.update({"fps": fps, "total_frames": total_frames, "total_seconds": round(total, 3), "target_seconds": round(total),
                 "voices": voices})
    json.dump(plan, open("scene_plan_timed.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"fps": fps, "width": w, "height": h, "samples": samples, "aspect": "vertical", "total_frames": total_frames,
               "total_seconds": round(total, 3), "tempo": 1.0, "lead": a.lead, "shots": tl_shots},
              open("timeline.json", "w"), indent=1)

    speech = sum(r[1] for r in results)
    md = [f"Voice report\nALL {len(results)} SHOTS HAVE A VOICE. VIDEO LENGTH = {total:.1f} s ({total_frames} frames at {fps} fps)\n",
          f"Narrator: {narrator} | model: {ctx['state']['model'] or 'cached/fake'} | speech {speech:.1f} s, pause after each shot {a.pause} s, picture {w}x{h}, {samples} samples\n",
          "Shot\tVoice\tVoice s\tShot s"]
    for sh, (path, secs, model, calls), t in zip(shots, results, tl_shots):
        md.append(f"{sh['id']}\t{model} [{calls} calls]\t{secs:.1f}\t{t['duration']:.1f}")
    open("voice_report.md", "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md))
    if total > 180:
        print("WARNING: longer than 3 minutes, this will not be a Short. Shorten the story or lower --pause.")


if __name__ == "__main__":
    main()
