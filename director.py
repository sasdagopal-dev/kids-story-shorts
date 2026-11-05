#!/usr/bin/env python3
"""director.py v2 - Gemini as the DIRECTOR of a real STORY, with checked choreography.

  python3 director.py                       # next topic from director_config.json
  python3 director.py --topic "..."         # your own topic
  python3 director.py --validate-only plan.json

What is new in v2
* the prompt asks for a real story (hero, wish, obstacle, mistake, consequence, smart fix)
  told by events, not a lecture. Each shot = one visible event, narration describes what is seen.
* Gemini chooses an ACTION per character (talk, listen, walk, run, shock, fight, eat, sad ...).
  This script turns it into the exact animation name of THAT model (humans, hoofed animals and
  dogs/wolves/foxes have different animation names in your packs).
* choreography is fixed by code, not left to Gemini: walkers/runners always get a destination,
  characters face each other or the speaker, characters are spread apart, and the camera
  looks at the action (target and distance follow the characters).
Edit the ASSET lists if you add packs. Everything is checked against them.
"""
import argparse, datetime, json, math, os, re, sys, time, urllib.error, urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"

# ---- characters and the REAL animation names from your asset check
HUMANS = ["Farmer", "King", "Worker", "Suit", "Adventurer", "Casual_2", "Casual_Hoodie", "Beach", "Punk"]
HOOF = ["Cow", "Horse_White", "Horse", "Donkey", "Deer", "Stag", "Alpaca", "Bull"]
CANINE = ["Fox", "Wolf", "Husky", "ShibaInu"]
ANIMALS = HOOF + CANINE
ANIMS = {
    "human": ["Death", "Gun_Shoot", "HitRecieve", "HitRecieve_2", "Idle", "Idle_Gun", "Idle_Gun_Pointing",
              "Idle_Gun_Shoot", "Idle_Neutral", "Idle_Sword", "Interact", "Kick_Left", "Kick_Right",
              "Punch_Left", "Punch_Right", "Roll", "Run", "Run_Back", "Run_Left", "Run_Right", "Run_Shoot",
              "Sword_Slash", "Walk", "Wave"],
    "hoof": ["Attack_Headbutt", "Attack_Kick", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2",
             "Idle_Headlow", "Idle_HitReact1", "Idle_HitReact2", "Jump_toIdle", "Walk"],
    "canine": ["Attack", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2", "Idle_2_HeadLow",
               "Idle_HitReact1", "Idle_HitReact2", "Jump_ToIdle", "Walk"],
}
# action word -> animation per kind
ACTIONS = {
    "idle":      {"human": "Idle_Neutral", "hoof": "Idle", "canine": "Idle"},
    "talk":      {"human": "Interact", "hoof": "Idle_2", "canine": "Idle_2"},
    "listen":    {"human": "Idle_Neutral", "hoof": "Idle", "canine": "Idle"},
    "walk":      {"human": "Walk", "hoof": "Walk", "canine": "Walk"},
    "run":       {"human": "Run", "hoof": "Gallop", "canine": "Gallop"},
    "wave":      {"human": "Wave", "hoof": "Idle_2", "canine": "Idle_2"},
    "shock":     {"human": "HitRecieve_2", "hoof": "Idle_HitReact1", "canine": "Idle_HitReact1"},
    "hurt":      {"human": "HitRecieve", "hoof": "Idle_HitReact2", "canine": "Idle_HitReact2"},
    "fight":     {"human": "Punch_Right", "hoof": "Attack_Headbutt", "canine": "Attack"},
    "kick":      {"human": "Kick_Right", "hoof": "Attack_Kick", "canine": "Attack"},
    "eat":       {"human": "Interact", "hoof": "Eating", "canine": "Eating"},
    "sad":       {"human": "Idle_Neutral", "hoof": "Idle_Headlow", "canine": "Idle_2_HeadLow"},
    "celebrate": {"human": "Wave", "hoof": "Idle_2", "canine": "Idle_2"},
    "jump":      {"human": "Roll", "hoof": "Gallop_Jump", "canine": "Gallop_Jump"},
    "faint":     {"human": "Death", "hoof": "Death", "canine": "Death"},
    "sword":     {"human": "Idle_Sword", "hoof": "Idle", "canine": "Idle"},
}
MOVING_ACTIONS = {"walk", "run"}
MOVING_ANIMS = {"Walk", "Run", "Gallop"}
ENVS = ["village", "forest", "storm"]
DEFAULT_PROPS = ["CommonTree_1", "CommonTree_2", "CommonTree_3", "Pine_1", "Bush_Common"]

