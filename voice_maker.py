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
NARRATOR = {"male": "Achird", "female": "Sulafat"}      # friendly / warm voices that suit animal stories
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


class MultiUnsupported(Exception):
    def __init__(self, model):
        super().__init__(model)
        self.model = model


class QuotaExhausted(Exception):
    pass


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
                if e.code == 400 and "multi-speaker" in t:
                    raise MultiUnsupported(model)
                if e.code == 404 or (e.code == 429 and ("perday" in t or "per day" in t)):
                    state["dead"].add(model)
                    if e.code == 429:
                        state["quota_hit"] = True
                    print(f"  {model}: not usable now ({'daily quota used up' if e.code == 429 else 'not found'}), trying the next model", flush=True)
                    break
                wait = min(90, (e.wait or 0) + 3) if e.code == 429 else 6 * attempt
                print(f"  {model}: attempt {attempt}/3 failed ({str(e)[:300]}), waiting {wait:.0f}s", flush=True)
                if attempt < 3:
                    SLEEP(wait)
    if state.get("quota_hit") and all(m in state["dead"] for m in state["models"]):
        raise QuotaExhausted(str(last)[:200])
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


# ------------------------------------------------------------------ Microsoft voices (fallback engines: Azure Speech, Edge)
from xml.sax.saxutils import escape as xml_escape

# role -> ([voice names, best first], rate %, pitch %). The first name that really exists is used.
MS_EN = {
    "narrator_male": (["en-US-AndrewNeural", "en-US-GuyNeural", "en-US-ChristopherNeural"], 0, 0),
    "narrator_female": (["en-US-AvaNeural", "en-US-JennyNeural", "en-US-AriaNeural"], 0, 0),
    "milo": (["en-US-AnaNeural", "en-US-EmmaNeural", "en-US-JennyNeural"], 6, 6),
    "ragnar": (["en-GB-ThomasNeural", "en-GB-RyanNeural", "en-US-DavisNeural"], -12, -6),
    "drakon": (["en-US-ChristopherNeural", "en-US-EricNeural", "en-US-GuyNeural"], -8, -10),
    "lakshmi": (["en-IN-NeerjaNeural", "en-US-AriaNeural", "en-US-JennyNeural"], -3, 0),
    "bholu": (["en-US-GuyNeural", "en-US-BrianNeural", "en-US-SteffanNeural"], 10, 6),
    "veer": (["en-GB-RyanNeural", "en-AU-WilliamNeural", "en-US-ChristopherNeural"], -6, -4),
}
MS_EN_SPARE = [(["en-US-EricNeural", "en-US-RogerNeural"], 0, -4), (["en-US-MichelleNeural", "en-US-AriaNeural"], 0, 0),
               (["en-AU-WilliamNeural", "en-GB-RyanNeural"], 4, 0), (["en-US-SteffanNeural", "en-US-BrianNeural"], -4, -6)]
MS_HI = {
    "narrator_male": (["hi-IN-MadhurNeural"], 0, 0),
    "narrator_female": (["hi-IN-SwaraNeural"], 0, 0),
    "milo": (["hi-IN-MadhurNeural"], 6, 14),
    "ragnar": (["hi-IN-MadhurNeural"], -12, -8),
    "drakon": (["hi-IN-MadhurNeural"], -6, -14),
    "lakshmi": (["hi-IN-SwaraNeural"], -3, 0),
    "bholu": (["hi-IN-MadhurNeural"], 10, 8),
    "veer": (["hi-IN-MadhurNeural"], -4, -4),
}
MS_HI_SPARE = [(["hi-IN-MadhurNeural"], 3, 4), (["hi-IN-SwaraNeural"], 4, 6)]


def edge_available():
    import asyncio, edge_tts
    return {x["ShortName"]: x for x in asyncio.run(edge_tts.list_voices())}


