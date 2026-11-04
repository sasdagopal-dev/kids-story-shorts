#!/usr/bin/env python3
"""tts3.py - ONE Gemini call for the WHOLE story, ONE voice, ONE mp3. Test version.

  python3 tts3.py --plan scene_plan_part1.json --voice Charon --model gemini-2.5-flash-preview-tts

* reads every shot's narration from the plan and joins them (one paragraph per shot)
* sends it to Gemini TTS in a single request
* if that request FAILS (429, server error, "tried to generate text") it repeats the same
  single request, up to --attempts times. It never splits the story into several calls.
Writes: out/story_voice.mp3, out/story_text.txt, out/report.md
The key is never printed.
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"
TEXT_KEYS = ("narration", "text", "voiceover", "voice_over", "dialogue")
SLEEP = time.sleep


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None):
        super().__init__(msg)
        self.code, self.retryable, self.wait = code, retryable, wait


def gcall(model, body, key):
    req = urllib.request.Request(
        f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
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
        daily = "perday" in text.lower() or "per day" in text.lower()
        retry = (e.code in (408, 429, 500, 502, 503, 504) and not daily) or \
                (e.code == 400 and "generate text" in text)
        raise GErr(f"HTTP {e.code}: {text[:400]}", e.code, retry, wait)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GErr(f"network: {e}", None, True, None)


def get_text(shot):
    for k in TEXT_KEYS:
        v = shot.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def load_story(path):
    with open(path, encoding="utf-8") as f:
        if path.lower().endswith((".txt", ".md")):
            return [p.strip() for p in f.read().split("\n\n") if p.strip()]
        plan = json.load(f)
    shots = plan if isinstance(plan, list) else (plan.get("shots") or plan.get("scenes") or [])
    return [t for t in (get_text(s) for s in shots) if t]


def probe(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", path]).decode().strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_part1.json", help="plan .json, or a .txt with paragraphs")
    ap.add_argument("--voice", default="Charon")
    ap.add_argument("--model", default="gemini-2.5-flash-preview-tts")
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("FAIL: GEMINI_API_KEY is empty. Add it under Settings > Secrets > Actions.")
    paras = load_story(a.plan)
    if not paras:
        sys.exit(f"FAIL: no narration found in {a.plan}")
    story = "\n\n".join(paras)
    open(os.path.join(a.out, "story_text.txt"), "w", encoding="utf-8").write(story)
    print(f"Story: {len(paras)} paragraphs, {len(story)} characters. Sending ONE request to {a.model} ({a.voice}).", flush=True)

    prompts = [f"Say slowly and warmly, like a storyteller, with a clear short pause between paragraphs:\n\n{story}",
               f"Say with a short pause between paragraphs:\n\n{story}", story]
    mp3 = os.path.join(a.out, "story_voice.mp3")
    last, used = None, 0
    for attempt in range(1, a.attempts + 1):
        used = attempt
        body = {"contents": [{"parts": [{"text": prompts[(attempt - 1) % 3]}]}],
                "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": a.voice}}}}}
        t = time.time()
        try:
            out = gcall(a.model, body, key)
            cands = out.get("candidates") or []
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
            if not inline:
                raise GErr(f"no audio in the answer ({cands[0].get('finishReason') if cands else 'no candidates'})", 200, True)
            pcm = base64.b64decode(inline["data"])
            m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
            raw = mp3 + ".pcm"
            open(raw, "wb").write(pcm)
            subprocess.check_call(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", m.group(1) if m else "24000",
                                   "-ac", "1", "-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                                   "-ar", "24000", "-codec:a", "libmp3lame", mp3])
            os.remove(raw)
            dur = probe(mp3)
            cps = len(story) / dur
            notes = []
            if not (5 <= cps <= 30):
                notes.append(f"WARNING: {cps:.1f} characters per second looks wrong, the audio may be cut short or padded")
            if dur > 175:
                notes.append("WARNING: longer than 175 s. Too long for a YouTube Short, shorten the narration")
            with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
                f.write("### Single-call story voice\n\n**OK**\n\n")
                f.write(f"- Model: {a.model}\n- Voice: {a.voice}\n- Paragraphs: {len(paras)}\n")
                f.write(f"- Characters: {len(story)}\n- Audio length: {dur:.1f} s ({cps:.1f} characters per second)\n")
                f.write(f"- Requests that succeeded: 1 (failed attempts before it: {attempt - 1})\n- Took: {time.time() - t:.0f} s\n")
                for n in notes:
                    f.write(f"\n{n}\n")
            print(f"OK: {dur:.1f}s audio, attempt {attempt}", *notes)
            return
        except GErr as e:
            last = e
            print(f"Attempt {attempt}/{a.attempts} failed: {str(e)[:300].replace(key, '***')}", flush=True)
            if not e.retryable or attempt == a.attempts:
                break
            wait = min(120, (e.wait or 0) + 5, ) if e.code == 429 else 10 * attempt
            print(f"Waiting {wait:.0f}s, then the same single request again", flush=True)
            SLEEP(wait)
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(f"### Single-call story voice\n\n**FAILED** after {used} attempt(s)\n\nLast error: {str(last)[:500]}\n")
    sys.exit("FAIL: Gemini did not return the story voice. See report.md for the last error.")


if __name__ == "__main__":
    main()
