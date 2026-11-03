#!/usr/bin/env python3
"""
Builds the finished video from the rendered frames, the narration and the subtitles.

  python3 assemble.py --plan scene_plan_timed.json --frames out --fps 12 --out out/movie_final.mp4

Needs: out/frame_00001.png ... (from movie_render.py), audio files named in the timed plan,
ffmpeg with libass, and the font "Noto Sans Devanagari" (apt package fonts-noto-core).
Optional: --music path/to/music.mp3  (mixed quietly under the voice)
"""
import argparse
import glob
import json
import math
import os
import re
import subprocess


def run(cmd):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def shot_frames(shot, fps):
    return max(2, int(round(float(shot["seconds"]) * fps)))   # same rule as movie_render.py


def ass_time(t):
    t = max(0.0, t)
    return "%d:%02d:%05.2f" % (int(t // 3600), int(t % 3600 // 60), t % 60)


def split_phrases(text, max_words=6):
    """Cut a narration line into short subtitle pieces."""
    pieces = []
    for part in re.split(r"(?<=[\u0964?!.,;:])\s+", text.strip()):
        words = part.split()
        if not words:
            continue
        n = math.ceil(len(words) / float(max_words))
        size = math.ceil(len(words) / float(n))
        for i in range(0, len(words), size):
            pieces.append(" ".join(words[i:i + size]))
    merged = []                     # glue very short pieces to the next one
    i = 0
    while i < len(pieces):
        cur = pieces[i]
        if len(cur.split()) < 3 and i + 1 < len(pieces) and len((cur + " " + pieces[i + 1]).split()) <= 8:
            cur = cur + " " + pieces[i + 1]
            i += 1
        merged.append(cur)
        i += 1
    return merged


def build_ass(plan, fps, font, size, path):
    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,4,1,2,40,40,230,1"
        % (font, size),
        "Style: Title,%s,%d,&H0050D8FF,&H000000FF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,1,5,2,8,40,40,170,1"
        % (font, size + 16), "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events, t0, lead = [], 0.0, 0.3
    for shot in plan["shots"]:
        dur = shot_frames(shot, fps) / float(fps)
        text = (shot.get("narration_hi") or "").strip()
        if text and shot.get("audio_seconds"):
            chunks = split_phrases(text)
            weights = [max(1, len(c)) for c in chunks]
            total_w = float(sum(weights))
            cursor = t0 + lead
            for c, w in zip(chunks, weights):
                length = shot["audio_seconds"] * w / total_w
                clean = c.replace("{", "(").replace("}", ")")
                events.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s"
                              % (ass_time(cursor), ass_time(cursor + length), clean))
                cursor += length
        t0 += dur
    if plan.get("title_hi"):
        events.append("Dialogue: 1,%s,%s,Title,,0,0,0,,%s" % (ass_time(0.5), ass_time(3.8), plan["title_hi"]))
    if plan.get("outro_hi"):
        events.append("Dialogue: 1,%s,%s,Title,,0,0,0,,%s"
                      % (ass_time(max(0.0, t0 - 3.6)), ass_time(t0), plan["outro_hi"]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(head + events) + "\n")
    return t0


def build_narration(plan, fps, audio_dir_tmp, out_wav):
    os.makedirs(audio_dir_tmp, exist_ok=True)
    list_lines = []
    for shot in plan["shots"]:
        dur = shot_frames(shot, fps) / float(fps)
        out = os.path.join(audio_dir_tmp, "pad_%02d.wav" % int(shot["id"]))
        src = shot.get("audio_file")
        if src and os.path.exists(src):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                 "-af", "aresample=44100,aformat=channel_layouts=mono,adelay=300:all=1,apad=whole_dur=%.3f" % dur,
                 "-t", "%.3f" % dur, "-ac", "1", "-ar", "44100", out])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                 "-t", "%.3f" % dur, out])
        list_lines.append("file '%s'" % os.path.abspath(out))
    list_path = os.path.join(audio_dir_tmp, "list.txt")
    with open(list_path, "w") as fh:
        fh.write("\n".join(list_lines) + "\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c", "copy", out_wav])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_timed.json")
    ap.add_argument("--frames", default="out")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--out-fps", type=int, default=24)
    ap.add_argument("--out", default="out/movie_final.mp4")
    ap.add_argument("--music", default="")
    ap.add_argument("--music-volume", default="0.12")
    ap.add_argument("--font", default="Noto Sans Devanagari")
    ap.add_argument("--font-size", type=int, default=50)
    args = ap.parse_args()

    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    fps = args.fps
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    expected = sum(shot_frames(s, fps) for s in plan["shots"])
    found = len(glob.glob(os.path.join(args.frames, "frame_*.png")))
    print("Frames expected %d, found %d" % (expected, found), flush=True)
    if found < expected:
        raise SystemExit("Not enough rendered frames (%d of %d). Run the render step first." % (found, expected))

    ass_path = os.path.join(out_dir, "subs.ass")
    total = build_ass(plan, fps, args.font, args.font_size, ass_path)

    narration = os.path.join(out_dir, "narration.wav")
    build_narration(plan, fps, os.path.join(out_dir, "audio_tmp"), narration)

    fade_v = "fade=t=in:st=0:d=0.5,fade=t=out:st=%.2f:d=0.9" % max(0.0, total - 0.9)
    fade_a = "afade=t=in:st=0:d=0.3,afade=t=out:st=%.2f:d=1.0" % max(0.0, total - 1.0)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", os.path.join(args.frames, "frame_%05d.png"), "-i", narration]
    has_music = bool(args.music) and os.path.exists(args.music)
    if has_music:
        cmd += ["-stream_loop", "-1", "-i", args.music,
                "-filter_complex",
                "[2:a]volume=%s[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0[mix];[mix]%s[a]"
                % (args.music_volume, fade_a),
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a", "-af", fade_a]
    cmd += ["-vf", "subtitles=%s,%s,fps=%d,format=yuv420p" % (ass_path, fade_v, args.out_fps),
            "-c:v", "libx264", "-crf", "20", "-preset", "medium",
            "-c:a", "aac", "-b:a", "160k", "-t", "%.3f" % total, "-movflags", "+faststart", args.out]
    run(cmd)
    print("Done: %s (%.1f seconds, music: %s)" % (args.out, total, "yes" if has_music else "no"), flush=True)


if __name__ == "__main__":
    main()