def build_ms_profiles(plan, gender, language, avail, extra_ids):
    table, spare = (MS_HI, list(MS_HI_SPARE)) if language == "Hindi" else (MS_EN, list(MS_EN_SPARE))
    used, out = set(), {}

    def pick(cands):
        for c in cands:
            if (not avail or c in avail) and c not in used:
                return c
        for c in cands:
            if not avail or c in avail:
                return c
        for nme in avail:                                  # any neural voice of the same language
            if nme.startswith(cands[0][:2]) and nme.endswith("Neural") and nme not in used:
                return nme
        return cands[0]

    def add(key, spec):
        cands, rate, pitch = spec
        v = pick(cands)
        used.add(v)
        out[key] = {"voice": v, "rate": rate, "pitch": pitch}

    add("narrator", table["narrator_female" if gender == "female" else "narrator_male"])
    ids = [c["id"] for c in plan.get("cast", [])] + [i for i in extra_ids if i not in [c["id"] for c in plan.get("cast", [])]]
    for cid in ids:
        if cid == "narrator":
            continue
        cast = next((c for c in plan.get("cast", []) if c["id"] == cid), {})
        if cast.get("ms_voice"):                           # your own choice in animal_studio.json: {"voice","rate","pitch"}
            out[cid] = dict({"rate": 0, "pitch": 0}, **cast["ms_voice"])
            used.add(out[cid]["voice"])
        elif cid in table:
            add(cid, table[cid])
        else:
            add(cid, spare.pop(0) if spare else table["narrator_male"])
    return out


def _read_pcm(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def edge_synth(text, prof, tmpdir):
    import asyncio, edge_tts
    mp3, wav = os.path.join(tmpdir, "_ms.mp3"), os.path.join(tmpdir, "_ms.wav")

    async def go():
        await edge_tts.Communicate(text, prof["voice"], rate=f"{int(prof['rate']):+d}%",
                                   pitch=f"{int(prof['pitch'] * 1.5):+d}Hz").save(mp3)
    asyncio.run(go())
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ar", "24000", "-ac", "1", wav], check=True)
    return _read_pcm(wav)


def azure_synth(text, prof, tmpdir, key, region):
    lang = prof["voice"][:5]
    ssml = (f"<speak version='1.0' xml:lang='{lang}'><voice name='{prof['voice']}'>"
            f"<prosody rate='{int(prof['rate']):+d}%' pitch='{int(prof['pitch']):+d}%'>{xml_escape(text)}</prosody></voice></speak>")
    req = urllib.request.Request(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1", data=ssml.encode("utf-8"),
                                 headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                                          "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm", "User-Agent": "shorts-voice"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    wav = os.path.join(tmpdir, "_az.wav")
    open(wav, "wb").write(data)
    return _read_pcm(wav)


def voice_shot_ms(idx, n, shot, ctx, engine, profiles):
    """One shot with Microsoft voices: every animal has its own voice, no two-voice limit, no quota."""
    segs = segments(shot) if ctx["characters"] else [("narrator", shot["narration"].strip())]
    prof_of = lambda spk: profiles.get(spk) or profiles["narrator"]
    cache_key = hashlib.sha1(json.dumps([engine, segs, [prof_of(s) for s, _ in segs]], ensure_ascii=False).encode()).hexdigest()[:10]
    raw_path = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}_{cache_key}.raw.wav")
    final = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}.wav")
    calls = 0
    if not os.path.exists(raw_path):
        parts, rate = [], 24000
        for spk, text in segs:
            if engine == "azure":
                pcm, rate = azure_synth(text, prof_of(spk), ctx["dir"], ctx["azure_key"], ctx["azure_region"])
            else:
                pcm, rate = edge_synth(text, prof_of(spk), ctx["dir"])
            calls += 1
            parts += [pcm, b"\x00\x00" * int(rate * 0.12)]
        write_wav(raw_path, b"".join(parts), rate)
    trim_silence(raw_path, final)
    return final, wav_seconds(final), engine, calls


