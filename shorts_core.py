"""shorts_core.py - shared data and geometry for the animal Shorts pipeline (v4).

No Blender needed here, so it can be tested anywhere. The director, the voice step and the
Blender renderer all use it, so they always agree about animals, actions and camera.

Coordinates: x = left/right, y = depth (away from the camera), z = up, all in metres.
Heading 0 = animal faces the camera (-y), +90 = faces right (+x), -90 = faces left.

v4: animals never hide each other from the camera (hidden_gap), speech timing helper
(speech_fraction), calmer camera drift on static shots.
"""
import math

HOOF = ["Cow", "Horse_White", "Horse", "Donkey", "Deer", "Stag", "Alpaca", "Bull"]
CANINE = ["Fox", "Wolf", "Husky", "ShibaInu"]
ANIMALS = HOOF + CANINE
ANIMS = {
    "hoof": ["Attack_Headbutt", "Attack_Kick", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2",
             "Idle_Headlow", "Idle_HitReact1", "Idle_HitReact2", "Jump_toIdle", "Walk"],
    "canine": ["Attack", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2", "Idle_2_HeadLow",
               "Idle_HitReact1", "Idle_HitReact2", "Jump_ToIdle", "Walk"],
}
TARGET_HEIGHT = {"Cow": 1.5, "Bull": 1.7, "Horse": 2.1, "Horse_White": 2.1, "Donkey": 1.5, "Deer": 1.5,
                 "Stag": 1.95, "Alpaca": 1.7, "Fox": 0.85, "Wolf": 1.05, "Husky": 0.95, "ShibaInu": 0.75}


def kind_of(model):
    return "canine" if model in CANINE else "hoof"


def body_len(model):
    """Nose to rump in metres (without the tail)."""
    return TARGET_HEIGHT.get(model, 1.5) * (1.5 if model in CANINE else 1.2)


def min_sep(model_a, model_b, extra=0.0):
    return 0.5 * (body_len(model_a) + body_len(model_b)) + 0.45 + extra


ACTIONS = {
    "idle":      {"hoof": "Idle", "canine": "Idle", "speed": 0.0},
    "listen":    {"hoof": "Idle", "canine": "Idle", "speed": 0.0},
    "talk":      {"hoof": "Idle", "canine": "Idle", "speed": 0.0},   # the renderer adds a speaking nod
    "walk":      {"hoof": "Walk", "canine": "Walk", "speed": 1.4},
    "run":       {"hoof": "Gallop", "canine": "Gallop", "speed": 5.5},
    "shock":     {"hoof": "Idle_HitReact1", "canine": "Idle_HitReact1", "speed": 0.0},
    "hurt":      {"hoof": "Idle_HitReact2", "canine": "Idle_HitReact2", "speed": 0.0},
    "fight":     {"hoof": "Attack_Headbutt", "canine": "Attack", "speed": 0.0},
    "kick":      {"hoof": "Attack_Kick", "canine": "Attack", "speed": 0.0},
    "eat":       {"hoof": "Eating", "canine": "Eating", "speed": 0.0},
    "sad":       {"hoof": "Idle_Headlow", "canine": "Idle_2_HeadLow", "speed": 0.0},
    "celebrate": {"hoof": "Gallop_Jump", "canine": "Gallop_Jump", "speed": 0.0},
}
FRAMINGS = ["extreme_close", "close", "medium", "wide"]
ANGLES = ["eye", "low", "high"]
MOVES = ["static", "push_in", "pull_out", "follow"]
SIDES = ["left", "right", "center"]

SMALL_DECOR = {"Grass_Common_Short", "Clover_1", "Clover_2", "Flower_3_Single", "Flower_4_Single",
               "Pebble_Round_1", "Pebble_Round_2", "Pebble_Round_3", "Petal_1", "Petal_2"}
