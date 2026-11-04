"""Step 3: join all rendered frames, Hindi voice, subtitles and optional music.

python scripts/assemble.py --plan out --frames frames --story story/finance_story.json --final final
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def run(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, cwd=cwd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="out")
    ap.add_argument("--frames", default="frames")
    ap.add_argument("--story", required=True)
    ap.add_argument("--final", default="final")
    ap.add_argument("--music", default="assets/music/bgm.mp3")
    ap.add_argument("--burn-subs", default="true")
    a = ap.parse_args()

    plan = os.path.abspath(a.plan)
    frames = os.path.abspath(a.frames)
    final = os.path.abspath(a.final)
    os.makedirs(final, exist_ok=True)
    tl = json.load(open(os.path.join(plan, "timeline.json"), encoding="utf-8"))
    story = json.load(open(a.story, encoding="utf-8"))
    fps, total = tl["fps"], tl["total_frames"]

    missing = [g for g in range(total) if not os.path.exists(os.path.join(frames, f"f_{g:06d}.png"))]
    if missing:
        print(f"ERROR: {len(missing)} frames missing, first few: {missing[:10]}")
        sys.exit(1)

    # ---- narration: each shot padded to its exact duration, then concatenated
    work = os.path.join(final, "_work")
    os.makedirs(work, exist_ok=True)
    lead_ms = int(tl["lead"] * 1000)
    lst = []
    for s in tl["shots"]:
        wav = os.path.join(work, f"shot_{s['id']:02d}.wav")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(plan, os.path.relpath(s["audio"], "out")) if not os.path.exists(s["audio"]) else s["audio"],
             "-af", f"adelay={lead_ms}|{lead_ms},apad=whole_dur={s['duration']:.3f}",
             "-t", f"{s['duration']:.3f}", "-ar", "44100", "-ac", "2", wav])
        lst.append(f"file '{wav}'")
    listfile = os.path.join(work, "list.txt")
    open(listfile, "w").write("\n".join(lst))
    narr = os.path.join(work, "narration.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", narr])

    # ---- video
    if tl["aspect"] == "vertical":
        W, H, fsize = 1080, 1920, 11
    else:
        W, H, fsize = 1920, 1080, 14
    shutil.copy(os.path.join(plan, "subs.srt"), os.path.join(work, "subs.srt"))
    vf = f"scale={W}:{H}:flags=lanczos,fps=30"
    if a.burn_subs.lower() == "true":
        style = (f"FontName=Noto Sans Devanagari,FontSize={fsize},PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=26")
        vf += f",subtitles=subs.srt:force_style='{style}'"

    inputs = ["-framerate", str(fps), "-i", os.path.join(frames, "f_%06d.png"), "-i", narr]
    af = []
    if os.path.exists(a.music):
        inputs += ["-stream_loop", "-1", "-i", a.music]
        af = ["-filter_complex",
              "[1:a]volume=1.0[v];[2:a]volume=0.10[m];[v][m]amix=inputs=2:duration=first:normalize=0[a]",
              "-map", "0:v", "-map", "[a]"]
    else:
        af = ["-map", "0:v", "-map", "1:a"]

    out_mp4 = os.path.join(final, "story_final.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, *af, "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-t", f"{tl['total_seconds']:.2f}",
         "-movflags", "+faststart", out_mp4], cwd=work)

    # ---- thumbnail
    thumb_shot = next((s for s in tl["shots"] if s["thumb"]), tl["shots"][len(tl["shots"]) // 2])
    g = (thumb_shot["start_frame"] + thumb_shot["end_frame"]) // 2
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(frames, f"f_{g:06d}.png"),
         "-vf", f"scale={W}:{H}:flags=lanczos", "-q:v", "2", os.path.join(final, "thumbnail.jpg")])

    # ---- title + description
    desc = "\n\n".join([story.get("description", ""), story.get("disclaimer", ""), story.get("credits", "")])
    open(os.path.join(final, "title.txt"), "w", encoding="utf-8").write(story["title"])
    open(os.path.join(final, "description.txt"), "w", encoding="utf-8").write(desc)
    shutil.copy(os.path.join(plan, "subs.srt"), os.path.join(final, "subtitles_hi.srt"))
    shutil.rmtree(work, ignore_errors=True)
    print("DONE:", out_mp4, f"({tl['total_seconds']} s)")


if __name__ == "__main__":
    main()
