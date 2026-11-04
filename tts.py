#!/usr/bin/env python3
"""Make Hindi narration for every shot and write the timed plan.

Called by the workflow like this:
  python3 tts.py --plan P.json --gender male --engine auto --rate=-8% [--on-fail silent | --no-audio] [--fps 12]

Outputs (in the current folder):
  scene_plan_timed.json   the plan + per-shot timing (seconds and frames)
  tts_summary.md          what happened for each shot
  narration_preview.mp3   all shot voices joined together
  audio/shot_XX.mp3       one voice file per shot
"""
import argparse
import asyncio
import base64
import json
import math
import os
import re
import subprocess
import sys
import urllib.request

LEAD = 0.30   # silence before the voice in each shot (seconds)
TAIL = 0.55   # silence after the voice
CHARS_PER_SEC = 12.0   # used to guess duration when there is no audio

EDGE_VOICES = {"male": "hi-IN-MadhurNeural", "female": "hi-IN-SwaraNeural"}
GEMINI_VOICES = {"male": "Charon", "female": "Kore"}
GEMINI_MODEL = "gemini-2.5-flash-preview-tts"

TEXT_KEYS = ("narration", "text", "voiceover", "voice_over", "dialogue")
LIST_KEYS = ("shots", "scenes")

QUALITY = {   # (width, height, samples) for landscape
    "fast": (640, 360, 8),
    "balanced": (960, 540, 16),
    "high": (1280, 720, 24),
}


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
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path])
    return float(out.decode().strip())


# ---------------------------------------------------------------- engines
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


def edge_synth(text, gender, rate, path):
    asyncio.run(_edge(text, EDGE_VOICES[gender], rate, path))


def gemini_synth(text, gender, rate, path):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": GEMINI_VOICES[gender]}}},
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm = path + ".pcm"
    with open(pcm, "wb") as f:
        f.write(base64.b64decode(b64))
    subprocess.check_call([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000",
        "-ac", "1", "-i", pcm, "-codec:a", "libmp3lame", path])
    os.remove(pcm)


def make_audio(text, a, path):
    """Returns the name of the engine that worked, or None."""
    order = {"edge": ["edge"], "gemini": ["gemini"],
             "auto": ["edge"] + (["gemini"] if os.environ.get("GEMINI_API_KEY") else [])}[a.engine]
    for name in order:
        try:
            if name == "edge":
                edge_synth(text, a.gender, a.rate, path)
            else:
                gemini_synth(text, a.gender, a.rate, path)
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return name
        except Exception as e:
            print(f"  {name} failed: {e}", file=sys.stderr)
    return None


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


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--gender", default="male", choices=["male", "female"])
    ap.add_argument("--engine", default="auto", choices=["auto", "edge", "gemini"])
    ap.add_argument("--rate", default="+0%")
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

    rows, files = [], []
    tl_shots, srt, srt_idx = [], [], 1
    t0, frame = 0.0, 0
    for i, s in enumerate(shots, start=1):
        s.setdefault("id", i)
        text = get_text(s)
        path = os.path.join("audio", f"shot_{i:02d}.mp3")
        engine, vdur = None, 0.0

        if text and not a.no_audio:
            print(f"Shot {i}: making voice ({len(text)} chars)")
            engine = make_audio(text, a, path)
            if engine:
                vdur = probe(path)
                files.append(path)
            elif a.on_fail == "stop":
                sys.exit(f"Shot {i}: voice failed and --on-fail is 'stop'")
            else:
                print(f"Shot {i}: voice failed, continuing silent")

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
        rows.append((i, engine or ("silent" if text else "no text"), vdur, dur, n))
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

    # preview = all shot voices joined
    if files:
        with open("preview_list.txt", "w") as f:
            for p in files:
                f.write(f"file '{os.path.abspath(p)}'\n")
        subprocess.call([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", "preview_list.txt", "-c:a", "libmp3lame", "narration_preview.mp3"])
        os.remove("preview_list.txt")

    with open("tts_summary.md", "w", encoding="utf-8") as f:
        f.write(f"### Narration summary\n\nVoice: {a.gender} | engine: {a.engine} | rate: {a.rate}\n\n")
        f.write("| Shot | Voice | Voice sec | Shot sec | Frames |\n|---|---|---|---|---|\n")
        for i, eng, v, d, n in rows:
            f.write(f"| {i} | {eng} | {v:.1f} | {d:.1f} | {n} |\n")
        f.write(f"\nTotal: {t0:.1f}s, {frame} frames at {a.fps} fps\n")

    print(f"Done. {len(shots)} shots, {t0:.1f}s, {frame} frames @ {a.fps}fps")


if __name__ == "__main__":
    main()
