#!/usr/bin/env python3
"""shorts_assemble.py - final video, subtitle FILES (not burned in), and upload data.

  python3 shorts_assemble.py --plan scene_plan_timed.json --timeline timeline.json \
      --video out/video_all.mp4 --out out/final_shorts.mp4 [--music music/bg.mp3]

Makes in the output folder:
  final_shorts.mp4     vertical 1080x1920, exactly the planned length, NO subtitles on the picture
  subtitles.srt / .vtt subtitle files for YouTube (upload them with the video as captions)
  youtube_meta.json    title, description, tags, flags for the later upload step
  thumbnail.jpg        frame from the most eye-catching shot
"""
import argparse, json, os, re, shutil, subprocess, sys, textwrap


def run(cmd):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd[:14]), "..." if len(cmd) > 14 else "", flush=True)
    subprocess.run(cmd, check=True)


def probe(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", path]).decode().strip())


def srt_time(t, sep=","):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def caption_chunks(text, max_line=36):
    """Split a narration into short caption texts (<= 2 lines of max_line characters)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", text) if p.strip()]
    chunks = []
    for p in parts:
        nlines = max(1, -(-len(p) // max_line))                 # lines needed
        width = -(-len(p) // nlines) + 5                        # even lines, no one-word orphan
        lines = textwrap.wrap(p, width) or [p]
        for i in range(0, len(lines), 2):
            chunks.append("\n".join(lines[i:i + 2]))
    return chunks


def build_captions(plan, tl):
    tempo = tl.get("tempo", 1.0)
    cues = []
    for s, t in zip(plan["shots"], tl["shots"]):
        text = s["narration"].strip()
        start = t["start_time"] + t["lead"]
        span = t["voice_seconds"] / tempo
        chunks = caption_chunks(text)
        total = sum(len(c) for c in chunks) or 1
        cur = start
        for c in chunks:
            d = span * len(c) / total
            cues.append((cur, min(cur + d, t["start_time"] + t["duration"]), c))
            cur += d
    return cues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_timed.json")
    ap.add_argument("--timeline", default="timeline.json")
    ap.add_argument("--video", required=True, help="joined silent video from the render parts")
    ap.add_argument("--out", required=True)
    ap.add_argument("--music", default="")
    ap.add_argument("--upscale", default="1080x1920", help="final size, empty = keep the render size")
    ap.add_argument("--out-fps", type=int, default=30)
    a = ap.parse_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    tl = json.load(open(a.timeline, encoding="utf-8"))
    target = float(tl["total_seconds"])
    if not os.path.exists(a.video):
        sys.exit(f"FAIL: video not found: {a.video}")
    out_path = os.path.abspath(a.out)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    # ---- narration: every shot padded to its exact length, then joined
    tempo = tl.get("tempo", 1.0)
    parts = []
    for t in tl["shots"]:
        wav = os.path.join(work, f"shot_{int(t['id']):02d}.wav")
        lead_ms = int(round(t["lead"] * 1000))
        if t.get("audio") and os.path.exists(t["audio"]):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(t["audio"]), "-af",
                 f"atempo={tempo:.4f},adelay={lead_ms}|{lead_ms},apad=whole_dur={t['duration']:.3f}",
                 "-t", f"{t['duration']:.3f}", "-ar", "44100", "-ac", "2", wav])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{t['duration']:.3f}", wav])
        parts.append(f"file '{wav}'")
    open(os.path.join(work, "list.txt"), "w").write("\n".join(parts))
    narr = os.path.join(work, "narration.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "list.txt"),
         "-c", "copy", narr])
    nd = probe(narr)
    if abs(nd - target) > 0.3:
        sys.exit(f"FAIL: narration is {nd:.2f} s but the plan says {target:.2f} s")

    # ---- video
    vf = []
    if a.upscale:
        w, h = a.upscale.split("x")
        vf.append(f"scale={w}:{h}:flags=lanczos")
    vf += [f"fps={a.out_fps}", "fade=t=in:st=0:d=0.3", f"fade=t=out:st={target - 0.5:.2f}:d=0.5"]
    inputs = ["-i", os.path.abspath(a.video), "-i", narr]
    music = os.path.abspath(a.music) if a.music else ""
    if music and os.path.exists(music):
        inputs += ["-stream_loop", "-1", "-i", music]
        af = ["-filter_complex",
              f"[1:a]volume=1.0[v];[2:a]volume=0.07,afade=t=in:d=1.5,afade=t=out:st={target - 2:.2f}:d=2[m];"
              f"[v][m]amix=inputs=2:duration=first:normalize=0,afade=t=out:st={target - 0.5:.2f}:d=0.5[a]",
              "-map", "0:v", "-map", "[a]"]
    else:
        af = ["-filter_complex", f"[1:a]afade=t=out:st={target - 0.5:.2f}:d=0.5[a]", "-map", "0:v", "-map", "[a]"]
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, *af, "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "medium",
         "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{target:.3f}",
         "-movflags", "+faststart", out_path])
    fd = probe(out_path)
    if abs(fd - target) > 0.6:
        sys.exit(f"FAIL: final video is {fd:.2f} s but must be {target:.2f} s")

    # ---- subtitle files (NOT burned into the picture)
    cues = build_captions(plan, tl)
    with open(os.path.join(out_dir, "subtitles.srt"), "w", encoding="utf-8") as f:
        for i, (s, e, c) in enumerate(cues, start=1):
            f.write(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{c}\n\n")
    with open(os.path.join(out_dir, "subtitles.vtt"), "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for s, e, c in cues:
            f.write(f"{srt_time(s, '.')} --> {srt_time(e, '.')}\n{c}\n\n")

    # ---- thumbnail and upload data
    th = next((t for t in tl["shots"] if t.get("thumb")), tl["shots"][len(tl["shots"]) // 2])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{th['start_time'] + th['duration'] / 2:.2f}", "-i", out_path,
         "-frames:v", "1", "-q:v", "2", os.path.join(out_dir, "thumbnail.jpg")])
    desc = "\n\n".join(x for x in [plan.get("description", ""), plan.get("disclaimer", ""), plan.get("credits", "")] if x)
    tags = list(plan.get("tags") or [])
    meta = {"title": plan["title"][:100], "description": desc + "\n\n#Shorts #MoneyLessons #FinancialLiteracy",
            "tags": tags, "categoryId": "27", "language": "en" if plan.get("language") == "English" else "hi",
            "privacyStatus": "private", "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True, "captionFile": "subtitles.srt", "thumbnail": "thumbnail.jpg",
            "durationSeconds": round(fd, 2)}
    json.dump(meta, open(os.path.join(out_dir, "youtube_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"DONE: {out_path} ({fd:.2f} s), {len(cues)} caption cues")


if __name__ == "__main__":
    main()