# ------------------------------------------------------------------ one shot
def voice_shot(idx, n, shot, ctx):
    language, voices, names, key, state = ctx["language"], ctx["voices"], ctx["names"], ctx["key"], ctx["state"]
    segs = segments(shot) if ctx["characters"] else [("narrator", shot["narration"].strip())]
    full = " ".join(t for _, t in segs)
    exp = units_of(full, language) / ctx["rate"] + 0.2 + 0.15 * pauses_in(full)
    style = style_phrase(shot, idx, n) if ctx["style"] else "off"
    cache_key = hashlib.sha1(json.dumps([segs, style, [voices[s] for s, _ in segs]], ensure_ascii=False).encode()).hexdigest()[:10]
    raw_path = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}_{cache_key}.raw.wav")
    final = os.path.join(ctx["dir"], f"shot_{shot['id']:02d}.wav")
    if os.path.exists(raw_path):
        trim_silence(raw_path, final)
        return final, wav_seconds(final), "cached", 0
    if ctx["fake"]:
        rate = 24000
        pcm = b"".join(int(8000 * math.sin(2 * math.pi * 220 * k / rate)).to_bytes(2, "little", signed=True) for k in range(int(rate * exp)))
        write_wav(raw_path, pcm, rate)
        trim_silence(raw_path, final)
        return final, wav_seconds(final), "fake", 0

    voices_used = []
    for spk, _ in segs:
        if spk not in voices_used:
            voices_used.append(spk)
    calls = 0

    def single(spk, text):
        nonlocal calls
        prompt = f"Say {style}: {text}" if ctx["style"] else text      # by default: the plain story text only
        calls += 1
        r = speak(prompt, single_cfg(voices[spk]), key, state)
        SLEEP(ctx["sleep"])
        return r

    def multi(group, preamble):
        nonlocal calls
        vs = []
        for spk, _ in group:
            if spk not in vs:
                vs.append(spk)
        a, b = vs[0], vs[1]
        lines = "\n".join(f"{names[s]}: {t}" for s, t in group)
        prompt = (f"TTS the following conversation between {names[a]} and {names[b]}:\n" if preamble else "") + lines
        calls += 1
        try:
            r = speak(prompt, multi_cfg([(names[a], voices[a]), (names[b], voices[b])]), key, state)
        except MultiUnsupported as ex:
            state["nomulti"].add(ex.model)
            print(f"  {ex.model} does not accept two voices in one call, voicing each animal separately", flush=True)
            return None
        SLEEP(ctx["sleep"])
        return r

    def runs():
        parts, model, rate = [], None, 24000
        for spk, t in segs:                                   # one call per speaker run
            pcm, rate, model = single(spk, t)
            parts.append(pcm)
            parts.append(b"\x00\x00" * int(rate * 0.12))
        return b"".join(parts), rate, model

    best = None
    for variant in (0, 1):
        if len(voices_used) == 1:
            pcm, rate, model = single(voices_used[0], full)
        elif len(voices_used) == 2 and state["model"] not in state["nomulti"]:
            r = multi(segs, preamble=(variant == 0))
            pcm, rate, model = r if r else runs()
        else:
            pcm, rate, model = runs()
        write_wav(raw_path, pcm, rate)
        trim_silence(raw_path, final)
        dur = wav_seconds(final)
        best = (dur, model)
        # a too long result is only worth one more try when something could have been read aloud
        # (a two-voice request or a style hint), and only once the normal reading speed is known
        suspicious = ctx["calibrated"] and dur > exp * 1.5 + 1.2 and (len(voices_used) == 2 or ctx["style"])
        if suspicious and variant == 0:
            print(f"  shot {shot['id']}: {dur:.1f}s but about {exp:.1f}s expected, voicing again in a plainer form", flush=True)
            if ctx["style"]:
                ctx["style"] = False
            continue
        break
    return final, wav_seconds(final), best[1], calls


# ------------------------------------------------------------------ ONE call for the whole story (single voice), cut into shots afterwards
def eff_len(s):
    return len(s) + 10 * pauses_in(s) + 4 * sum(s.count(c) for c in ",;:")