SLEEP = time.sleep


def kind_of(model):
    return "human" if model in HUMANS else ("canine" if model in CANINE else "hoof")


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class Reject(Exception):
    """The answer is not good enough. The reason goes back to Gemini."""


# ------------------------------------------------------------------ Gemini
def gcall(model, body, key):
    req = urllib.request.Request(
        f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        wait = None
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
        if m:
            wait = float(m.group(1))
        raise GErr(f"HTTP {e.code}: {text[:300]}", e.code,
                   e.code in (408, 429, 500, 502, 503, 504), wait, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GErr(f"network: {e}", None, True, None, "")


def pick_models(key, override):
    if override:
        return [m.strip() for m in override.split(",") if m.strip()]
    found = []
    try:
        req = urllib.request.Request(f"{BASE}/models?pageSize=200", headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        for m in data.get("models", []):
            name = m["name"].split("/")[-1]
            mt = re.match(r"^gemini-(\d+(?:\.\d+)?)-(flash|flash-lite)$", name)
            if mt and "generateContent" in m.get("supportedGenerationMethods", []):
                found.append((-float(mt.group(1)), 0 if mt.group(2) == "flash" else 1, name))
    except Exception as e:
        print(f"could not list models: {e}", file=sys.stderr)
    found.sort()
    names = [n for _, _, n in found][:4]
    return names or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def ask_json(prompt, models, key, state):
    last = None
    for model in models:
        if model in state["dead"]:
            continue
        for attempt in range(1, 4):
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}}
            try:
                out = gcall(model, body, key)
                cands = out.get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                text = "".join(p.get("text", "") for p in parts).strip()
                if not text:
                    raise GErr("empty answer", 200, True)
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
                state["model"] = model
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise Reject(f"your answer was not valid JSON ({e})")
            except GErr as e:
                last = e
                t = (e.text or str(e)).lower()
                if "api key not valid" in t or e.code in (401, 403):
                    sys.exit(f"FAIL: Gemini rejected the key or access: {str(e)[:200]}")
                if e.code == 404 or (e.code == 429 and ("perday" in t or "per day" in t)):
                    state["dead"].add(model)
                    print(f"  {model}: not usable now, trying the next model", file=sys.stderr)
                    break
                if not e.retryable and e.code != 400:
                    state["dead"].add(model)
                    break
                wait = min(90, (e.wait or 0) + 3) if e.code == 429 else 8 * attempt
                print(f"  {model}: attempt {attempt}/3 failed ({str(e)[:100]}), waiting {wait:.0f}s", file=sys.stderr)
                if attempt < 3:
                    SLEEP(wait)
    raise RuntimeError(f"no Gemini model answered: {last}")


# ------------------------------------------------------------------ small helpers
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def num(x, default):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pair(v, default, xr=(-6.5, 6.5), yr=(-1.5, 6.5)):
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return [round(clamp(num(v[0], default[0]), *xr), 2), round(clamp(num(v[1], default[1]), *yr), 2)]
    return list(default)


def hindi_ratio(s):
    letters = [c for c in s if c.isalpha()]
    return (sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters)) if letters else 0.0


# ------------------------------------------------------------------ choreography (done by code)
def resolve_animation(model, action, anim, notes, n):
    kind = kind_of(model)
    action = (action or "").strip().lower() if isinstance(action, str) else ""
    if action in ACTIONS:
        return action, ACTIONS[action][kind]
    if isinstance(anim, str) and anim in ANIMS[kind]:
        return ("walk" if anim == "Walk" else "run" if anim in ("Run", "Gallop") else "raw"), anim
    if action or anim:
        notes.append(f"shot {n}: {model} action/animation '{action or anim}' unknown, used idle")
    return "idle", ACTIONS["idle"][kind]


def build_character(c, notes, n):
    if not isinstance(c, dict) or c.get("model") not in HUMANS + ANIMALS:
        if isinstance(c, dict):
            notes.append(f"shot {n}: unknown character '{c.get('model')}' removed")
        return None
    action, anim = resolve_animation(c["model"], c.get("action"), c.get("anim"), notes, n)
    ch = {"model": c["model"], "action": action, "anim": anim, "pos": pair(c.get("pos"), [0, 0])}
    if isinstance(c.get("move"), (list, tuple)):
        ch["move"] = pair(c["move"], ch["pos"], xr=(-8, 8))
    return ch


