#!/usr/bin/env python3
"""Join the rendered video with Hindi narration, subtitles and optional music.

Called by the workflow:
  python3 assemble_movie.py --plan scene_plan_timed.json --video out/video_all.mp4 \
      --fps 12 --out out/movie_final.mp4 --music "path/or/empty"

Needs in the current folder (downloaded from the 'plan' artifact):
  scene_plan_timed.json, timeline.json, subs.srt, audio/shot_XX.mp3
Writes: the final mp4 and out/subs.ass
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def run(cmd, cwd=None):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd)


def find_shots(plan):
    if isinstance(plan, list):
        return plan
    for k in ("shots", "scenes"):
        if isinstance(plan.get(k), list):
            return plan[k]
    sys.exit("Plan has no 'shots' list")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="scene_plan_timed.json")
    ap.add_argument("--video", required=True, help="joined silent video")
    ap.add_argument("--out", required=True, help="final mp4 path")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--music", default="")
    ap.add_argument("--timeline", default="timeline.json")
    ap.add_argument("--srt", default="subs.srt")
    ap.add_argument("--burn-subs", default="true")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        sys.exit(f"Video not found: {a.video}")

    with open(a.plan, encoding="utf-8") as f:
        plan = json.load(f)
    shots = find_shots(plan)
    tl = {}
    if os.path.exists(a.timeline):
        with open(a.timeline, encoding="utf-8") as f:
            tl = json.load(f)

    total_seconds = tl.get("total_seconds") or plan.get("total_seconds")
    aspect = tl.get("aspect", "landscape")
    out_path = os.path.abspath(a.out)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    video = os.path.abspath(a.video)

    work = os.path.join(out_dir, "_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    # ---- narration: each shot padded to its exact duration, then joined
    parts = []
    for s in shots:
        dur = float(s["duration"])
        lead_ms = int(float(s.get("lead", 0.3)) * 1000)
        wav = os.path.join(work, f"shot_{int(s['id']):02d}.wav")
        audio = s.get("audio")
        if audio and os.path.exists(audio):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(audio),
                 "-af", f"adelay={lead_ms}|{lead_ms},apad=whole_dur={dur:.3f}",
                 "-t", f"{dur:.3f}", "-ar", "44100", "-ac", "2", wav])
        else:  # silent shot
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", wav])
        parts.append(f"file '{wav}'")
    listfile = os.path.join(work, "list.txt")
    with open(listfile, "w") as f:
        f.write("\n".join(parts))
    narr = os.path.join(work, "narration.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listfile, "-c", "copy", narr])

    # ---- subtitles (also saved as .ass)
    have_srt = os.path.exists(a.srt) and os.path.getsize(a.srt) > 0
    if have_srt:
        shutil.copy(a.srt, os.path.join(work, "subs.srt"))
        subprocess.call(["ffmpeg", "-y", "-loglevel", "error", "-i", a.srt,
                         os.path.join(out_dir, "subs.ass")])

    # ---- final video
    W, H, fsize = (1080, 1920, 11) if aspect == "vertical" else (1920, 1080, 14)
    vf = f"scale={W}:{H}:flags=lanczos,fps=30"
    if have_srt and a.burn_subs.lower() == "true":
        style = (f"FontName=Noto Sans Devanagari,FontSize={fsize},PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=26")
        vf += f",subtitles=subs.srt:force_style='{style}'"

    inputs = ["-i", video, "-i", narr]
    music = os.path.abspath(a.music) if a.music else ""
    if music and os.path.exists(music):
        inputs += ["-stream_loop", "-1", "-i", music]
        amap = ["-filter_complex",
                "[1:a]volume=1.0[v];[2:a]volume=0.10[m];"
                "[v][m]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        amap = ["-map", "0:v", "-map", "1:a"]

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, *amap, "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k"]
    if total_seconds:
        cmd += ["-t", f"{float(total_seconds):.2f}"]
    cmd += ["-movflags", "+faststart", out_path]
    run(cmd, cwd=work)

    shutil.rmtree(work, ignore_errors=True)
    print("DONE:", out_path)


if __name__ == "__main__":
    main()
