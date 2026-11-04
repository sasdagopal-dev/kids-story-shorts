#!/usr/bin/env python3
"""tts2_check.py - full check of the Gemini voice, its retry logic and the Edge fallback.

  python3 tts2_check.py [--model M] [--voices Charon,Kore] [--burst 3]
                        [--plan scene_plan_part1.json --shots 2] [--offline] [--strict]

What it checks (each line in the report is PASS / FAIL / WARN / SKIP):
  1. tools: ffmpeg, ffprobe, edge-tts, GEMINI_API_KEY present
  2. retry logic, offline, with a fake Gemini (429, 403, empty audio, 500)
  3. key works, and which TTS models the key can see
  4. real Hindi voices (male + female), audio length and loudness checked
  5. a long narration (checks the audio is not cut short)
  6. real shots from your plan file (optional)
  7. burst of quick calls (finds the free-tier rate limit)
  8. Edge voice on its own (male + female)
  9. the full chain with a WRONG key: Gemini must fail and Edge must take over

Writes: out/tts2_report.md and the test audio files in out/.
The key is never printed.
"""
import argparse, asyncio, base64, json, math, os, random, re, struct, subprocess
import sys, time, urllib.error, urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED = ["gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"]
GV = {"male": "Charon", "female": "Kore"}
EV = {"male": "hi-IN-MadhurNeural", "female": "hi-IN-SwaraNeural"}

HI_SHORT = "एक छोटे से गाँव में रामू नाम का किसान रहता था।"
HI_LONG = ("एक दिन गाँव में राजा अपने सफ़ेद घोड़े पर आए। उन्होंने दोनों की परेशानी सुनी और "
           "मुस्कुराकर बोले, कमाई बढ़ाने से पहले, कमाई को संभालना सीखो। राजा ने पहला नियम बताया। "
           "जब भी पैसा मिले, सबसे पहले उसका एक हिस्सा अपने लिए बचाओ। बाक़ी बचे पैसों से खर्च चलाओ। "
           "बचत आख़िर में नहीं, सबसे पहले होनी चाहिए।")

KEY = ""
SLEEP = time.sleep
RESULTS = []


def scrub(s):
    s = str(s)
    return s.replace(KEY, "***") if KEY else s


def rec(name, status, detail=""):
    RESULTS.append((name, status, scrub(detail)))
    print(f"[{status}] {name}: {scrub(detail)}", flush=True)


class GeminiError(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None):
        super().__init__(msg)
        self.code, self.retryable, self.wait = code, retryable, wait


# ------------------------------------------------------------------ Gemini
def gemini_call(model, body, key):
    req = urllib.request.Request(
        f"{BASE}/models/{model}:generateContent",
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
        raise GeminiError(f"HTTP {e.code}: {text[:300]}", e.code,
                          e.code in (408, 429, 500, 502, 503, 504), wait)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GeminiError(f"network: {e}", None, True, None)


def gemini_pcm(text, voice, model, key, retries=5):
    """Returns (pcm_bytes, sample_rate, attempts). Retries on 429/5xx/empty audio."""
    prompt = ("Read the following Hindi story narration slowly and clearly, like a warm "
              "storyteller. Only speak the text after the colon.\n\n" + text)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    last, attempt = None, 0
    for attempt in range(1, retries + 1):
        try:
            out = gemini_call(model, body, key)
            cands = out.get("candidates") or []
            parts = []
            if cands:
                parts = (cands[0].get("content") or {}).get("parts") or []
            inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
            if not inline:
                why = cands[0].get("finishReason") if cands else out.get("promptFeedback")
                raise GeminiError(f"no audio in the answer ({why})", 200, True)
            pcm = base64.b64decode(inline["data"])
            if len(pcm) < 4000:
                raise GeminiError("audio too short", 200, True)
            m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
            return pcm, int(m.group(1)) if m else 24000, attempt
        except GeminiError as e:
            last = e
            if not e.retryable or attempt == retries:
                break
            wait = min(90, e.wait if e.wait else 4 * attempt * attempt) + random.uniform(0, 1)
            print(f"   retry {attempt}/{retries - 1} in {wait:.0f}s: {scrub(e)[:120]}", flush=True)
            SLEEP(wait)
    raise GeminiError(f"{last} (gave up after {attempt} attempts)", last.code, last.retryable)


def pcm_to_mp3(pcm, rate, path):
    raw = path + ".pcm"
    with open(raw, "wb") as f:
        f.write(pcm)
    subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
                           "-ac", "1", "-i", raw, "-ar", "24000", "-codec:a", "libmp3lame", path])
    os.remove(raw)


# ------------------------------------------------------------------ Edge
def edge_make(text, voice, path, rate="-8%"):
    import edge_tts

    async def go():
        last = None
        for a in range(4):
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(path)
                if os.path.getsize(path) > 1000:
                    return
            except Exception as e:
                last = e
            await asyncio.sleep(2 + a * 3)
        raise RuntimeError(f"Edge failed: {last}")
    asyncio.run(go())


