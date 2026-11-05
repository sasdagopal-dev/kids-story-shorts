#!/usr/bin/env python3
"""director.py - Gemini as the DIRECTOR. Writes today's story + scene plan for the renderer.

  python3 director.py                       # next topic from director_config.json
  python3 director.py --topic "..."         # your own topic
  python3 director.py --validate-only plan.json   # check/repair an existing plan

Output: plans/plan_YYYY-MM-DD.json  (same format as your Ramu plan: title, narration, env,
characters, props, camera per shot). Everything Gemini writes is checked against the asset
lists below and repaired, so the renderer never gets an animal or animation that does not exist.
If the answer is rejected (wrong length, wrong shot count, not Hindi...) Gemini is asked again
with the reason, up to --attempts times, and other Gemini models are tried.
Edit the ASSET lists below if you add packs.
"""
import argparse, datetime, json, os, re, sys, time, urllib.error, urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"

# ---- assets the renderer can use (from your asset check and your Ramu plan)
HUMANS = ["Farmer", "King", "Worker", "Suit", "Adventurer", "Casual_2", "Casual_Hoodie", "Beach", "Punk"]
ANIMALS = ["Cow", "Horse_White", "Horse", "Fox", "Donkey", "Deer", "Stag", "Wolf", "Husky", "ShibaInu", "Alpaca", "Bull"]
HUMAN_ANIMS = ["Idle", "Idle_Neutral", "Walk", "Run", "Wave", "Interact", "Death", "HitRecieve_2",
               "Sword_Slash", "Punch", "Kick", "Roll"]
ANIMAL_ANIMS = ["Idle", "Idle_2", "Walk", "Gallop", "Eating", "Death"]
MOVING = {"Walk", "Run", "Gallop"}
PROPS = ["Pot", "CommonTree_1", "CommonTree_2"]
ENVS = ["village", "forest", "storm"]

SLEEP = time.sleep


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class Reject(Exception):
    """The answer was received but is not good enough. The reason goes back to Gemini."""


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
    """One question to Gemini, with retries and model switching. Returns parsed JSON."""
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
                body_t = (e.text or str(e)).lower()
                if "api key not valid" in body_t or e.code in (401, 403):
                    sys.exit(f"FAIL: Gemini rejected the key or access: {str(e)[:200]}")
                if e.code == 404 or (e.code == 429 and ("perday" in body_t or "per day" in body_t)):
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


# ------------------------------------------------------------------ validation and repair
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def num(x, default):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pair(v, default):
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return [round(clamp(num(v[0], default[0]), -7.5, 7.5), 2), round(clamp(num(v[1], default[1]), -2.5, 7.5), 2)]
    return list(default)


def hindi_ratio(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters)


def fix_character(c, notes, n):
    if not isinstance(c, dict):
        return None
    model = c.get("model")
    if model not in HUMANS + ANIMALS:
        notes.append(f"shot {n}: unknown character '{model}' removed")
        return None
    anims = HUMAN_ANIMS if model in HUMANS else ANIMAL_ANIMS
    anim = c.get("anim")
    if anim not in anims:
        notes.append(f"shot {n}: {model} animation '{anim}' not available, used Idle")
        anim = "Idle"
    out = {"model": model, "anim": anim, "pos": pair(c.get("pos"), [0, 0])}
    if "heading" in c:
        out["heading"] = round(clamp(num(c["heading"], 0), -180, 180))
    if anim in MOVING and isinstance(c.get("move"), (list, tuple)):
        out["move"] = pair(c["move"], out["pos"])
    return out


def fix_prop(p, notes, n):
    if not isinstance(p, dict) or p.get("model") not in PROPS:
        notes.append(f"shot {n}: unknown prop removed")
        return None
    out = {"model": p["model"], "pos": pair(p.get("pos"), [0, 2])}
    if "scale" in p:
        out["scale"] = round(clamp(num(p["scale"], 1.0), 0.5, 2.5), 2)
    if "heading" in p:
        out["heading"] = round(clamp(num(p["heading"], 0), -180, 180))
    g = p.get("grow")
    if isinstance(g, (list, tuple)) and len(g) >= 2 and p["model"].startswith("CommonTree"):
        out["grow"] = [round(clamp(num(g[0], 0.15), 0.05, 1.0), 2), round(clamp(num(g[1], 1.4), 0.5, 2.5), 2)]
    return out