def find_silences(path, noise_db=-35, dur=0.22):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", f"silencedetect=noise={noise_db}dB:d={dur}", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
    ends = [(float(x), float(y)) for x, y in re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", r.stderr)]
    out = []
    for s, (e, d) in zip(starts, ends):
        out.append((max(0.0, s), e, d))
    return out


def llm_align(path, texts, key, cache_path):
    """Ask a Gemini TEXT model (its own, much bigger quota) when each segment starts. Optional: None if it does not work."""
    if os.path.exists(cache_path):
        try:
            return json.load(open(cache_path))["starts"]
        except Exception:
            pass
    mp3 = path + ".align.mp3"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ar", "16000", "-ac", "1", "-b:a", "48k", mp3], check=True)
        b64 = base64.b64encode(open(mp3, "rb").read()).decode()
    except Exception as e:
        print("  alignment skipped (could not prepare the audio):", str(e)[:80], flush=True)
        return None
    n = len(texts)
    prompt = (f"Listen to the audio. It reads these {n} numbered text segments in order, nothing skipped. For EACH segment give the time in seconds "
              f"(decimal) at which its first spoken word starts. Return ONLY JSON: {{\"starts\": [{n} numbers in increasing order, the first one near 0]}}\n\n"
              + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts)))
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "audio/mp3", "data": b64}}, {"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    for model in [m for m in os.environ.get("ALIGN_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m]:
        try:
            req = urllib.request.Request(f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=300) as r:
                out = json.load(r)
            txt = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
            starts = [float(x) for x in json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip()))["starts"]]
            if len(starts) == n and all(starts[i] < starts[i + 1] for i in range(n - 1)):
                json.dump({"starts": starts, "model": model}, open(cache_path, "w"))
                print(f"  alignment by {model}: ok", flush=True)
                return starts
        except Exception as e:
            print(f"  alignment with {model} not possible ({str(e)[:70]})", flush=True)
    return None


def pick_cuts(T, texts, sil, llm):
    """The n-1 cut times. Each cut goes into a real pause, chosen so the shots follow the text lengths (and the LLM's times)."""
    n = len(texts)
    w = [eff_len(t) for t in texts]
    tot = float(sum(w))
    exp = [T * sum(w[:i]) / tot for i in range(1, n)]
    est = [(0.65 * llm[i] + 0.35 * exp[i - 1]) if llm else exp[i - 1] for i in range(1, n)]
    cands = [((s + e) / 2.0, d) for s, e, d in sil if 1.5 < (s + e) / 2.0 < T - 1.5]
    m = len(cands)
    if m < n - 1:
        print(f"  only {m} pauses found for {n - 1} cuts, some cuts are placed by the text length", flush=True)
        return [min(max(est[i], 1.5), T - 1.5) for i in range(n - 1)]
    INF = 1e18
    best = [[INF] * m for _ in range(n - 1)]
    back = [[-1] * m for _ in range(n - 1)]
    cost = lambda i, j: abs(cands[j][0] - est[i]) - 1.2 * min(cands[j][1], 0.7)
    for j in range(m):
        best[0][j] = cost(0, j)
    for i in range(1, n - 1):
        for j in range(m):
            for k in range(j):
                if cands[j][0] - cands[k][0] >= 1.8 and best[i - 1][k] + cost(i, j) < best[i][j]:
                    best[i][j] = best[i - 1][k] + cost(i, j)
                    back[i][j] = k
    j = min(range(m), key=lambda x: best[n - 2][x])
    if best[n - 2][j] >= INF:
        return [min(max(est[i], 1.5), T - 1.5) for i in range(n - 1)]
    cuts = []
    for i in range(n - 2, -1, -1):
        cuts.append(cands[j][0])
        j = back[i][j]
    return cuts[::-1]


def voice_looks_ok(seconds, texts, language, rate):
    """A single Gemini call for the whole story can come back CUT OFF (the last shots silent) or with extra noise.
    Compare the length with what the text needs. Returns (ok, message). Wide limits: only clear failures are caught."""
    units = sum(units_of(t, language) for t in texts)
    exp = units / max(0.5, rate)
    if seconds < 0.55 * exp:
        return False, f"voice is only {seconds:.0f}s but the text needs about {exp:.0f}s: it looks cut off"
    if seconds > 2.3 * exp + 8:
        return False, f"voice is {seconds:.0f}s but the text needs about {exp:.0f}s: it looks wrong (extra sound or repeats)"
    return True, ""


