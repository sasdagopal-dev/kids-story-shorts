"""Step 1: make Hindi voice for every shot, measure it, and build the timeline.

Outputs (in --out):
  audio/shot_XX.mp3   voice per shot
  timeline.json       frame ranges per shot, fps, resolution, samples
  subs.srt            subtitles (Hindi)
Also writes `shards=[0,1,...]` to $GITHUB_OUTPUT for the parallel render jobs.
"""
import argparse
import asyncio
import json
import math
import os
import re
import subprocess

import edge_tts

LEAD = 0.30   # seconds of silence before the voice starts in each shot
TAIL = 0.55   # seconds of silence after the voice ends

QUALITY = {   # (width, height, samples) for landscape
    "draft": (640, 360, 8),
    "standard": (960, 540, 16),
    "high": (1280, 720, 24),
}


def probe(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", path])
    return float(out.decode().strip())


async def synth(text, voice, rate, path):
    last = None
    for attempt in range(4):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(path)
            if os.path.getsize(path) > 1000:
                return
        except Exception as e:  # network hiccup
            last = e
        await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"TTS failed for {path}: {last}")


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def split_lines(text, max_chars=46):
    parts = [p.strip() for p in re.split(r"(?<=[।\.\?!,])\s+", text) if p.strip()]
    merged = []
    for p in parts:
        if merged and (len(merged[-1]) < 14 or len(merged[-1]) + len(p) + 1 <= max_chars):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--story", required=True)
    ap.add_argument("--out", default="out")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", default="standard", choices=list(QUALITY))
    ap.add_argument("--aspect", default="landscape", choices=["landscape", "vertical"])
    ap.add_argument("--shards", type=int, default=6)
    a = ap.parse_args()

    story = json.load(open(a.story, encoding="utf-8"))
    os.makedirs(os.path.join(a.out, "audio"), exist_ok=True)
    voice = story.get("voice", "hi-IN-MadhurNeural")
    rate = story.get("rate", "+0%")

    w, h, samples = QUALITY[a.quality]
    if a.aspect == "vertical":
        w, h = h, w

    shots, srt, idx = [], [], 1
    frame, t0, total_voice = 0, 0.0, 0.0
    for s in story["shots"]:
        path = os.path.join(a.out, "audio", f"shot_{s['id']:02d}.mp3")
        asyncio.run(synth(s["narration"], voice, rate, path))
        vdur = probe(path)
        total_voice += vdur
        dur = max(vdur + LEAD + TAIL, float(s.get("min_seconds", 0)))
        n = int(math.ceil(dur * a.fps))
        dur = n / a.fps
        shots.append({
            "id": s["id"], "title": s.get("title", ""), "audio": path,
            "voice_seconds": round(vdur, 3), "duration": round(dur, 3),
            "start_time": round(t0, 3), "start_frame": frame, "end_frame": frame + n,
            "thumb": bool(s.get("camera", {}).get("thumb")),
        })
        # subtitles, time-share by character count
        lines = split_lines(s["narration"])
        total_chars = sum(len(x) for x in lines) or 1
        cur = t0 + LEAD
        for ln in lines:
            span = vdur * len(ln) / total_chars
            srt.append(f"{idx}\n{srt_time(cur)} --> {srt_time(cur + span)}\n{ln}\n")
            idx += 1
            cur += span
        frame += n
        t0 += dur

    timeline = {
        "fps": a.fps, "width": w, "height": h, "samples": samples,
        "aspect": a.aspect, "total_frames": frame,
        "total_seconds": round(t0, 2), "voice": voice, "lead": LEAD,
        "shots": shots,
    }
    json.dump(timeline, open(os.path.join(a.out, "timeline.json"), "w"), indent=1)
    open(os.path.join(a.out, "subs.srt"), "w", encoding="utf-8").write("\n".join(srt))

    print(f"Voice total {total_voice:.1f}s | video {t0:.1f}s | frames {frame} @ {a.fps}fps")
    print(f"Render size {w}x{h}, {samples} samples, {a.shards} parallel shards")
    est = frame / a.shards * 5 / 60
    print(f"Rough estimate at 5 s/frame: about {est:.0f} minutes per shard")

    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("shards=" + json.dumps(list(range(a.shards))) + "\n")


if __name__ == "__main__":
    main()