def fix_camera(cam):
    cam = cam if isinstance(cam, dict) else {}
    t = cam.get("target")
    if isinstance(t, (list, tuple)) and len(t) >= 3:
        target = [round(clamp(num(t[0], 0), -6, 6), 2), round(clamp(num(t[1], 0), -2, 6), 2),
                  round(clamp(num(t[2], 1.2), 0.5, 2.5), 2)]
    else:
        target = [0, 0.3, 1.2]
    out = {"target": target, "dist": round(clamp(num(cam.get("dist"), 8), 4.5, 11), 1)}
    if "dist_end" in cam:
        out["dist_end"] = round(clamp(num(cam["dist_end"], out["dist"]), 4.0, 11), 1)
    if "sweep" in cam:
        out["sweep"] = round(clamp(num(cam["sweep"], 0), -45, 45))
    if "angle" in cam:
        out["angle"] = round(clamp(num(cam["angle"], 0), -25, 20))
    if cam.get("thumb"):
        out["thumb"] = True
    return out


def repair_plan(plan, cfg, strict_length=True):
    """Returns (clean_plan, notes). Raises Reject when the story itself must be rewritten."""
    notes = []
    if not isinstance(plan, dict) or not isinstance(plan.get("shots"), list):
        raise Reject("the JSON must be an object with a 'shots' list")
    want = int(cfg.get("shots", 12))
    shots_in = plan["shots"]
    if not (want - 2 <= len(shots_in) <= want + 2):
        raise Reject(f"you wrote {len(shots_in)} shots, I need exactly {want}")
    shots = []
    for n, s in enumerate(shots_in, start=1):
        text = (s.get("narration") or "").strip() if isinstance(s, dict) else ""
        if not text:
            raise Reject(f"shot {n} has no narration")
        if hindi_ratio(text) < 0.7:
            raise Reject(f"shot {n} narration is not Hindi in Devanagari script")
        chars = [fix_character(c, notes, n) for c in (s.get("characters") or [])[:6]]
        chars = [c for c in chars if c][:5]
        if not chars:
            notes.append(f"shot {n}: no valid character, added the farmer")
            chars = [{"model": "Farmer", "anim": "Idle", "pos": [0, 0]}]
        props = [fix_prop(p, notes, n) for p in (s.get("props") or [])[:4]]
        props = [p for p in props if p][:3]
        env = s.get("env")
        if env not in ENVS:
            notes.append(f"shot {n}: environment '{env}' not available, used village")
            env = "village"
        title = s.get("title") if isinstance(s.get("title"), str) else ""
        if not re.fullmatch(r"[A-Za-z0-9 _\-]{2,40}", title or ""):
            title = f"Shot {n}"
        shot = {"id": n, "title": title, "narration": text, "env": env, "characters": chars}
        if props:
            shot["props"] = props
        shot["camera"] = fix_camera(s.get("camera"))
        shots.append(shot)

    # the last shot must carry the disclaimer
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
    desc = (plan.get("description") or "").strip()
    clean = {
        "title": title.strip()[:95],
        "rate": "-8%",
        "description": desc,
        "credits": cfg["credits"],
        "disclaimer": cfg["disclaimer"],
        "shots": shots,
    }
    return clean, notes, total