def make_voice(text, path, key, model, gender="male", order=("gemini", "edge"), retries=5):
    """The same chain the movie uses: try engines in order, return the one that worked."""
    for eng in order:
        try:
            if eng == "gemini":
                if not key:
                    raise GeminiError("no key", None, False)
                pcm, rate, _ = gemini_pcm(text, GV[gender], model, key, retries)
                pcm_to_mp3(pcm, rate, path)
            else:
                edge_make(text, EV[gender], path)
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return eng
        except Exception as e:
            print(f"   {eng} failed: {scrub(e)[:160]}", flush=True)
    return None


# ------------------------------------------------------------------ audio checks
def audio_stats(path):
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    ).decode().strip())
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", p.stderr)
    return dur, (float(m.group(1)) if m else None)


def live_test(name, text, voice, model, key, path, retries):
    t = time.time()
    try:
        pcm, rate, att = gemini_pcm(text, voice, model, key, retries)
        pcm_to_mp3(pcm, rate, path)
        dur, mean = audio_stats(path)
        ok = dur >= 0.8 and (mean is None or mean > -55)
        rec(name, "PASS" if ok else "FAIL",
            f"model={model} voice={voice} {dur:.1f}s mean={mean}dB attempts={att} took={time.time() - t:.0f}s"
            + ("" if ok else " (audio is silent or too short)"))
        return ok, dur
    except Exception as e:
        rec(name, "FAIL", f"model={model} voice={voice}: {e}")
        return False, 0.0


# ------------------------------------------------------------------ offline retry tests
def retry_unit_tests():
    global gemini_call, SLEEP
    real_call, real_sleep = gemini_call, SLEEP
    SLEEP = lambda s: None
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(24000))
    good = {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "audio/L16;codec=pcm;rate=24000",
        "data": base64.b64encode(tone).decode()}}]}}]}
    e429 = lambda: GeminiError("HTTP 429: quota", 429, True, 1)
    e500 = lambda: GeminiError("HTTP 500: internal", 500, True, None)
    e403 = lambda: GeminiError("HTTP 403: denied", 403, False, None)
    cases = [
        ("429, 429, then OK", [e429(), e429(), good], 5, True, 3),
        ("403 must NOT be retried", [e403()], 5, False, 1),
        ("empty audio twice, then OK", [{}, {"candidates": [{"finishReason": "OTHER"}]}, good], 5, True, 3),
        ("500 every time: gives up after 5", [e500() for _ in range(5)], 5, False, 5),
        ("works first time", [good], 5, True, 1),
    ]
    try:
        for name, seq, retries, want_ok, want_calls in cases:
            it, calls = iter(seq), [0]

            def fake(model, body, key, it=it, calls=calls):
                calls[0] += 1
                x = next(it)
                if isinstance(x, Exception):
                    raise x
                return x
            gemini_call = fake
            try:
                _, _, att = gemini_pcm("test", "Charon", "m", "k", retries)
                ok = want_ok
            except GeminiError:
                ok = not want_ok
            good_case = ok and calls[0] == want_calls
            rec(f"retry logic: {name}", "PASS" if good_case else "FAIL",
                f"calls made={calls[0]}, expected={want_calls}")
    finally:
        gemini_call, SLEEP = real_call, real_sleep


