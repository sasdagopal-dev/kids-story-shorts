#!/usr/bin/env python3
"""preview_timeline.py - quick timeline WITHOUT voice, for the fast picture check.

  python3 preview_timeline.py --plan plans/episode_x.json --quality fast
Writes scene_plan_timed.json, timeline.json and preview_table.md (story vs direction, shot by shot).
Shot lengths follow the word counts, scaled to the target length.
"""
import argparse, json, re

QUALITY = {"fast": (540, 960, 8), "balanced": (720, 1280, 16), "high": (720, 1280, 32), "ultra": (1080, 1920, 28)}

ap = argparse.ArgumentParser()
ap.add_argument("--plan", required=True)
ap.add_argument("--quality", default="fast")
ap.add_argument("--fps", type=int, default=12)
a = ap.parse_args()
plan = json.load(open(a.plan, encoding="utf-8"))
target = float(plan.get("target_seconds", 150))
shots = plan["shots"]
weights = [max(3, len(re.findall(r"\S+", s["narration"]))) for s in shots]
total_w = float(sum(weights))
w, h, samples = QUALITY[a.quality]
total_frames = int(round(target * a.fps))
cum, tl_shots = 0.0, []
for i, (s, wt) in enumerate(zip(shots, weights), start=1):
    start_f = int(round(cum * a.fps))
    cum += target * wt / total_w
    end_f = total_frames if i == len(shots) else int(round(cum * a.fps))
    dur = (end_f - start_f) / a.fps
    s.update({"duration": round(dur, 3), "start_frame": start_f, "end_frame": end_f, "start_time": round(start_f / a.fps, 3)})
    tl_shots.append({"id": s["id"], "duration": round(dur, 3), "start_frame": start_f, "end_frame": end_f,
                     "start_time": round(start_f / a.fps, 3), "lead": 0.25, "voice_seconds": round(dur * 0.8, 3),
                     "audio": None, "thumb": bool(s.get("thumb"))})
plan.update({"fps": a.fps, "total_frames": total_frames, "total_seconds": target})
json.dump(plan, open("scene_plan_timed.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump({"fps": a.fps, "width": w, "height": h, "samples": samples, "aspect": "vertical", "total_frames": total_frames,
           "total_seconds": target, "tempo": 1.0, "lead": 0.25, "shots": tl_shots}, open("timeline.json", "w"), indent=1)
with open("preview_table.md", "w", encoding="utf-8") as f:
    f.write("### Story and direction, shot by shot\n\nCompare each row with its 3 pictures (shot_NN_a / b / c) in the artifact.\n\n"
            "| Shot | Look | Camera | Who does what | Narration |\n|---|---|---|---|---|\n")
    for s in shots:
        cam = s["camera"]
        who = "; ".join(f"{c['id']} ({c['model']}) at {c['pos']}: " + " > ".join(
            b["action"] + (f" to {b['to']}" if 'to' in b else "") for b in c["beats"]) for c in s["characters"])
        if s.get("speakers"):
            who += " | SPEAKS: " + ", ".join(f"{x['id']}@{x['at']}" for x in s["speakers"])
        f.write(f"| {s['id']} | {s['look']} | {cam['framing']} / {cam['angle']} / {cam['move']} on {cam['focus']} ({cam['side']}) | "
                f"{who} | {s['narration'].replace('|', '/')} |\n")
print("Preview timeline ready:", len(shots), "shots,", total_frames, "frames")