# ------------------------------------------------------------------ prompt
def build_prompt(cfg, topic, history, feedback):
    cast = "\n".join(f"- {c['name']} = model {c['model']}: {c['role']}" for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" for h in history[-15:]) or "- (none yet)"
    example = {
        "id": 4, "title": "First rule", "narration": "राजा ने पहला नियम बताया। ...",
        "env": "village",
        "characters": [{"model": "Farmer", "anim": "Idle", "pos": [-2.2, 0.3], "heading": 70},
                       {"model": "King", "anim": "Interact", "pos": [0.3, 0], "heading": -70},
                       {"model": "Fox", "anim": "Walk", "pos": [-6.0, 0], "move": [4.0, 0]}],
        "props": [{"model": "Pot", "pos": [1.3, 0.2], "scale": 1.6}],
        "camera": {"target": [0, 0, 1.2], "dist": 6.5, "dist_end": 5.0, "sweep": 25, "angle": -12}}
    p = f"""You are the director of a daily 2.5-minute animated YouTube Short about money and finance awareness.
Audience: {cfg['audience']}.
Today's lesson: {topic}

Write ONE new short story that teaches this lesson, and direct every shot for a 3D renderer.

STORY RULES
- Exactly {cfg['shots']} shots. Narration language: {cfg['language']}.
- Total narration across all shots: between {cfg['min_chars']} and {cfg['max_chars']} characters (count them). Each shot about 100-140 characters, 1-3 short sentences, easy to speak aloud.
- Arc: hook in shot 1, a problem, the wise lesson, a clear example with small rupee numbers, a warning, a 3-point recap, then the last shot says the video is for education only, not investment or financial advice, and asks viewers to subscribe.
- Educational only: never name stocks, funds, apps, banks or brands, never promise returns, never tell people to buy something.
- Do not repeat these earlier stories, make the situation and ending different:
{past}
{('- Fix this problem from your last answer: ' + feedback) if feedback else ''}

CAST (use these, you may add the village animals):
{cast}

ASSETS THE RENDERER HAS (use ONLY these names)
- Human models: {', '.join(HUMANS)}   animations: {', '.join(HUMAN_ANIMS)}
- Animal models: {', '.join(ANIMALS)}   animations: {', '.join(ANIMAL_ANIMS)}
- Props: {', '.join(PROPS)} (CommonTree_* may have "grow":[start_scale,end_scale] to show a seed becoming a tree)
- env: {', '.join(ENVS)} (village = day village, forest = green forest, storm = rain and dark sky)

SHOT FIELDS
- title: 2-4 English words. narration: Hindi text spoken by the narrator.
- characters: 1-4 per shot. pos=[x,y] with x from -6 to 6 and y from -1 to 5 (x left-right, y depth). heading = degrees the character faces (70 faces right, -70 faces left, 0 faces the camera). "move":[x,y] only with Walk/Run/Gallop, the end point of the walk.
- Keep characters apart by at least 1.5 units. Put the speaker or the important character near the centre.
- camera: target=[x,y,z] (z about 1.2), dist 5-10 (smaller = closer), optional dist_end (slow push-in), sweep (-30..30 degrees) and angle (-15..15). Vary the camera between shots. Put "thumb":true on exactly one shot, the most eye-catching.
- Vary env, grouping and camera so the video does not look repetitive.

Return ONLY JSON: {{"title": "<Hindi video title, max 90 characters, no clickbait lies>", "description": "<Hindi description, 2-3 sentences>", "shots": [ ... ]}}
One shot looks like this:
{json.dumps(example, ensure_ascii=False)}
"""
    return p


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="director_config.json")
    ap.add_argument("--progress", default="director_progress.json")
    ap.add_argument("--out-dir", default="plans")
    ap.add_argument("--topic", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--models", default="", help="comma list; empty = best Gemini flash models your key sees")
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--no-progress", action="store_true", help="do not move to the next topic")
    ap.add_argument("--validate-only", default="")
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))

    if a.validate_only:
        plan = json.load(open(a.validate_only, encoding="utf-8"))
        try:
            clean, notes, total = repair_plan(plan, cfg, strict_length=False)
        except Reject as e:
            sys.exit(f"INVALID: {e}")
        print(f"Plan is usable: {len(clean['shots'])} shots, {total} characters "
              f"(about {total / 10.4:.0f} s of voice).")
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
        topic = ("Choose a NEW beginner money-awareness lesson for Indian villagers that is not "
                 "in the earlier stories list")
    print("Topic:", topic)

    models = pick_models(key, a.models)
    print("Gemini models:", ", ".join(models))
    state = {"dead": set(), "model": ""}
    feedback, plan, notes, total = "", None, [], 0
    for attempt in range(1, a.attempts + 1):
        print(f"Director attempt {attempt}/{a.attempts}", flush=True)
        try:
            raw = ask_json(build_prompt(cfg, topic, prog["history"], feedback), models, key, state)
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
    plan["topic"] = topic
    plan["director_model"] = state["model"]
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    if not a.no_progress:
        if not a.topic and prog["next_topic"] < len(topics):
            prog["next_topic"] += 1
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path})
        json.dump(prog, open(a.progress, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    est = total / 10.4
    summary = (f"### Director result\n\n**{plan['title']}**\n\n- Plan file: `{path}`\n- Topic: {topic}\n"
               f"- Model: {state['model']}\n- Shots: {len(plan['shots'])}\n"
               f"- Narration: {total} characters, about {est:.0f} s of voice, about {est + 0.85 * len(plan['shots']):.0f} s video\n")
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