def voice_single(shots, ctx, a):
    texts = [s["narration"].strip() for s in shots]
    voice = ctx["voices"]["narrator"]
    h = hashlib.sha1(json.dumps([texts, voice, ctx["fake"]], ensure_ascii=False).encode()).hexdigest()[:10]
    raw = os.path.join(ctx["dir"], f"single_{h}.raw.wav")
    full = os.path.join(ctx["dir"], f"single_{h}.wav")
    calls = 0
    if not os.path.exists(raw):
        if ctx["fake"]:
            rate, pcm = 24000, b""
            for t in texts:
                secs = units_of(t, ctx["language"]) / 2.4 + 0.3
                pcm += b"".join(int(8000 * math.sin(2 * math.pi * 220 * k / rate)).to_bytes(2, "little", signed=True) for k in range(int(rate * secs)))
                pcm += b"\x00\x00" * int(rate * 0.7)
        else:
            print(f"Voice: ONE Gemini call for the whole story, voice {voice}", flush=True)
            for tryno in (1, 2, 3):                          # check the answer is complete; ask again (max 3 calls), then the caller falls back
                pcm, rate, model = speak("\n\n".join(texts), single_cfg(voice), ctx["key"], ctx["state"])
                calls += 1
                ok, why = voice_looks_ok(len(pcm) / 2.0 / rate, texts, ctx["language"], ctx["rate"])
                if ok:
                    break
                print(f"  voice check {tryno}/3: {why}", flush=True)
                if tryno == 3:
                    raise RuntimeError("Gemini voice kept coming back incomplete: " + why)
        write_wav(raw, pcm, rate)
    trim_silence(raw, full)
    T = wav_seconds(full)
    llm = None if ctx["fake"] else llm_align(full, texts, ctx["key"], os.path.join(ctx["dir"], f"align_{h}.json"))
    cuts = pick_cuts(T, texts, find_silences(full), llm)
    print("  cuts at:", ", ".join(f"{c:.1f}" for c in cuts), f"| total speech {T:.1f}s", flush=True)
    with wave.open(full, "rb") as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    bounds = [0.0] + list(cuts) + [T]
    results = []
    for i, sh in enumerate(shots):
        s0, s1 = int(bounds[i] * rate) * 2, int(bounds[i + 1] * rate) * 2
        path = os.path.join(ctx["dir"], f"shot_{sh['id']:02d}.wav")
        write_wav(path, frames[s0:s1], rate)
        results.append((path, (s1 - s0) / 2.0 / rate, "gemini (1 call)" if True else "", calls if i == 0 else 0))
    return results


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
    ap.add_argument("--sleep", type=float, default=3.0, help="pause between TTS calls (free tier limits)")
    ap.add_argument("--style", choices=["off", "on"], default="off", help="on = add a short \"Say ...:\" hint (it can be read aloud)")
    ap.add_argument("--layout", choices=["single", "per_shot"], default="single",
                    help="single = ONE Gemini call for the whole story and ONE voice, cut into shots afterwards (fewest calls)")
    ap.add_argument("--engine", choices=["auto", "gemini", "azure", "edge"], default="auto", help="auto = Gemini first, then Azure (if keys), then Edge")
    ap.add_argument("--fallback", choices=["consistent", "mixed"], default="consistent", help="consistent = if Gemini fails, voice the WHOLE video with the next engine")
    ap.add_argument("--gemini-rounds", type=int, default=3, help="how many full rounds Gemini gets per shot before the fallback")
    ap.add_argument("--characters", choices=["auto", "off"], default="auto", help="off = the narrator reads everything (fewest calls)")
    ap.add_argument("--fake", action="store_true", help="test without Gemini: beeps of the expected length")
    a, _unused = ap.parse_known_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    language = plan.get("language", "English")
    shots = plan["shots"]
    key = os.environ.get("GEMINI_API_KEY", "").strip()
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

    az_key, az_region = os.environ.get("AZURE_SPEECH_KEY", "").strip(), os.environ.get("AZURE_SPEECH_REGION", "").strip()
    engines = {"auto": ["gemini"] + (["azure"] if az_key and az_region else []) + ["edge"],
               "gemini": ["gemini"], "azure": ["azure"], "edge": ["edge"]}[a.engine]
    if "gemini" in engines and not key and not a.fake:
        engines.remove("gemini")
        print("No GEMINI_API_KEY: starting with the Microsoft voices.", flush=True)
    if not engines:
        sys.exit("FAIL: no voice engine is available.")
    ctx = {"language": language, "voices": voices, "names": names, "key": key, "dir": a.audio_dir, "fake": a.fake, "sleep": a.sleep,
           "rate": 2.0 if language != "Hindi" else 9.0, "style": a.style == "on", "characters": a.characters == "auto", "calibrated": False,
           "azure_key": az_key, "azure_region": az_region,
           "state": {"model": "", "models": models, "dead": set(), "nomulti": set(), "quota_hit": False}}
    ms = {"profiles": None}

    def ms_profiles():
        if ms["profiles"] is None:
            avail = {}
            try:
                avail = edge_available()
            except Exception as ex:
                print(f"  could not read the Microsoft voice list ({str(ex)[:80]}), using the built-in names", flush=True)
            ms["profiles"] = build_ms_profiles(plan, a.gender, language, avail, list(voices))
            print("Microsoft voices:", ", ".join(f"{k}={p['voice']}({p['rate']:+d}%,{p['pitch']:+d})" for k, p in ms["profiles"].items()), flush=True)
        return ms["profiles"]

    def one(engine, i, sh):
        if engine == "gemini":
            for rnd in range(1, a.gemini_rounds + 1):          # try Gemini as long as it can possibly work
                try:
                    return voice_shot(i, len(shots), sh, ctx)
                except QuotaExhausted:
                    raise
                except RuntimeError as ex:
                    if rnd == a.gemini_rounds:
                        raise
                    print(f"  Gemini round {rnd}/{a.gemini_rounds} failed ({str(ex)[:120]}), waiting {30 * rnd}s", flush=True)
                    SLEEP(30 * rnd)
        for rnd in range(1, 4):
            try:
                return voice_shot_ms(i, len(shots), sh, ctx, engine, ms_profiles())
            except Exception as ex:
                if rnd == 3:
                    raise RuntimeError(f"{engine} failed: {str(ex)[:150]}")
                print(f"  {engine} attempt {rnd}/3 failed ({str(ex)[:100]}), waiting {5 * rnd}s", flush=True)
                SLEEP(5 * rnd)

    results, rates, ei, i = None, [], 0, 0
    single_mode = False
    if a.layout == "single" and engines[0] == "gemini":
        try:
            results = voice_single(shots, ctx, a)
            single_mode = True
        except (QuotaExhausted, RuntimeError) as ex:
            print(f"FALLBACK: Gemini could not make the voice ({str(ex)[:140]}). Using {engines[1] if len(engines) > 1 else 'nothing'} with ONE narrator voice.", flush=True)
            results = None
            engines = engines[1:]
            ctx["characters"] = False
            if not engines:
                if isinstance(ex, QuotaExhausted):
                    print("STOP: the daily voice quota is used up. Run again after the quota resets (or switch on billing).", flush=True)
                    sys.exit(75)
                raise
    if results is None:
        results = []
    else:
        i = len(shots)
    while i < len(shots):
        eng, sh = engines[ei], shots[i]
        print(f"Voice shot {sh['id']}/{len(shots)} [{eng}]", flush=True)
        try:
            res = one(eng, i, sh)
        except (QuotaExhausted, RuntimeError) as ex:
            if ei + 1 >= len(engines):
                if isinstance(ex, QuotaExhausted):
                    print(f"STOP: the daily voice quota is used up ({ex}). {i} of {len(shots)} shots are finished and kept; run the workflow "
                          "again after the quota resets (or switch on billing) and it continues.", flush=True)
                    sys.exit(75)
                raise
            ei += 1
            keep = "for the WHOLE video, so every voice stays the same" if a.fallback == "consistent" else "for the remaining shots"
            print(f"FALLBACK: {eng} could not finish ({str(ex)[:140]}). Using {engines[ei]} {keep}.", flush=True)
            if a.fallback == "consistent":
                results, rates, i = [], [], 0
            continue
        if eng == "gemini" and res[1] > 0.5:
            full = " ".join(t for _, t in segments(sh))
            rates.append(units_of(full, language) / max(0.5, res[1] - 0.15 * pauses_in(full) - 0.2))
            if len(rates) >= 3:
                ctx["rate"] = statistics.median(rates)
                ctx["calibrated"] = True
        results.append(res)
        i += 1
    final_engine = "gemini" if single_mode else engines[ei]
    if final_engine != engines[0]:
        print(f"NOTE: the voice was made with {final_engine}, not {engines[0]}.", flush=True)
    if final_engine != "gemini":
        narrator = ms_profiles()["narrator"]["voice"]

    # ---- timeline: the video is exactly as long as the voice needs
    fps = a.fps
    w, h, samples = QUALITY.get(a.quality, QUALITY["high"])
    durs, leads = [], []
    for i, (_, secs, _, _) in enumerate(results):
        if single_mode:                                  # the shots are slices of one continuous reading: no extra gaps
            lead = a.lead if i == 0 else 0.0
            durs.append(max(3.0, lead + secs + (a.end_pause if i == len(results) - 1 else 0.0)))
        else:
            lead = a.lead
            durs.append(max(a.min_shot, a.lead + secs + (a.end_pause if i == len(results) - 1 else a.pause)))
        leads.append(lead)
    min_total = float(os.environ.get("MIN_VIDEO_SECONDS", "120") or 0)         # the channel wants at least 2 minutes (no fixed maximum below 3 minutes)
    if min_total and durs and sum(durs) < min_total:
        add = min(10.0, min_total - sum(durs))
        print(f"NOTE: the story is {sum(durs):.0f} s, below the {min_total:.0f} s minimum: the last shot is held {add:.1f} s longer.", flush=True)
        durs[-1] += add
        if sum(durs) < min_total - 0.5:
            print(f"WARNING: still only {sum(durs):.0f} s. The story was too short; the director should write more (target 300 to 380 words).", flush=True)
    bounds, acc = [], 0.0
    for dsec in durs:
        bounds.append((int(round(acc * fps)), int(round((acc + dsec) * fps))))
        acc += dsec
    total_frames = bounds[-1][1]
    tl_shots = []
    for sh, (path, secs, model, calls), (sf, ef), lead in zip(shots, results, bounds, leads):
        dur = (ef - sf) / fps
        sh.update({"duration": round(dur, 3), "start_frame": sf, "end_frame": ef, "start_time": round(sf / fps, 3)})
        tl_shots.append({"id": sh["id"], "duration": round(dur, 3), "start_frame": sf, "end_frame": ef,
                         "start_time": round(sf / fps, 3), "lead": lead, "voice_seconds": round(secs, 3),
                         "audio": path, "thumb": bool(sh.get("thumb"))})
    total = total_frames / float(fps)
    plan.update({"fps": fps, "total_frames": total_frames, "total_seconds": round(total, 3), "target_seconds": round(total),
                 "voices": voices, "voice_engine": final_engine})
    json.dump(plan, open("scene_plan_timed.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"fps": fps, "width": w, "height": h, "samples": samples, "aspect": "vertical", "total_frames": total_frames,
               "total_seconds": round(total, 3), "tempo": 1.0, "lead": a.lead, "shots": tl_shots},
              open("timeline.json", "w"), indent=1)

    speech = sum(r[1] for r in results)
    md = [f"Voice report\nALL {len(results)} SHOTS HAVE A VOICE. VIDEO LENGTH = {total:.1f} s ({total_frames} frames at {fps} fps)\n",
          f"Narrator: {narrator} | engine: {final_engine} ({ctx['state']['model'] if final_engine == 'gemini' else 'Microsoft'}) | speech {speech:.1f} s, pause after each shot {a.pause} s, picture {w}x{h}, {samples} samples\n",
          "Shot\tVoice\tVoice s\tShot s"]
    for sh, (path, secs, model, calls), t in zip(shots, results, tl_shots):
        md.append(f"{sh['id']}\t{model} [{calls} calls]\t{secs:.1f}\t{t['duration']:.1f}")
    open("voice_report.md", "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md))
    if total > 180:
        print("WARNING: longer than 3 minutes, this will not be a Short. Shorten the story or lower --pause.")


if __name__ == "__main__":
    main()