def choreograph(chars, focus_model, n, notes):
    """Spacing, destinations for walkers, facing, in that order."""
    movers = [c for c in chars if c["anim"] in MOVING_ANIMS]
    still = [c for c in chars if c["anim"] not in MOVING_ANIMS]

    # 1. spread standing characters apart (at least 1.6 units on the x axis)
    still.sort(key=lambda c: c["pos"][0])
    for i in range(1, len(still)):
        if still[i]["pos"][0] - still[i - 1]["pos"][0] < 1.6 and abs(still[i]["pos"][1] - still[i - 1]["pos"][1]) < 1.2:
            still[i]["pos"][0] = round(still[i - 1]["pos"][0] + 1.6, 2)
    if still and still[-1]["pos"][0] > 6.0:
        shift = still[-1]["pos"][0] - 6.0
        for c in still:
            c["pos"][0] = round(c["pos"][0] - shift, 2)

    # 2. every walker or runner needs a real destination
    focus = next((c for c in still if c["model"] == focus_model), still[0] if still else None)
    for c in movers:
        mv = c.get("move")
        dist = math.hypot(mv[0] - c["pos"][0], mv[1] - c["pos"][1]) if mv else 0
        if not mv or dist < 1.5:
            if focus is not None:
                side = -1.8 if c["pos"][0] < focus["pos"][0] else 1.8
                c["move"] = [round(clamp(focus["pos"][0] + side, -6.5, 6.5), 2), focus["pos"][1]]
            else:
                c["move"] = [round(clamp(c["pos"][0] + (4.0 if c["pos"][0] < 0 else -4.0), -8, 8), 2), c["pos"][1]]
            if mv:
                notes.append(f"shot {n}: {c['model']} walk was too short, destination fixed")
            else:
                notes.append(f"shot {n}: {c['model']} had no destination, added one")
        c.pop("heading", None)          # walkers face the way they walk

    # 3. face each other / the speaker
    def end_point(c):
        return c["move"] if c["anim"] in MOVING_ANIMS and "move" in c else c["pos"]
    spk = next((c for c in chars if c["model"] == focus_model), chars[0])
    for c in chars:
        if c["anim"] in MOVING_ANIMS:
            continue
        others = [o for o in chars if o is not c]
        if not others:
            c["heading"] = 25
            continue
        if c is spk:
            tgt = min(others, key=lambda o: abs(end_point(o)[0] - c["pos"][0]))
        else:
            tgt = spk
        dx = end_point(tgt)[0] - c["pos"][0]
        if abs(dx) < 0.6:
            c["heading"] = 0
        else:
            c["heading"] = int(math.copysign(clamp(50 + 15 * min(abs(dx), 2), 50, 80), dx))


def build_camera(cam, chars, focus_model):
    cam = cam if isinstance(cam, dict) else {}
    pts, w = [], []
    for c in chars:
        a = c["pos"]
        b = c.get("move") if c["anim"] in MOVING_ANIMS else None
        p = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] if b else a
        pts.append(p)
        w.append(2.0 if c["model"] == focus_model else 1.0)
    sw = sum(w)
    tx = sum(p[0] * x for p, x in zip(pts, w)) / sw
    ty = sum(p[1] * x for p, x in zip(pts, w)) / sw
    xs = [c["pos"][0] for c in chars] + [c["move"][0] for c in chars if c.get("move") and c["anim"] in MOVING_ANIMS]
    spread = max(xs) - min(xs)
    z = 1.2 if any(c["model"] in HUMANS for c in chars) else 1.0
    dist = 6.0 if len(chars) == 1 else clamp(5.5 + 0.75 * spread, 5.5, 10.5)
    out = {"target": [round(clamp(tx, -4.5, 4.5), 2), round(clamp(ty, -1, 5), 2), z], "dist": round(dist, 1)}
    de = num(cam.get("dist_end"), None) if "dist_end" in cam else None
    if de is not None:
        out["dist_end"] = round(clamp(de, 4.0, 11), 1)
    elif any(c["model"] == focus_model and c["action"] == "talk" for c in chars):
        out["dist_end"] = round(max(4.5, dist - 1.3), 1)
    if "sweep" in cam:
        out["sweep"] = round(clamp(num(cam["sweep"], 0), -35, 35))
    if "angle" in cam:
        out["angle"] = round(clamp(num(cam["angle"], 0), -20, 15))
    if cam.get("thumb"):
        out["thumb"] = True
    return out


