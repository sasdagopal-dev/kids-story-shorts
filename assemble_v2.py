#!/usr/bin/env python3
"""Assemble shard video + per-shot narration + optional music/subtitles.

This is the single canonical assembler for Story Brain v2.
It accepts the scene_plan_timed.json produced by tts.py and retime_v2.py.

Usage:
  python3 assemble_v2.py --plan scene_plan_timed.json \
      --video out/video_all.mp4 --fps 12 --out out/movie_final.mp4 --music ""

The plan should contain shots with duration (or seconds), optional audio and lead.
If subs.srt is absent, a basic SRT is generated from the shot narration.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def run(cmd: list[Any], cwd: str | None = None) -> None:
    command = [str(item) for item in cmd]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, cwd=cwd)


def get_shots(plan: Any) -> list[dict[str, Any]]:
    if isinstance(plan, list):
        return plan
    if isinstance(plan, dict):
        for key in ("shots", "scenes"):
            if isinstance(plan.get(key), list):
                return plan[key]
    raise ValueError("Plan has no 'shots' list")


def shot_duration(shot: dict[str, Any]) -> float:
    for key in ("duration", "seconds", "shot_seconds", "shot_sec"):
        try:
            value = float(shot.get(key))
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 3.0


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def resolve_audio(shot: dict[str, Any], root: Path) -> Path | None:
    candidates: list[Path] = []
    declared = shot.get("audio")
    if isinstance(declared, str) and declared.strip():
        p = Path(declared)
        candidates.append(p if p.is_absolute() else root / p)
    shot_id = int(shot.get("id", 0))
    for ext in ("mp3", "wav", "m4a", "aac", "ogg"):
        candidates.append(root / "audio" / f"shot_{shot_id:02d}.{ext}")
        candidates.append(root / "audio" / f"shot_{shot_id:03d}.{ext}")
        candidates.append(root / "audio" / f"shot_{shot_id}.{ext}")
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    return None


def write_generated_srt(shots: list[dict[str, Any]], path: Path) -> bool:
    cursor = 0.0
    entries: list[str] = []
    number = 1
    for shot in shots:
        duration = shot_duration(shot)
        narration = str(shot.get("narration") or "").strip()
        lead = max(0.0, min(duration - 0.1, float(shot.get("lead", 0.25) or 0.0)))
        if narration:
            start = cursor + lead
            end = max(start + 0.4, cursor + duration - 0.05)
            entries.extend([
                str(number),
                f"{srt_timestamp(start)} --> {srt_timestamp(end)}",
                narration.replace("\n", " "),
                "",
            ])
            number += 1
        cursor += duration
    if not entries:
        return False
    path.write_text("\n".join(entries), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble the Story Brain v2 episode")
    parser.add_argument("--plan", required=True, help="scene_plan_timed.json")
    parser.add_argument("--video", required=True, help="joined silent video")
    parser.add_argument("--out", required=True, help="final MP4 output")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--music", default="")
    parser.add_argument("--srt", default="subs.srt")
    parser.add_argument("--burn-subs", choices=("true", "false"), default="true")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    args = parser.parse_args()

    root = Path.cwd()
    plan_path = Path(args.plan).resolve()
    video_path = Path(args.video).resolve()
    if not plan_path.is_file():
        sys.exit(f"Plan not found: {plan_path}")
    if not video_path.is_file():
        sys.exit(f"Video not found: {video_path}")
    if args.fps < 1 or args.fps > 60:
        sys.exit("FPS must be between 1 and 60.")

    with plan_path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    shots = get_shots(plan)
    if not shots:
        sys.exit("Plan contains no shots.")

    total_seconds = sum(shot_duration(shot) for shot in shots)
    aspect = str(plan.get("aspect", "vertical")).lower() if isinstance(plan, dict) else "vertical"
    if aspect != "vertical":
        width, height = 1920, 1080
    else:
        width, height = args.width, args.height

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "_assemble_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # Make a narration timeline with one exact-duration WAV per shot.
    concat_lines: list[str] = []
    missing_audio: list[int] = []
    for index, shot in enumerate(shots, start=1):
        shot_id = int(shot.get("id", index))
        duration = shot_duration(shot)
        lead_ms = max(0, int(float(shot.get("lead", 0.25) or 0.0) * 1000))
        wav_path = (work / f"shot_{shot_id:03d}.wav").resolve()
        audio_path = resolve_audio(shot, root)
        if audio_path:
            run([
                "ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                "-af", f"adelay={lead_ms}|{lead_ms},apad=whole_dur={duration:.4f}",
                "-t", f"{duration:.4f}", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav_path,
            ])
        else:
            missing_audio.append(shot_id)
            run([
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{duration:.4f}",
                "-c:a", "pcm_s16le", wav_path,
            ])
        escaped = str(wav_path).replace("'", "'\\''")
        concat_lines.append(f"file '{escaped}'")

    list_path = work / "audio_list.txt"
    list_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    narration_path = work / "narration.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", list_path, "-c:a", "pcm_s16le", narration_path])

    # Use supplied subtitles when available; otherwise create them from narration.
    srt_input = Path(args.srt)
    if not srt_input.is_absolute():
        srt_input = root / srt_input
    srt_path = work / "subs.srt"
    if srt_input.is_file() and srt_input.stat().st_size > 0:
        shutil.copy2(srt_input, srt_path)
    else:
        generated = write_generated_srt(shots, srt_path)
        if generated:
            print("No supplied subs.srt found; generated subtitles from the shot narration.")

    have_srt = srt_path.is_file() and srt_path.stat().st_size > 0
    ass_out = out_path.parent / "subs.ass"
    if have_srt:
        # ASS is also saved as a sidecar artifact for later editing.
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(srt_path), str(ass_out)], check=False)

    # Encode final MP4 at the requested FPS. The render is 720x1280 by default;
    # scale to 1080x1920 for a clean vertical delivery file.
    vf = f"scale={width}:{height}:flags=lanczos,fps={args.fps}"
    if have_srt and args.burn_subs == "true":
        language = str(plan.get("lang", "en")).lower() if isinstance(plan, dict) else "en"
        font = "Noto Sans Devanagari" if language == "hi" else "Noto Sans"
        style = (
            f"FontName={font},FontSize=48,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00101010,BorderStyle=1,Outline=3,Shadow=1,"
            "Alignment=2,MarginV=58"
        )
        vf += f",subtitles=subs.srt:force_style='{style}'"

    inputs = ["-i", str(video_path), "-i", str(narration_path)]
    music_path = Path(args.music).resolve() if args.music else None
    if music_path and music_path.is_file():
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]
        audio_map = [
            "-filter_complex",
            "[1:a]volume=1.0[narr];[2:a]volume=0.10[music];"
            "[narr][music]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
            "-map", "0:v:0", "-map", "[aout]",
        ]
    else:
        audio_map = ["-map", "0:v:0", "-map", "1:a:0"]

    command = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs, *audio_map,
        "-vf", vf, "-r", str(args.fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{total_seconds:.3f}",
        "-movflags", "+faststart", str(out_path),
    ]
    run(command, cwd=str(work))

    shutil.rmtree(work, ignore_errors=True)
    print(f"DONE: {out_path}")
    print(f"Duration target: {total_seconds:.2f}s | FPS: {args.fps} | Aspect: {aspect}")
    if missing_audio:
        print("WARNING: silent fallback used for shot(s): " + ", ".join(map(str, missing_audio)))
    if have_srt:
        print(f"Subtitles: {ass_out if ass_out.exists() else srt_path}")


if __name__ == "__main__":
    main()