LOOKS = {
    "meadow_day": dict(elev=48, az=35, sun=3.0, sun_col=(1.0, 0.96, 0.88), world=0.4, dust=0.6,
                       ground=((0.30, 0.54, 0.17), (0.20, 0.42, 0.11)), water=False, night=False,
                       trees=["CommonTree_1", "CommonTree_2", "CommonTree_3", "CommonTree_4", "CommonTree_5"], n_trees=10,
                       extras=["Bush_Common", "Bush_Common_Flowers", "Flower_3_Group", "Flower_4_Group", "Grass_Common_Tall",
                               "Clover_1", "Plant_1"], n_extras=22,
                       decor=["Grass_Common_Short", "Clover_1", "Clover_2", "Flower_3_Single", "Pebble_Round_1"], n_decor=26),
    "forest_day": dict(elev=40, az=20, sun=2.6, sun_col=(1.0, 0.96, 0.82), world=0.35, dust=0.6,
                       ground=((0.20, 0.38, 0.13), (0.13, 0.28, 0.09)), water=False, night=False,
                       trees=["Pine_1", "Pine_2", "Pine_3", "CommonTree_2", "CommonTree_3", "CommonTree_5"], n_trees=24,
                       extras=["Bush_Common", "Fern_1", "Mushroom_Common", "Grass_Common_Tall", "Plant_7"], n_extras=24,
                       decor=["Grass_Common_Short", "Clover_2", "Pebble_Round_2"], n_decor=20),
    "golden_sunset": dict(elev=7, az=55, sun=2.8, sun_col=(1.0, 0.60, 0.30), world=0.35, dust=3.0,
                          ground=((0.34, 0.45, 0.14), (0.24, 0.34, 0.10)), water=False, night=False,
                          trees=["CommonTree_1", "CommonTree_3", "CommonTree_4", "Pine_1"], n_trees=12,
                          extras=["Bush_Common", "Flower_4_Group", "Grass_Common_Tall", "Grass_Wispy_Tall"], n_extras=22,
                          decor=["Grass_Common_Short", "Flower_4_Single", "Pebble_Round_3"], n_decor=22),
    "riverbank_dawn": dict(elev=11, az=-40, sun=2.4, sun_col=(1.0, 0.80, 0.66), world=0.4, dust=2.0,
                           ground=((0.26, 0.46, 0.18), (0.18, 0.36, 0.12)), water=True, night=False,
                           trees=["CommonTree_2", "CommonTree_4", "Pine_2"], n_trees=9,
                           extras=["Bush_Common", "Grass_Common_Tall", "Fern_1", "Pebble_Square_1", "Pebble_Square_2"], n_extras=22,
                           decor=["Pebble_Round_1", "Pebble_Round_2", "Grass_Common_Short", "Clover_1"], n_decor=24),
    "storm_dusk": dict(elev=14, az=10, sun=0.8, sun_col=(0.72, 0.78, 1.0), world=0.25, dust=9.0,
                       ground=((0.16, 0.27, 0.14), (0.10, 0.19, 0.09)), water=False, night=False,
                       trees=["CommonTree_1", "DeadTree_1", "DeadTree_3", "CommonTree_3"], n_trees=10,
                       extras=["Bush_Common", "Grass_Wispy_Tall", "Mushroom_Common"], n_extras=14,
                       decor=["Grass_Common_Short", "Pebble_Round_2"], n_decor=14),
    "ember_sky": dict(elev=9, az=40, sun=2.6, sun_col=(1.0, 0.38, 0.14), world=0.3, dust=7.0,
                      ground=((0.20, 0.17, 0.10), (0.12, 0.09, 0.06)), water=False, night=False,
                      trees=["DeadTree_1", "DeadTree_2", "DeadTree_3", "DeadTree_4", "CommonTree_1"], n_trees=12,
                      extras=["Bush_Common", "Grass_Wispy_Tall", "Mushroom_Common"], n_extras=10,
                      decor=["Grass_Common_Short", "Pebble_Round_2", "Pebble_Square_1"], n_decor=14),
    "night_moon": dict(elev=25, az=-30, sun=1.9, sun_col=(0.55, 0.66, 1.0), world=0.9, dust=1.0,
                       ground=((0.10, 0.17, 0.12), (0.06, 0.11, 0.08)), water=False, night=True,
                       trees=["Pine_1", "Pine_3", "CommonTree_2", "DeadTree_2"], n_trees=14,
                       extras=["Bush_Common", "Mushroom_Laetiporus", "Grass_Common_Tall"], n_extras=12,
                       decor=["Grass_Common_Short", "Pebble_Round_1"], n_decor=12),
}

TURN_SECONDS = 0.45


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def ease(p):
    p = clamp(p, 0.0, 1.0)
    return p * p * (3 - 2 * p)


def lerp(a, b, p):
    return a + (b - a) * p


def angle_lerp(a, b, p):
    d = (b - a + 180.0) % 360.0 - 180.0
    return a + d * p


def heading_to(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) < 0.05:
        return None
    return math.degrees(math.atan2(dx, -dy))


def fovs(width, height, lens=30.0, sensor=36.0):
    if width >= height:
        h = 2 * math.atan(sensor / (2 * lens))
        v = 2 * math.atan(sensor * height / width / (2 * lens))
    else:
        v = 2 * math.atan(sensor / (2 * lens))
        h = 2 * math.atan(sensor * width / height / (2 * lens))
    return h, v