def fix_prop(p, allowed, notes, n):
    if not isinstance(p, dict) or p.get("model") not in allowed:
        notes.append(f"shot {n}: prop '{p.get('model') if isinstance(p, dict) else p}' not allowed, removed")
        return None
    out = {"model": p["model"], "pos": pair(p.get("pos"), [0, 2.5], yr=(0, 7))}
    if "scale" in p:
        out["scale"] = round(clamp(num(p["scale"], 1.0), 0.5, 2.5), 2)
    g = p.get("grow")
    if isinstance(g, (list, tuple)) and len(g) >= 2 and p["model"].startswith("CommonTree"):
        out["grow"] = [round(clamp(num(g[0], 0.15), 0.05, 1.0), 2), round(clamp(num(g[1], 1.4), 0.5, 2.5), 2)]
    return out


def repair_plan(plan, cfg, strict_length=True):
    notes = []
    if not isinstance(plan, dict) or not isinstance(plan.get("shots"), list):
        raise Reject("the JSON must be an object with a 'shots' list")
    want = int(cfg.get("shots", 12))
    shots_in = plan["shots"]
    if not (want - 2 <= len(shots_in) <= want + 2):
        raise Reject(f"you wrote {len(shots_in)} shots, I need exactly {want}")
    allowed = cfg.get("props_allowed") or DEFAULT_PROPS
    shots, human_shots = [], 0
    for n, s in enumerate(shots_in, start=1):
        if not isinstance(s, dict):
            raise Reject(f"shot {n} is not an object")
        text = (s.get("narration") or "").strip()
        if not text:
            raise Reject(f"shot {n} has no narration")
        if hindi_ratio(text) < 0.7:
            raise Reject(f"shot {n} narration is not Hindi in Devanagari script")
        chars = [build_character(c, notes, n) for c in (s.get("characters") or [])[:6]]
        chars = [c for c in chars if c][:4]
        if not chars:
            raise Reject(f"shot {n} has no valid characters (use only the listed models)")
        models = [c["model"] for c in chars]
        focus = s.get("focus") if s.get("focus") in models else next((m for m in models if m in HUMANS), models[0])
        if any(m in HUMANS for m in models):
            human_shots += 1
        choreograph(chars, focus, n, notes)
        props = [fix_prop(p, allowed, notes, n) for p in (s.get("props") or [])[:4]]
        props = [p for p in props if p][:3]
        env = s.get("env") if s.get("env") in ENVS else "village"
        title = s.get("title") if isinstance(s.get("title"), str) else ""
        if not re.fullmatch(r"[A-Za-z0-9 _\-]{2,40}", title or ""):
            title = f"Shot {n}"
        shot = {"id": n, "title": title, "narration": text, "env": env,
                "visual": (s.get("visual") or "")[:200], "focus": focus,
                "characters": [{k: v for k, v in c.items() if k != "action"} for c in chars]}
        if props:
            shot["props"] = props
        shot["camera"] = build_camera(s.get("camera"), chars, focus)
        shots.append(shot)

    if human_shots < int(cfg.get("min_human_shots", 0)):
        raise Reject(f"only {human_shots} shots show a human character, at least {cfg['min_human_shots']} must "
                     f"(use Farmer, King, Worker, Suit and the other human models in the story)")
    last = shots[-1]["narration"]
    if "शिक्षा" not in last or "सलाह" not in last:
        shots[-1]["narration"] = last.rstrip() + " " + cfg["disclaimer"]
        notes.append("disclaimer sentence added to the last shot")
    total = sum(len(s["narration"]) for s in shots)
    lo, hi = int(cfg["min_chars"]), int(cfg["max_chars"])
    if strict_length and not (lo <= total <= hi):
        raise Reject(f"total narration is {total} characters, it must be between {lo} and {hi} "
                     f"({'shorten' if total > hi else 'lengthen'} it)")
    thumbs = [i for i, s in enumerate(shots) if s["camera"].get("thumb")]
    for i in thumbs[1:]:
        del shots[i]["camera"]["thumb"]
    if not thumbs:
        shots[len(shots) // 2]["camera"]["thumb"] = True
    title = plan.get("title") if isinstance(plan.get("title"), str) else ""
    if not title.strip() or hindi_ratio(title) < 0.5:
        raise Reject("the 'title' must be a Hindi title")
    clean = {"title": title.strip()[:95], "rate": "-8%", "description": (plan.get("description") or "").strip(),
             "credits": cfg["credits"], "disclaimer": cfg["disclaimer"], "shots": shots}
    return clean, notes, total


# ------------------------------------------------------------------ prompt
def build_prompt(cfg, topic, genre, history, feedback):
    cast = "\n".join(f"- {c['name']} = model {c['model']}: {c['role']}" for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" for h in history[-15:]) or "- (none yet)"
    example = {
        "id": 6, "title": "Storm night",
        "visual": "Rain floods the field. Ramu stands calmly by his clay pot of savings while Shyam runs in panic.",
        "narration": "एक रात तेज़ बारिश आई और खेत में पानी भर गया। रामू बोला, \"घबराओ मत, हमारे पास बचत है।\" श्याम घबराकर भागता आया।",
        "env": "storm", "focus": "Farmer",
        "characters": [
            {"model": "Farmer", "action": "talk", "pos": [-1.5, 0.3]},
            {"model": "Worker", "action": "run", "pos": [-6.0, 0.5], "move": [1.2, 0.5]},
            {"model": "Cow", "action": "shock", "pos": [3.0, 1.5]}],
        "props": [{"model": "CommonTree_2", "pos": [4.5, 4.0], "scale": 1.5}],
        "camera": {"dist_end": 5.5, "sweep": -20}}
    return f"""You are the director of a daily 2.5-minute animated YouTube Short. It is a STORY (not a lecture) that secretly teaches money awareness.
Audience: {cfg['audience']}.
Lesson to teach: {topic}
Story style today: {genre}

STORY CRAFT (most important)
- A real story with named characters from the cast: a hero with a wish, an obstacle, a tempting mistake, a clear consequence, and a clever fix. Show the lesson through EVENTS the viewer sees, never as a lecture. Only the last two shots may recap the lesson.
- Shot plan for {cfg['shots']} shots: 1 hook (something happens at once), 2-3 the people and their problem, 4-5 the temptation or mistake, 6-8 the consequence (storm, scam, debt, loss), 9-10 the smart fix and the happy change, 11 three-point recap by the wise character, 12 education-only disclaimer and subscribe request.
- Narration = a storyteller's voice, vivid, 1-3 short sentences (100-140 characters), with one or two spoken lines in quotes, for example रामू बोला, "...". Language: {cfg['language']}.
- Total narration across all shots: between {cfg['min_chars']} and {cfg['max_chars']} characters. Count them.
- Each shot is ONE visible event. The narration must describe exactly what the viewer sees, and each character's action must match it: if the text says someone runs, that character's action is "run" with a destination; if someone speaks, action "talk" and the others "listen"; surprise = "shock"; a fight = "fight" or "kick".
- Educational only: never name stocks, funds, apps, banks or brands, never promise returns, never tell people to buy something.
- Make this story different from the earlier ones:
{past}
{('- Fix this problem from your last answer: ' + feedback) if feedback else ''}

CAST (use these names and models consistently; at least {cfg.get('min_human_shots', 8)} of the shots must show a human):
{cast}

WHAT THE RENDERER CAN DO (use ONLY these)
- Character "model": {', '.join(HUMANS)}, {', '.join(ANIMALS)}
- Character "action" (pick one per character): {', '.join(ACTIONS)}
   talk = gesturing speaker, listen = standing and listening, walk/run = moves from "pos" to "move",
   shock = surprised, hurt = knocked back, fight/kick = attack, eat, sad (head low), celebrate, wave, jump, faint, sword, idle.
- "move":[x,y] is REQUIRED for walk and run (the end point). Everyone else stands at "pos".
- Positions: x from -6 to 6 (left to right), y from -1 to 5 (near to far). Keep standing characters at least 2 units apart. Walkers may start off-screen at x=-6.5 or 6.5. I will turn characters toward each other automatically.
- "focus" = the model of the main character of the shot (the speaker or the one the event happens to).
- env: {', '.join(ENVS)} (village = sunny village, forest = green forest, storm = rain and dark sky)
- props (optional, background only): {', '.join(cfg.get('props_allowed') or DEFAULT_PROPS)}; CommonTree_* may have "grow":[start,end] to show a seed becoming a tree.
- camera (optional): "dist_end" to push in, "sweep" -30..30, "angle" -15..15; put "thumb":true on exactly one shot, the most exciting one. I place the camera on the action automatically.
- title: 2-4 English words. visual: one English sentence of what the viewer sees.

Return ONLY JSON: {{"title": "<Hindi video title, max 90 characters, honest, no clickbait lies>", "description": "<Hindi description, 2-3 sentences>", "shots": [ ... ]}}
One shot looks like this:
{json.dumps(example, ensure_ascii=False)}
"""


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="director_config.json")
    ap.add_argument("--progress", default="director_progress.json")
    ap.add_argument("--out-dir", default="plans")
    ap.add_argument("--topic", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--models", default="")
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument("--validate-only", default="")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))

    if a.validate_only:
        plan = json.load(open(a.validate_only, encoding="utf-8"))
        try:
            clean, notes, total = repair_plan(plan, cfg, strict_length=False)
        except Reject as e:
            sys.exit(f"INVALID: {e}")
        print(f"Plan is usable: {len(clean['shots'])} shots, {total} characters (about {total / 10.4:.0f} s of voice).")
        for n in notes:
            print(" fixed:", n)
        return

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("FAIL: GEMINI_API_KEY is empty. Add it under Settings > Secrets > Actions.")
    prog = {"next_topic": 0, "history": []}
    if os.path.exists(a.progress):
        try:
            prog.update(json.load(open(a.progress, encoding="utf-8")))
        except Exception:
            pass
    topics = cfg.get("topics") or []
    if a.topic:
        topic = a.topic
    elif prog["next_topic"] < len(topics):
        topic = topics[prog["next_topic"]]
    else:
        topic = "Choose a NEW beginner money-awareness lesson for Indian villagers that is not in the earlier stories list"
    genres = cfg.get("genres") or ["a village folk tale"]
    genre = genres[len(prog["history"]) % len(genres)]
    print("Topic:", topic, "| style:", genre)

    models = pick_models(key, a.models)
    print("Gemini models:", ", ".join(models))
    state = {"dead": set(), "model": ""}
    feedback, plan, notes, total = "", None, [], 0
    for attempt in range(1, a.attempts + 1):
        print(f"Director attempt {attempt}/{a.attempts}", flush=True)
        try:
            raw = ask_json(build_prompt(cfg, topic, genre, prog["history"], feedback), models, key, state)
            plan, notes, total = repair_plan(raw, cfg, strict_length=True)
            break
        except Reject as e:
            feedback = str(e)
            print("  rejected:", feedback, flush=True)
            plan = None
        except RuntimeError as e:
            print("  ", e, flush=True)
            if attempt == a.attempts:
                break
            SLEEP(30)
    if plan is None:
        sys.exit(f"FAIL: could not get a usable story after {a.attempts} attempts. Last problem: {feedback}")

    date = a.date or datetime.date.today().isoformat()
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"plan_{date}.json")
    k = 2
    while os.path.exists(path):
        path = os.path.join(a.out_dir, f"plan_{date}_{k}.json")
        k += 1
    plan["topic"], plan["genre"], plan["director_model"] = topic, genre, state["model"]
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not a.no_progress:
        if not a.topic and prog["next_topic"] < len(topics):
            prog["next_topic"] += 1
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path})
        json.dump(prog, open(a.progress, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    est = total / 10.4
    summary = (f"### Director result\n\n**{plan['title']}**\n\n- Plan file: `{path}`\n- Topic: {topic}\n- Style: {genre}\n"
               f"- Model: {state['model']}\n- Shots: {len(plan['shots'])}\n"
               f"- Narration: {total} characters, about {est:.0f} s of voice, about {est + 0.85 * len(plan['shots']):.0f} s video\n\n"
               "| Shot | Focus | Who does what |\n|---|---|---|\n")
    for s in plan["shots"]:
        who = ", ".join(f"{c['model']}:{c['anim']}" for c in s["characters"])
        summary += f"| {s['id']} {s['title']} | {s['focus']} | {who} |\n"
    if notes:
        summary += "\nAuto-fixes:\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    open("director_summary.md", "w", encoding="utf-8").write(summary)
    print(summary)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"plan_file={path}\n")


if __name__ == "__main__":
    main()