# ------------------------------------------------------------------ main
def main():
    global KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--voices", default="Charon,Kore")
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--burst", type=int, default=0, help="number of quick calls to find the rate limit")
    ap.add_argument("--plan", default="")
    ap.add_argument("--shots", type=int, default=0)
    ap.add_argument("--all-models", action="store_true")
    ap.add_argument("--offline", action="store_true", help="only the tests that need no internet")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any test FAILS")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    KEY = os.environ.get("GEMINI_API_KEY", "").strip()

    # 1. tools
    for tool in ("ffmpeg", "ffprobe"):
        ok = subprocess.run([tool, "-version"], capture_output=True).returncode == 0
        rec(f"tool: {tool}", "PASS" if ok else "FAIL", "found" if ok else "missing")
    try:
        import edge_tts  # noqa
        rec("tool: edge-tts", "PASS", "installed")
    except Exception:
        rec("tool: edge-tts", "FAIL" if not a.offline else "WARN", "not installed (pip install edge-tts)")
    rec("secret: GEMINI_API_KEY", "PASS" if KEY else "FAIL",
        f"set ({len(KEY)} characters)" if KEY else "empty: add it in Settings > Secrets > Actions")

    # 2. retry logic
    retry_unit_tests()

    gemini_ok = edge_ok = False
    if not a.offline:
        # 3. key and models
        names, primary = [], a.model
        if KEY:
            try:
                req = urllib.request.Request(f"{BASE}/models?pageSize=200", headers={"x-goog-api-key": KEY})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                names = [m["name"].split("/")[-1] for m in data.get("models", []) if "tts" in m["name"].lower()]
                rec("key is valid, models listed", "PASS", "TTS models: " + (", ".join(names) or "none"))
            except urllib.error.HTTPError as e:
                rec("key is valid, models listed", "FAIL",
                    f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
            except Exception as e:
                rec("key is valid, models listed", "FAIL", str(e))
            if not primary:
                primary = next((m for m in PREFERRED if m in names), names[0] if names else PREFERRED[0])

            # 4. voices
            for v in [x.strip() for x in a.voices.split(",") if x.strip()]:
                ok, _ = live_test(f"gemini voice {v}", HI_SHORT, v, primary, KEY,
                                  os.path.join(a.out, f"gemini_{v}.mp3"), a.retries)
                gemini_ok = gemini_ok or ok
                SLEEP(3)

            # other models (optional)
            if a.all_models:
                for m in [n for n in names if n != primary][:2]:
                    ok, _ = live_test(f"other model {m}", HI_SHORT, GV["male"], m, KEY,
                                      os.path.join(a.out, f"model_{m}.mp3"), a.retries)
                    if not ok:
                        RESULTS[-1] = (RESULTS[-1][0], "WARN", RESULTS[-1][2])
                    SLEEP(3)

            # 5. long text
            if gemini_ok:
                ok, dur = live_test("gemini long narration", HI_LONG, GV["male"], primary, KEY,
                                    os.path.join(a.out, "gemini_long.mp3"), a.retries)
                if ok and dur < len(HI_LONG) / 30:
                    rec("long narration not cut short", "WARN", f"only {dur:.1f}s for {len(HI_LONG)} characters")
                elif ok:
                    rec("long narration not cut short", "PASS", f"{dur:.1f}s for {len(HI_LONG)} characters")

            # 6. real shots
            if a.plan and a.shots > 0 and gemini_ok:
                try:
                    plan = json.load(open(a.plan, encoding="utf-8"))
                    shots = plan if isinstance(plan, list) else (plan.get("shots") or plan.get("scenes") or [])
                    n = 0
                    for s in shots:
                        txt = next((s[k] for k in ("narration", "text", "voiceover") if isinstance(s.get(k), str) and s[k].strip()), "")
                        if not txt:
                            continue
                        n += 1
                        live_test(f"plan shot {s.get('id', n)}", txt, GV["male"], primary, KEY,
                                  os.path.join(a.out, f"plan_shot_{n:02d}.mp3"), a.retries)
                        SLEEP(3)
                        if n >= a.shots:
                            break
                except Exception as e:
                    rec("plan file", "FAIL", str(e))

            # 7. burst
            if a.burst > 0 and gemini_ok:
                ok_n, limited = 0, 0
                for i in range(a.burst):
                    try:
                        gemini_pcm(HI_SHORT, GV["male"], primary, KEY, retries=1)
                        ok_n += 1
                    except GeminiError as e:
                        if e.code == 429:
                            limited += 1
                rec("rate limit burst", "WARN" if limited else "PASS",
                    f"{a.burst} quick calls: {ok_n} ok, {limited} hit the free-tier limit (429)"
                    + (". In tts.py keep --pause at 10 or more." if limited else ""))
        else:
            rec("gemini tests", "SKIP", "no key")

        # 8. Edge alone
        for g in ("male", "female"):
            p = os.path.join(a.out, f"edge_{g}.mp3")
            try:
                edge_make(HI_SHORT, EV[g], p)
                dur, mean = audio_stats(p)
                ok = dur >= 0.8
                edge_ok = edge_ok or ok
                rec(f"edge voice {g}", "PASS" if ok else "FAIL", f"{dur:.1f}s mean={mean}dB")
            except Exception as e:
                rec(f"edge voice {g}", "FAIL", str(e))

        # 9. fallback chain with a wrong key
        p = os.path.join(a.out, "fallback.mp3")
        eng = make_voice(HI_SHORT, p, "WRONG_KEY_FOR_TEST", primary or PREFERRED[0], "male", retries=2)
        rec("fallback: wrong Gemini key -> Edge takes over",
            "PASS" if eng == "edge" else "FAIL", f"engine used: {eng}")

    # report
    if a.offline:
        verdict = "OFFLINE CHECK ONLY"
    elif gemini_ok:
        verdict = "GEMINI VOICE WORKS" + ("" if edge_ok else " (Edge backup NOT working)")
    elif edge_ok:
        verdict = "GEMINI DOES NOT WORK, EDGE BACKUP WORKS (the movie will still get a voice)"
    else:
        verdict = "NOTHING WORKS: no voice engine is available"
    counts = {s: sum(1 for r in RESULTS if r[1] == s) for s in ("PASS", "FAIL", "WARN", "SKIP")}
    with open(os.path.join(a.out, "tts2_report.md"), "w", encoding="utf-8") as f:
        f.write(f"### Voice check\n\n**Verdict: {verdict}**\n\n")
        f.write(f"PASS {counts['PASS']} | FAIL {counts['FAIL']} | WARN {counts['WARN']} | SKIP {counts['SKIP']}\n\n")
        f.write("| Result | Test | Detail |\n|---|---|---|\n")
        for n, s, d in RESULTS:
            f.write(f"| {s} | {n} | {d.replace('|', '/')[:240]} |\n")
    print("\nVERDICT:", verdict)

    if not a.offline and not (gemini_ok or edge_ok):
        sys.exit(1)
    if a.strict and counts["FAIL"]:
        sys.exit(1)
    if a.offline and counts["FAIL"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