def az_of(side):
    return {"left": -20.0, "right": 20.0}.get(side, 0.0)


# ------------------------------------------------------------------ speech timing
def _eff(s):
    """Reading length that counts pauses: sentence ends and commas take extra time."""
    return len(s) + 10 * sum(s.count(c) for c in ".!?\u2026\u0964") + 4 * sum(s.count(c) for c in ",;:")


def speech_fraction(text, pos):
    """How far into the spoken narration (0..1) is character number pos, pauses included."""
    return clamp(_eff(text[:pos]) / max(1, _eff(text)), 0.0, 1.0)


# ------------------------------------------------------------------ choreography
def _seg(t0, t1, action, kind, p0, p1):
    return {"t0": t0, "t1": t1, "action": action, "anim": ACTIONS[action][kind],
            "p0": list(p0), "p1": list(p1), "moving": ACTIONS[action]["speed"] > 0 and p0 != p1, "heading": None}


def build_schedule(ch, dur):
    kind = kind_of(ch["model"])
    cur_p = [float(ch["pos"][0]), float(ch["pos"][1])]
    beats = [b for b in (ch.get("beats") or []) if isinstance(b, dict)][:3] or [{"action": "idle"}]
    segs, cur_t = [], 0.0
    for i, b in enumerate(beats):
        act = b.get("action") if b.get("action") in ACTIONS else "idle"
        at = b.get("at")
        start = cur_t
        if isinstance(at, (int, float)):
            start = max(cur_t, min(float(at), 0.95) * dur)
        if start > cur_t + 1e-6:
            segs.append(_seg(cur_t, start, "idle", kind, cur_p, cur_p))
            cur_t = start
        speed = ACTIONS[act]["speed"]
        to = b.get("to")
        if speed > 0 and isinstance(to, (list, tuple)) and len(to) >= 2:
            p1 = [float(to[0]), float(to[1])]
            d = math.hypot(p1[0] - cur_p[0], p1[1] - cur_p[1])
            if d >= 0.3:
                t1 = min(dur, cur_t + d / speed)
                frac = min(1.0, (t1 - cur_t) * speed / d)
                p_end = [cur_p[0] + (p1[0] - cur_p[0]) * frac, cur_p[1] + (p1[1] - cur_p[1]) * frac]
                segs.append(_seg(cur_t, t1, act, kind, cur_p, p_end))
                cur_t, cur_p = t1, p_end
                continue
        if speed > 0:
            act = "idle"
        if i + 1 < len(beats):
            nxt = beats[i + 1].get("at")
            t1 = min(float(nxt), 0.95) * dur if isinstance(nxt, (int, float)) else cur_t + (dur - cur_t) * 0.5
            t1 = min(dur, max(t1, cur_t + 0.3))
        else:
            t1 = dur
        segs.append(_seg(cur_t, t1, act, kind, cur_p, cur_p))
        cur_t = t1
    if cur_t < dur - 1e-6:
        segs.append(_seg(cur_t, dur, "idle", kind, cur_p, cur_p))
    return segs


def assign_headings(chars, focus_id):
    anchors = {c["id"]: c["segs"][-1]["p1"] for c in chars}
    for c in chars:
        prev = None
        for s in c["segs"]:
            h = None
            if s["moving"]:
                h = heading_to(s["p0"], s["p1"])
            else:
                tgt = None
                if c.get("face") in anchors and c["face"] != c["id"]:
                    tgt = anchors[c["face"]]
                elif focus_id in anchors and focus_id != c["id"]:
                    tgt = anchors[focus_id]
                else:
                    others = [a for k, a in anchors.items() if k != c["id"]]
                    if others:
                        tgt = min(others, key=lambda a: math.hypot(a[0] - s["p0"][0], a[1] - s["p0"][1]))
                if tgt is not None:
                    h = heading_to(s["p0"], tgt)
            if h is None:
                h = prev if prev is not None else 0.0
            s["heading"] = h
            prev = h
    return chars


def state_at(segs, t):
    idx = len(segs) - 1
    for i, s in enumerate(segs):
        if t < s["t1"]:
            idx = i
            break
    s = segs[idx]
    span = max(1e-6, s["t1"] - s["t0"])
    q = clamp((t - s["t0"]) / span, 0.0, 1.0) if s["moving"] else 0.0
    pos = (s["p0"][0] + (s["p1"][0] - s["p0"][0]) * q, s["p0"][1] + (s["p1"][1] - s["p0"][1]) * q)
    h = s["heading"] if s["heading"] is not None else 0.0
    if idx > 0 and segs[idx - 1]["heading"] is not None:
        k = ease((t - s["t0"]) / TURN_SECONDS)
        h = angle_lerp(segs[idx - 1]["heading"], h, k)
    return pos, h, s["anim"], s["action"]


