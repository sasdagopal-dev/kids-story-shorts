#!/usr/bin/env python3
"""
retime_v2.py  -  makes walking and running match the ground speed.

Runs AFTER the voice is made (when every shot has its real length):

  python3 retime_v2.py --plan scene_plan_timed.json

Why: a walking character must cover the ground at the speed of its legs, otherwise it looks like
it is sliding or moving in slow motion. The distance is now worked out from the real shot length.

If you see feet sliding, change the 4 numbers below:
  - character seems to GLIDE FORWARD faster than its legs move  -> make the number SMALLER
  - character seems to WALK ON THE SPOT / move too slowly        -> make the number BIGGER
"""
import argparse
import json

HUMAN_WALK = 0.8      # body heights per second
HUMAN_RUN = 2.3
ANIMAL_WALK = 0.75    # body lengths per second
ANIMAL_GALLOP = 2.4
MIN_TRAVEL = 3.0      # metres
MAX_TRAVEL = 28.0


def speed_of(a):
    act = str(a.get("action", "")).lower()
    if a.get("height"):
        return float(a["height"]) * (HUMAN_RUN if act.startswith(("run", "roll")) else HUMAN_WALK)
    return float(a.get("size", 2.0)) * (ANIMAL_GALLOP if act.startswith("gallop") else ANIMAL_WALK)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_timed.json")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)

    changed = 0
    for shot in plan["shots"]:
        movers = [a for a in shot["actors"] if list(a["start"][:2]) != list(a["end"][:2])]
        if not movers:
            continue
        secs = float(shot["seconds"])
        speed = sum(speed_of(a) for a in movers) / len(movers)
        dx = max(MIN_TRAVEL, min(MAX_TRAVEL, speed * secs))
        base = [float(a["start"][0]) for a in movers]
        lead_x = base[0]
        for a, x0 in zip(movers, base):
            off = x0 - lead_x
            z = a["start"][2:] if len(a["start"]) > 2 else []
            a["start"] = [round(off - dx / 2.0, 2), a["start"][1]] + list(z)
            a["end"] = [round(off + dx / 2.0, 2), a["end"][1]] + list(z)
        shot["travel_m"] = round(dx, 1)
        changed += 1
        print("shot %2d: %.1f s, speed %.2f m/s -> travels %.1f m" % (shot["id"], secs, speed, dx))
    with open(args.out or args.plan, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)
    print("Retimed %d moving shot(s)." % changed)


if __name__ == "__main__":
    main()
