#!/usr/bin/env python3
"""Retiming pass for STORY BRAIN v2 plans.

Reads scene_plan_timed.json after TTS has assigned real per-shot durations.
Keeps the Story Brain schema (actors[].start/end/action) and normalizes the
fields consumed by the renderer and assembler.

Usage:
  python3 retime_v2.py --plan scene_plan_timed.json
  python3 retime_v2.py --plan scene_plan_timed.json --fps 12
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

# Approximate ground speeds, in metres/second, matching the animation cadence.
HUMAN_WALK = 0.80       # body heights per second
HUMAN_RUN = 2.30
ANIMAL_WALK = 0.75      # body lengths per second
ANIMAL_GALLOP = 2.40
MIN_TRAVEL = 3.0
MAX_TRAVEL = 28.0


def shot_duration(shot: dict[str, Any]) -> float:
    """Prefer the actual TTS duration; fall back to Story Brain's estimate."""
    # TTS v2 updates Story Brain's original seconds field; duration is the
    # assembler-compatible alias we write below.
    for key in ("seconds", "duration", "shot_seconds", "shot_sec"):
        value = shot.get(key)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return 3.0


def speed_of(actor: dict[str, Any]) -> float:
    action = str(actor.get("action", "")).lower()
    if actor.get("height") is not None:
        try:
            height = float(actor["height"])
        except (TypeError, ValueError):
            height = 1.8
        factor = HUMAN_RUN if action.startswith(("run", "roll")) else HUMAN_WALK
        return max(0.1, height * factor)
    try:
        size = float(actor.get("size", 2.0))
    except (TypeError, ValueError):
        size = 2.0
    factor = ANIMAL_GALLOP if action.startswith(("gallop", "run")) else ANIMAL_WALK
    return max(0.1, size * factor)


def xy(point: Any, fallback=(0.0, 0.0)) -> tuple[float, float]:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            return float(point[0]), float(point[1])
        except (TypeError, ValueError):
            pass
    return float(fallback[0]), float(fallback[1])


def is_moving(actor: dict[str, Any]) -> bool:
    start = actor.get("start")
    end = actor.get("end")
    if not isinstance(start, (list, tuple)) or not isinstance(end, (list, tuple)):
        return False
    sx, sy = xy(start)
    ex, ey = xy(end)
    return math.hypot(ex - sx, ey - sy) > 0.05


def retime_plan(plan: dict[str, Any], fps_override: int | None = None) -> tuple[int, int]:
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("The plan must contain a non-empty 'shots' array.")

    fps = int(fps_override or plan.get("fps") or 12)
    if fps < 1 or fps > 60:
        raise ValueError(f"Invalid FPS value: {fps}")
    plan["fps"] = fps
    plan.setdefault("aspect", "vertical")
    plan.setdefault("width", 720)
    plan.setdefault("height", 1280)

    changed = 0
    total_seconds = 0.0
    for index, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"Shot {index} is not a JSON object.")
        duration = shot_duration(shot)
        # Keep both names: the renderer can use seconds, while the assembler
        # consumes duration. Never overwrite a genuine TTS duration.
        shot["duration"] = round(duration, 4)
        shot["seconds"] = round(duration, 4)
        total_seconds += duration

        actors = shot.get("actors") or []
        if not isinstance(actors, list):
            raise ValueError(f"Shot {index}: 'actors' must be a list.")
        movers = [actor for actor in actors if isinstance(actor, dict) and is_moving(actor)]
        if not movers:
            continue

        average_speed = sum(speed_of(actor) for actor in movers) / len(movers)
        travel = max(MIN_TRAVEL, min(MAX_TRAVEL, average_speed * duration))
        base_x = [xy(actor.get("start"))[0] for actor in movers]
        lead_x = base_x[0]

        # Preserve the original group spacing and each actor's lane/depth.
        # The group travels at a shared distance so a chase/follow formation
        # does not spread apart because the models have different body sizes.
        for actor, original_x in zip(movers, base_x):
            start = actor.get("start", [0, 0])
            end = actor.get("end", start)
            _sx, sy = xy(start)
            _ex, ey = xy(end, (original_x, sy))
            offset = original_x - lead_x
            z_start = list(start[2:]) if len(start) > 2 else []
            z_end = list(end[2:]) if len(end) > 2 else z_start
            actor["start"] = [round(offset - travel / 2.0, 2), round(sy, 2)] + z_start
            actor["end"] = [round(offset + travel / 2.0, 2), round(ey if abs(ey - sy) > 0.05 else sy, 2)] + z_end

        shot["travel_m"] = round(travel, 2)
        changed += 1
        print(
            "shot %2d: %.2f s, average speed %.2f m/s -> group travel %.1f m"
            % (shot.get("id", index), duration, average_speed, travel),
            flush=True,
        )

    plan["total_seconds"] = round(total_seconds, 3)
    total_frames = round(total_seconds * fps)
    plan["total_frames"] = total_frames
    return changed, total_frames


def main() -> None:
    parser = argparse.ArgumentParser(description="Retiming pass for Story Brain v2 scene plans")
    parser.add_argument("--plan", default="scene_plan_timed.json")
    parser.add_argument("--out", default="", help="Optional output path; default overwrites --plan")
    parser.add_argument("--fps", type=int, default=0, help="Override plan FPS (default: plan value or 12)")
    args = parser.parse_args()

    if not os.path.isfile(args.plan):
        sys.exit(f"Plan file not found: {args.plan}")
    try:
        with open(args.plan, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
        if not isinstance(plan, dict):
            raise ValueError("Plan root must be a JSON object.")
        changed, total_frames = retime_plan(plan, args.fps or None)
        output = args.out or args.plan
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
        print(
            f"Retimed {changed} moving shot(s). Total: {plan['total_seconds']:.2f}s, "
            f"{total_frames} frames at {plan['fps']} FPS. Saved: {output}"
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        sys.exit(f"RETIME FAILED: {exc}")


if __name__ == "__main__":
    main()