# ------------------------------------------------------------------ camera direction
ANGLE_PITCH = {"eye": 3.0, "low": -6.0, "high": 24.0}


def camera_state(cam, chars, t, dur, hfov, vfov):
    focus = next((c for c in chars if c["id"] == cam.get("focus")), chars[0])
    framing = cam.get("framing", "medium")
    move = cam.get("move", "static")
    p = ease(t / max(dur, 1e-6))
    times = [t] if move == "follow" else [dur * 0.5, dur * 0.75, dur]

    def pos_at(c, tt):
        return state_at(c["segs"], tt)[0]

    if framing in ("close", "extreme_close"):
        subs = [focus]
    elif framing == "wide":
        subs = list(chars)
    else:
        mid = pos_at(focus, dur / 2.0)
        subs = [c for c in chars if c is focus or math.hypot(pos_at(c, dur / 2.0)[0] - mid[0],
                                                              pos_at(c, dur / 2.0)[1] - mid[1]) <= 3.5]
    pts = [pos_at(c, tt) for c in subs for tt in times]
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    if move == "follow":
        cx, cy = pos_at(focus, t)
    hmax = max(c["height"] for c in subs)
    hf = focus["height"]
    extent = (max(xs) - min(xs)) + 0.9 * hmax
    if framing == "extreme_close":
        cw, tz = 1.15 * hf, 0.72 * hf
    elif framing == "close":
        cw, tz = 1.75 * hf, 0.55 * hf
    elif framing == "medium":
        cw, tz = max(2.3 * hf, 1.5 * extent), 0.50 * hmax
    else:
        cw, tz = max(3.6 * hmax, 1.4 * extent), 0.42 * hmax + 0.15
    d = max(cw / (2 * math.tan(hfov / 2)), 1.25 * hmax / (2 * math.tan(vfov / 2)) if framing in ("medium", "wide") else 0)
    d = clamp(d, 4.2, 26.0)

    az = az_of(cam.get("side", "center"))
    pitch = ANGLE_PITCH.get(cam.get("angle", "eye"), 3.0)
    tx, ty = cx, cy
    if move == "push_in":
        d *= lerp(1.08, 0.94, p)
    elif move == "pull_out":
        d *= lerp(0.94, 1.08, p)
    elif move == "static":                      # never a dead frame: very slow drift
        d *= lerp(1.0, 0.985, p)
        az += lerp(-1.2, 1.2, p)
    a, pt = math.radians(az), math.radians(pitch)
    cam_pos = (tx + d * math.sin(a) * math.cos(pt), ty - d * math.cos(a) * math.cos(pt),
               max(0.3, tz + d * math.sin(pt)))
    return {"cam": cam_pos, "target": (tx, ty, tz), "dist": d}


# ------------------------------------------------------------------ keep animals apart AND visible
def _moving(ch):
    return any(ACTIONS.get(b.get("action"), {"speed": 0})["speed"] > 0 and b.get("to") for b in ch.get("beats", []))


def _attacks(ch):
    return any(b.get("action") in ("fight", "kick") for b in ch.get("beats", []))


def hidden_gap(ma, mb, pa, pb, az):
    """> 0 when, seen from the camera, the nearer animal covers a big part of the farther one."""
    a = math.radians(az)
    ca, sa = math.cos(a), math.sin(a)
    la, lb = pa[0] * ca + pa[1] * sa, pb[0] * ca + pb[1] * sa            # across the picture
    da, db = pa[1] * ca - pa[0] * sa, pb[1] * ca - pb[0] * sa            # distance from camera
    if abs(da - db) > 7.0:
        return 0.0
    ha, hb = TARGET_HEIGHT.get(ma, 1.5), TARGET_HEIGHT.get(mb, 1.5)
    near_h, far_h = (ha, hb) if da <= db else (hb, ha)
    if near_h < 0.6 * far_h:                                              # a small animal in front hides nobody
        return 0.0
    need = 0.6 * 0.5 * (body_len(ma) + body_len(mb)) * 0.85
    return max(0.0, need - abs(la - lb))


def _free_spot(chars, idx, sched, dur, az, step=0.4):
    me = chars[idx]
    times = [min(dur, k * step) for k in range(int(dur / step) + 2)]
    others = []
    for j, c in enumerate(chars):
        if j == idx:
            continue
        lunge = 0.8 if (_attacks(me) or _attacks(c)) else 0.0
        others.append((min_sep(me["model"], c["model"], lunge), c["model"], [(t, state_at(sched[j], t)[0]) for t in times]))
    best, best_d = None, None
    xs = [x / 10.0 for x in range(-26, 27, 4)]
    ys = [y / 10.0 for y in range(0, 76, 4)]
    for x in xs:
        for y in ys:
            ok = True
            for need, om, path in others:
                for t, p in path:
                    if abs(p[0]) > 4.0:
                        continue
                    if math.hypot(x - p[0], y - p[1]) < need or (t >= 0.4 * dur and hidden_gap(me["model"], om, (x, y), p, az) > 0.02):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                d = math.hypot(x - me["pos"][0], y - me["pos"][1])
                if best is None or d < best_d:
                    best, best_d = [x, y], d
    return best


def fix_collisions(chars, dur, step=0.2, tries=24, az=0.0):
    """Changes chars in place so that no two animals overlap and none hides another from the camera.
    Standing animals move to the nearest free spot, walkers get their path shifted. True when clear."""
    for _ in range(tries):
        sched = [build_schedule(c, dur) for c in chars]
        worst = None
        k = 0
        while True:
            t = min(dur, k * step)
            pos = [state_at(s, t)[0] for s in sched]
            for i in range(len(chars)):
                for j in range(i + 1, len(chars)):
                    if abs(pos[i][0]) > 4.0 or abs(pos[j][0]) > 4.0:
                        continue
                    d = math.hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1])
                    lunge = 0.8 if (_attacks(chars[i]) or _attacks(chars[j])) else 0.0
                    g_sep = min_sep(chars[i]["model"], chars[j]["model"], lunge) - d
                    g_hid = hidden_gap(chars[i]["model"], chars[j]["model"], pos[i], pos[j], az) if t >= 0.4 * dur else 0.0
                    gap, kind = (g_sep, "sep") if g_sep >= g_hid else (g_hid, "hid")
                    if gap > 0.05 and (worst is None or gap > worst[0]):
                        worst = (gap, i, j, t, pos[i], pos[j], kind)
            if t >= dur:
                break
            k += 1
        if worst is None:
            return True
        gap, i, j, t, pi, pj, kind = worst
        order = [j, i] if (_moving(chars[j]) or not _moving(chars[i])) else [i, j]
        done = False
        for mover in order:
            c = chars[mover]
            if not _moving(c):
                spot = _free_spot(chars, mover, sched, dur, az)
                if spot is not None:
                    c["pos"] = [round(spot[0], 2), round(spot[1], 2)]
                    done = True
                    break
        if done:
            continue
        mover = order[0]
        pm, po = (pj, pi) if mover == j else (pi, pj)
        shift = gap + 0.35
        c = chars[mover]
        sx = 1.0 if pm[0] >= po[0] else -1.0
        if kind == "hid":
            dx, dy = clamp(pm[0] + sx * shift, -2.6, 2.6) - pm[0], 0.0
            if abs(dx) < 0.2:
                dx, dy = 0.0, clamp(pm[1] + (1.0 if pm[1] >= po[1] else -1.0) * shift, 0.0, 7.5) - pm[1]
        else:
            sign = 1.0 if pm[1] >= po[1] else -1.0
            ny = clamp(pm[1] + sign * shift, 0.0, 7.5)
            dx, dy = (0.0, ny - pm[1]) if abs(ny - pm[1]) >= 0.2 else (clamp(pm[0] + sx * shift, -2.6, 2.6) - pm[0], 0.0)
        for b in c.get("beats", []):
            if b.get("to"):
                b["to"] = [round(clamp(b["to"][0] + dx, -2.6, 2.6), 2), round(clamp(b["to"][1] + dy, 0.0, 7.5), 2)]
        if abs(c["pos"][0]) <= 4.0:
            c["pos"] = [round(clamp(c["pos"][0] + dx, -7.0, 7.0), 2), round(clamp(c["pos"][1] + dy, 0.0, 7.5), 2)]
    return False


def shot_setup(shot, dur):
    import copy
    spec = copy.deepcopy(shot["characters"])
    az = az_of((shot.get("camera") or {}).get("side"))
    fix_collisions(spec, dur, az=az)
    chars = []
    for c in spec:
        chars.append({"id": c["id"], "model": c["model"], "face": c.get("face"), "tint": c.get("tint"),
                      "z": c.get("z", 0.0), "height": TARGET_HEIGHT.get(c["model"], 1.5),
                      "segs": build_schedule(c, dur)})
    assign_headings(chars, (shot.get("camera") or {}).get("focus"))
    return chars
