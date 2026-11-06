#!/usr/bin/env python3
"""story_director.py - Gemini is the DIRECTOR of a 2.5 minute animal Short about money awareness.

  python3 story_director.py                      # next topic from animal_studio.json
  python3 story_director.py --topic "..."        # your own topic
  python3 story_director.py --validate-only plans/episode_x.json

Output: plans/episode_YYYY-MM-DD.json and director_report.md

Gemini writes the story AND the direction (who walks where, who talks, camera framing, angle and
movement). This script then CHECKS it, repairs small mistakes (unknown animal, wrong action word,
characters too far apart, an animal named in the story but missing from the picture, a verb like "ran"
without a running animal ...) and sends a rejected story back to Gemini with the reason.
A second Gemini pass (continuity check) compares narration and actions shot by shot.
"""
import argparse, datetime, json, math, os, re, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shorts_core as core

BASE = "https://generativelanguage.googleapis.com/v1beta"
SLEEP = time.sleep


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class Reject(Exception):
    """The story is not good enough. The reason goes back to Gemini."""


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
        raise GErr(f"HTTP {e.code}: {text[:300]}", e.code, e.code in (408, 429, 500, 502, 503, 504), wait, text)
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
    return [n for _, _, n in found][:4] or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


def ask_json(prompt, models, key, state, temperature=0.85):
    last = None
    for model in models:
        if model in state["dead"]:
            continue
        for attempt in range(1, 4):
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}}
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


# ------------------------------------------------------------------ text helpers
def count_units(text, unit):
    if unit == "chars":
        return len(text)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text))


def lang_ratio(text, language):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    if language == "Hindi":
        return sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters)
    return sum(1 for c in letters if c.isascii()) / len(letters)


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ English verb -> action fixes
VERB_RULES = [
    ("run",       r"\b(ran|run|runs|running|raced|rushed|dashed|galloped|sprinted|bolted|charged|hurried)\b"),
    ("walk",      r"\b(walked|walks|walking|wandered|strolled|arrived|approached|entered|marched|trotted)\b"),
    ("eat",       r"\b(ate|eat|eating|munched|nibbled|chewed)\b"),
    ("shock",     r"\b(gasped|shocked|startled|froze|surprised|stunned)\b"),
    ("sad",       r"\b(sighed|wept|cried|sobbed|worried|ashamed|sad|regretted)\b"),
    ("celebrate", r"\b(cheered|danced|jumped|celebrated|happy|proud|hooray)\b"),
    ("kick",      r"\b(kicked)\b"),
    ("fight",     r"\b(butted|rammed|fought|attacked)\b"),
]
TALK_RE = re.compile(r"\b(said|asked|replied|shouted|whispered|called|cried out|explained|declared|told|answered|warned)\b|[\"“”]")


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def names_in(sentence, cast):
    """[(position, cast_entry)] for every cast name found in the sentence."""
    out = []
    for c in cast:
        m = re.search(r"\b" + re.escape(c["name"]) + r"\b", sentence, re.I)
        if m:
            out.append((m.start(), c))
    return sorted(out, key=lambda x: x[0])


def free_spot(chars, near, dx):
    for k in range(8):
        x = near[0] + dx * (1 + 0.5 * k) if k else near[0] + dx
        y = near[1] + (k % 3) * 0.9
        if all(math.hypot(x - c["pos"][0], y - c["pos"][1]) >= 1.3 for c in chars):
            return [round(core.clamp(x, -2.4, 2.4), 2), round(core.clamp(y, 0.0, 5.0), 2)]
    return [round(core.clamp(near[0] + dx, -2.4, 2.4), 2), round(near[1] + 1.2, 2)]


# ------------------------------------------------------------------ validation and repair
def repair_plan(raw, cfg, strict=True):
    notes = []
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    cast_by_id = {c["id"]: c for c in cfg["cast"]}
    cast_by_name = {c["name"].lower(): c for c in cfg["cast"]}
    if not isinstance(raw, dict) or not isinstance(raw.get("shots"), list):
        raise Reject("the JSON must be an object with a 'shots' list")
    want = int(cfg.get("shots", 20))
    if len(raw["shots"]) != want:
        raise Reject(f"you wrote {len(raw['shots'])} shots, I need exactly {want}")

    shots = []
    prev_look = "meadow_day"
    for n, s in enumerate(raw["shots"], start=1):
        if not isinstance(s, dict):
            raise Reject(f"shot {n} is not an object")
        text = (s.get("narration") or "").strip()
        if not text:
            raise Reject(f"shot {n} has no narration")
        if lang_ratio(text, language) < 0.75:
            raise Reject(f"shot {n} narration must be written in {language}")
        units = count_units(text, unit)
        lo, hi = tg["shot"]
        if strict and not (lo - 2 <= units <= hi + 4):
            raise Reject(f"shot {n} narration has {units} {unit}, each shot must have {lo} to {hi} {unit}")
        look = s.get("look") if s.get("look") in core.LOOKS else prev_look
        if s.get("look") not in core.LOOKS:
            notes.append(f"shot {n}: look '{s.get('look')}' unknown, kept '{prev_look}'")
        prev_look = look

        chars = []
        for c in (s.get("characters") or [])[:5]:
            if not isinstance(c, dict):
                continue
            ref = c.get("id") or c.get("name") or ""
            entry = cast_by_id.get(str(ref).lower()) or cast_by_name.get(str(ref).lower())
            if entry is None and c.get("model") in core.ANIMALS:
                entry = next((x for x in cfg["cast"] if x["model"] == c["model"]), None)
            if entry is None or any(x["id"] == entry["id"] for x in chars):
                notes.append(f"shot {n}: character '{ref}' not in the cast or repeated, removed")
                continue
            beats = []
            for b in (c.get("beats") or [])[:3]:
                if not isinstance(b, dict):
                    continue
                act = b.get("action") if b.get("action") in core.ACTIONS else "idle"
                nb = {"action": act}
                at = num(b.get("at"))
                if at is not None:
                    nb["at"] = round(core.clamp(at, 0.0, 0.95), 2)
                to = b.get("to")
                if isinstance(to, (list, tuple)) and len(to) >= 2 and core.ACTIONS[act]["speed"] > 0:
                    nb["to"] = [round(core.clamp(num(to[0], 0), -2.6, 2.6), 2), round(core.clamp(num(to[1], 2), 0.0, 5.5), 2)]
                beats.append(nb)
            first_moves = bool(beats and core.ACTIONS[beats[0]["action"]]["speed"] > 0 and "to" in beats[0])
            px = core.clamp(num((c.get("pos") or [0, 2])[0], 0), -7.0 if first_moves else -2.4, 7.0 if first_moves else 2.4)
            py = core.clamp(num((c.get("pos") or [0, 2])[1] if len(c.get("pos") or []) > 1 else 2, 2), 0.0, 5.5)
            chars.append({"id": entry["id"], "model": entry["model"], "pos": [round(px, 2), round(py, 2)],
                          "beats": beats or [{"action": "idle"}],
                          **({"face": c["face"]} if c.get("face") in cast_by_id else {})})
        # an animal named in the narration must be in the picture
        for _, entry in names_in(text, cfg["cast"]):
            if len(chars) < 4 and all(x["id"] != entry["id"] for x in chars):
                near = chars[0]["pos"] if chars else [0, 2]
                chars.append({"id": entry["id"], "model": entry["model"], "pos": free_spot(chars, near, 1.6),
                              "beats": [{"action": "listen"}]})
                notes.append(f"shot {n}: {entry['name']} is named in the story, added to the picture")
        if not chars:
            raise Reject(f"shot {n} has no valid characters (use only the cast ids)")
        chars = chars[:4]

        cam_in = s.get("camera") if isinstance(s.get("camera"), dict) else {}
        focus = cam_in.get("focus") if cam_in.get("focus") in [c["id"] for c in chars] else chars[0]["id"]
        cam = {"framing": cam_in.get("framing") if cam_in.get("framing") in core.FRAMINGS else "medium",
               "angle": cam_in.get("angle") if cam_in.get("angle") in core.ANGLES else "eye",
               "move": cam_in.get("move") if cam_in.get("move") in core.MOVES else "static",
               "focus": focus,
               "side": cam_in.get("side") if cam_in.get("side") in core.SIDES else "center"}
        shot = {"id": n, "title": (s.get("title") if isinstance(s.get("title"), str) and 2 <= len(s["title"]) <= 40 else f"Shot {n}"),
                "look": look, "narration": text, "visual": str(s.get("visual") or "")[:220],
                "camera": cam, "characters": chars}
        if s.get("thumb"):
            shot["thumb"] = True
        shots.append(shot)

    # ---- separation of standing characters (vertical frame: stack them in depth, keep them close)
    for sh in shots:
        placed = []
        for c in sh["characters"]:
            tries = 0
            while tries < 8 and any(math.hypot(c["pos"][0] - p[0], c["pos"][1] - p[1]) < 1.3 for p in placed):
                c["pos"][1] = round(c["pos"][1] + 1.0, 2)
                tries += 1
            placed.append(c["pos"])

    # ---- the verbs of the narration must be visible (English only)
    if language == "English":
        for sh in shots:
            fix_actions(sh, cfg, notes)

    # ---- camera variety
    framings = {sh["camera"]["framing"] for sh in shots}
    if strict and len(framings) < 3:
        raise Reject("use more camera variety: at least 3 different framings (extreme_close, close, medium, wide)")
    for i in range(2, len(shots)):
        a, b, c = (shots[i - 2]["camera"], shots[i - 1]["camera"], shots[i]["camera"])
        if (a["framing"], a["move"]) == (b["framing"], b["move"]) == (c["framing"], c["move"]):
            c["move"] = "push_in" if c["move"] != "push_in" else "pan"
            notes.append(f"shot {i + 1}: three equal camera setups in a row, camera move changed")

    # ---- last shot: disclaimer
    disc = cfg["disclaimer"][language]
    last = shots[-1]["narration"]
    key_words = ("education", "advice") if language == "English" else ("शिक्षा", "सलाह")
    if not all(k in last.lower() for k in key_words):
        shots[-1]["narration"] = last.rstrip() + " " + disc
        notes.append("disclaimer sentence added to the last shot")

    total = sum(count_units(sh["narration"], unit) for sh in shots)
    tlo, thi = tg["total"]
    if strict and not (tlo <= total <= thi):
        raise Reject(f"total narration is {total} {unit}, it must be between {tlo} and {thi} "
                     f"({'shorten' if total > thi else 'lengthen'} it so the whole video is 2.5 minutes)")

    thumbs = [i for i, sh in enumerate(shots) if sh.get("thumb")]
    for i in thumbs[1:]:
        del shots[i]["thumb"]
    if not thumbs:
        shots[len(shots) // 2]["thumb"] = True

    title = raw.get("title") if isinstance(raw.get("title"), str) else ""
    if not title.strip():
        raise Reject("the 'title' is missing")
    suffix = cfg.get("title_suffix", "")
    title = title.strip()
    if suffix and title.endswith(suffix):
        title = title[:-len(suffix)]
    title = title[:90 - len(suffix)]
    tags = [str(t)[:30] for t in (raw.get("tags") or []) if isinstance(t, str)][:15]
    used_ids = []
    for sh in shots:
        for c in sh["characters"]:
            if c["id"] not in used_ids:
                used_ids.append(c["id"])
    plan = {"version": 3, "language": language, "title": title + cfg.get("title_suffix", ""),
            "description": str(raw.get("description") or "").strip(), "tags": tags,
            "disclaimer": disc, "credits": cfg["credits"], "target_seconds": cfg.get("target_seconds", 150),
            "cast": [cast_by_id[i] for i in used_ids], "shots": shots}
    return plan, notes, total


def fix_actions(sh, cfg, notes):
    chars = {c["id"]: c for c in sh["characters"]}
    focus = sh["camera"]["focus"]

    def has_move(c):
        return any(core.ACTIONS[b["action"]]["speed"] > 0 and "to" in b for b in c["beats"])

    for sent in sentences(sh["narration"]):
        named = [(p, e) for p, e in names_in(sent, cfg["cast"]) if e["id"] in chars]
        for action, rx in VERB_RULES:
            m = re.search(rx, sent, re.I)
            if not m:
                continue
            before = [e for p, e in named if p < m.start()]
            if not named:
                continue                       # nobody is named: do not guess who does it
            subj_id = before[-1]["id"] if before else named[0][1]["id"]
            c = chars.get(subj_id)
            if c is None:
                continue
            cur = {b["action"] for b in c["beats"]}
            if action in ("run", "walk"):
                if has_move(c):
                    continue
                others = [o for o in sh["characters"] if o is not c]
                anchor = others[0]["pos"] if others else [0, 2.5]
                side = 1 if c["pos"][0] >= anchor[0] else -1          # arrive from the side it is already on
                dest = [round(core.clamp(anchor[0] + side * 1.7, -2.4, 2.4), 2), round(anchor[1], 2)]
                c["pos"] = [round(core.clamp(dest[0] + side * 5.0, -7.0, 7.0), 2), dest[1]]
                c["beats"] = [{"action": action, "to": dest, "at": 0.0}, {"action": "talk" if TALK_RE.search(sent) else "idle"}]
                notes.append(f"shot {sh['id']}: the story says {c['id']} {action}s, movement added")
            elif action not in cur and not has_move(c):
                c["beats"] = [{"action": action}]
                notes.append(f"shot {sh['id']}: the story says {c['id']} is '{action}', action set")
            break
        # who speaks
        if TALK_RE.search(sent) and named:
            spk = named[0][1]["id"]
            for p, e in named:
                if re.search(re.escape(e["name"]) + r"\W+(\w+\W+){0,2}(said|asked|replied|shouted|whispered|called|explained|declared|told|answered|warned)", sent, re.I):
                    spk = e["id"]
                    break
            c = chars.get(spk)
            if c and not has_move(c) and not any(b["action"] in ("talk", "shock", "sad", "eat", "celebrate", "fight", "kick") for b in c["beats"]):
                c["beats"] = [{"action": "talk"}]
                notes.append(f"shot {sh['id']}: {spk} speaks, action set to talk")


# ------------------------------------------------------------------ prompt
def build_prompt(cfg, topic, genre, history, feedback):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    cast = "\n".join(f"- id \"{c['id']}\" = {c['name']} (model {c['model']}): {c['role']}" for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" for h in history[-15:]) or "- (none yet)"
    n = cfg.get("shots", 20)
    example = {
        "id": 9, "title": "The costly bargain", "look": "meadow_day",
        "visual": "Bhola proudly shows a shiny bell. Lakshmi looks at his empty coin bag with worry.",
        "narration": "Bhola jingled his new golden bell proudly, but his coin bag was completely empty." if language == "English"
        else "भोला ने अपनी नई सुनहरी घंटी बजाई, पर उसकी थैली खाली थी।",
        "camera": {"framing": "medium", "angle": "eye", "move": "push_in", "focus": "bhola", "side": "left"},
        "characters": [
            {"id": "bhola", "pos": [-1.0, 2.0], "beats": [{"action": "celebrate", "at": 0.0}, {"action": "sad", "at": 0.6}]},
            {"id": "lakshmi", "pos": [1.0, 2.6], "face": "bhola", "beats": [{"action": "walk", "to": [0.7, 2.4], "at": 0.0}, {"action": "listen"}]}]}
    return f"""You are an award-winning animation director, a children's storyteller and a money-awareness teacher, all in one.
Make a {cfg.get('target_seconds', 150)}-second vertical (9:16) YouTube Short. The heroes are ANIMALS in a natural world (meadow, forest, riverbank, storm, night). It is a real STORY with feeling and humour that secretly teaches one money lesson. It is NOT a lecture.
Audience: families and first-time earners in India. Language of the narration: {language}.
Lesson to teach: {topic}
Story style today: {genre}

STORY RULES
1. Exactly {n} shots. Each shot is ONE clear visible moment of 4 to 9 seconds, told by 1 or 2 short sentences ({tg['shot'][0]} to {tg['shot'][1]} {unit}). The WHOLE narration must be {tg['total'][0]} to {tg['total'][1]} {unit} in total, because the voice must fill 2.5 minutes. Count carefully.
2. Structure: shots 1-2 hook (something surprising happens at once); 3-5 meet the hero and the wish; 6-8 temptation or trick; 9-12 the mistake and its painful, funny consequence; 13-16 the wise animal shows the rule with small rupee numbers the viewer can follow; 17-18 the hero applies it and things get better; 19 three-point recap in simple words; 20 education-only disclaimer and a friendly "subscribe for more stories".
3. Use 3 to 5 cast animals. Same animal = same role in every shot. Give them feeling (worry, pride, shock, joy). Short spoken lines in quotes are welcome.
4. Teach only the lesson of today. Never name real banks, apps, brands, stocks or funds. Never promise returns. Never tell people to buy anything. Money amounts are small and simple (Rs 100, Rs 20).
5. Do not repeat these earlier stories:
{past}
{('6. FIX THIS PROBLEM FROM YOUR LAST ANSWER: ' + feedback) if feedback else ''}

CAST (use ONLY these ids)
{cast}

DIRECTION RULES (the renderer follows them exactly, so be precise)
- Every verb in the narration that an animal does must be visible. "ran" = action run with a destination "to". "walked in" = walk with "to". "said" = that animal's action talk, the others listen. Surprise = shock. Sadness or worry = sad. Eating = eat. Joy = celebrate. Hit = kick or fight. Hurt = hurt.
- Actions allowed: {', '.join(core.ACTIONS)}. Only walk and run move; they REQUIRE "to":[x,y]. Walking speed is 1.4 m/s, running 5.5 m/s, so a 6 second shot allows at most about 8 m of walking.
- Each character has 1 to 3 "beats" in time order. "at" is when the beat starts as a fraction of the shot (0 = at the start of the narration, 0.5 = half way). Start every important action at the moment the narration mentions it.
- The frame is VERTICAL and narrow. Keep every character at x from -2.4 to 2.4 and y from 0 to 5 (y = distance from the camera; use depth to separate animals, at least 1.3 m apart). Use at most 2 characters in close or extreme_close shots, at most 4 in any shot. An animal may start off-screen (x up to +-7) only if its first beat is a walk or run to a point inside.
- "face": the id of the animal this character looks at (default: the camera focus).
- camera = {{"framing": one of {core.FRAMINGS}, "angle": one of {core.ANGLES}, "move": one of {core.MOVES}, "focus": id of the animal the camera is about, "side": one of {core.SIDES}}}.
  Think like a director: open with a wide establishing shot; use close or extreme_close for emotion and the key lesson line; use medium for conversations; push_in on important lines; pull_out for consequences; follow for walking or running; low angle for a proud or powerful animal, high angle for a small or sad one; orbit or crane_up for the big moment. Never repeat the same framing and move 3 times in a row. Use at least 3 different framings.
- look = one of {list(core.LOOKS)}. Change the look only when the story moves to a new place or time (meadow_day, forest_day, golden_sunset, riverbank_dawn, storm_dusk, night_moon).
- Put "thumb": true on the single most eye-catching shot.

SELF-CHECK before you answer: shots = {n}; total {unit} in range; every named animal appears in that shot's characters; every walk/run has "to"; no animal is outside the allowed area; last shot has the disclaimer idea ({cfg['disclaimer'][language]}).

Return ONLY JSON:
{{"title": "<catchy honest {language} title, max 60 characters>", "description": "<2-3 sentence {language} description>", "tags": ["<10 short tags>"], "shots": [ ... {n} shots ... ]}}
One shot looks like this:
{json.dumps(example, ensure_ascii=False)}
"""


# ------------------------------------------------------------------ continuity pass
def critic_pass(plan, cfg, models, key, state):
    brief = [{"id": s["id"], "narration": s["narration"], "focus": s["camera"]["focus"],
              "characters": [{"id": c["id"], "pos": c["pos"], "beats": c["beats"]} for c in s["characters"]]}
             for s in plan["shots"]]
    prompt = f"""You are the continuity checker of an animated story. Compare each shot's NARRATION with what the animals DO.
A shot is WRONG if the narration says an animal does something (runs, walks in, speaks, is shocked, eats, is sad, celebrates, fights ...) but that animal's beats show something else, if a speaking animal is not the one with action talk, or if an animal that acts in the narration is missing.
Actions allowed: {', '.join(core.ACTIONS)}. walk and run need "to":[x,y]. Allowed ids: {', '.join(c['id'] for c in cfg['cast'])}. At most 4 characters per shot, x -2.4..2.4, y 0..5, animals at least 1.3 m apart, beats in time order with "at" 0..0.95.
Return ONLY JSON: {{"fixes": [{{"id": <shot id>, "characters": [{{"id": "...", "pos": [x, y], "beats": [{{"action": "...", "to": [x, y], "at": 0.0}}]}}]}}]}}
Include ONLY shots that must change. If everything matches, return {{"fixes": []}}.

SHOTS:
{json.dumps(brief, ensure_ascii=False)}
"""
    try:
        res = ask_json(prompt, models, key, state, temperature=0.2)
    except (Reject, RuntimeError) as e:
        print("  continuity pass skipped:", str(e)[:120])
        return None, []
    raw = json.loads(json.dumps(plan))
    fixed = []
    for f in (res.get("fixes") if isinstance(res, dict) else None) or []:
        try:
            idx = int(f["id"]) - 1
            if 0 <= idx < len(raw["shots"]) and isinstance(f.get("characters"), list) and f["characters"]:
                raw["shots"][idx]["characters"] = f["characters"]
                fixed.append(int(f["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return raw, fixed


def report_md(plan, topic, genre, model, total, unit, notes, path):
    est = total / 2.4 if unit == "words" else total / 10.4
    md = (f"### Director report\n\n**{plan['title']}**\n\n- Plan file: `{path}`\n- Topic: {topic}\n- Style: {genre}\n"
          f"- Gemini model: {model}\n- Shots: {len(plan['shots'])} | narration: {total} {unit} (about {est:.0f} s of voice, "
          f"the voice step fits the video to exactly {plan['target_seconds']} s)\n\n"
          "| # | Look | Camera | Who does what | Narration |\n|---|---|---|---|---|\n")
    for s in plan["shots"]:
        cam = s["camera"]
        who = "; ".join(f"{c['id']}: " + ">".join(b["action"] for b in c["beats"]) for c in s["characters"])
        md += (f"| {s['id']} | {s['look']} | {cam['framing']}/{cam['angle']}/{cam['move']} on {cam['focus']} | "
               f"{who} | {s['narration'].replace('|', '/')} |\n")
    if notes:
        md += "\nAutomatic fixes:\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    return md


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="animal_studio.json")
    ap.add_argument("--progress", default="studio_progress.json")
    ap.add_argument("--out-dir", default="plans")
    ap.add_argument("--topic", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--language", default="", help="English or Hindi (default: from the config)")
    ap.add_argument("--models", default="")
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument("--validate-only", default="")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    if a.language:
        cfg["language"] = a.language
    language = cfg["language"]
    unit = cfg["targets"][language]["unit"]

    if a.validate_only:
        plan = json.load(open(a.validate_only, encoding="utf-8"))
        try:
            clean, notes, total = repair_plan(plan, cfg, strict=False)
        except Reject as e:
            sys.exit(f"INVALID: {e}")
        print(f"Plan is usable: {len(clean['shots'])} shots, {total} {unit}.")
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
        topic = "Choose a NEW beginner money-awareness lesson that is not in the earlier stories list"
    genres = cfg.get("genres") or ["a fable"]
    genre = genres[len(prog["history"]) % len(genres)]
    print("Topic:", topic, "| style:", genre, "| language:", language)

    models = pick_models(key, a.models)
    print("Gemini models:", ", ".join(models))
    state = {"dead": set(), "model": ""}
    feedback, plan, notes, total = "", None, [], 0
    for attempt in range(1, a.attempts + 1):
        print(f"Director attempt {attempt}/{a.attempts}", flush=True)
        try:
            raw = ask_json(build_prompt(cfg, topic, genre, prog["history"], feedback), models, key, state)
            plan, notes, total = repair_plan(raw, cfg, strict=True)
            fixed_raw, fixed = critic_pass(plan, cfg, models, key, state)
            if fixed:
                try:
                    plan2, notes2, total2 = repair_plan(fixed_raw, cfg, strict=True)
                    plan, total = plan2, total2
                    notes = notes + [f"continuity pass changed the direction of shots {fixed}"] + notes2
                except Reject as e:
                    print("  continuity fixes were not usable, kept the first version:", e)
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
        sys.exit(f"FAIL: no usable story after {a.attempts} attempts. Last problem: {feedback}")

    date = a.date or datetime.date.today().isoformat()
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"episode_{date}.json")
    k = 2
    while os.path.exists(path):
        path = os.path.join(a.out_dir, f"episode_{date}_{k}.json")
        k += 1
    plan["topic"], plan["genre"], plan["director_model"] = topic, genre, state["model"]
    json.dump(plan, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not a.no_progress:
        if not a.topic and prog["next_topic"] < len(topics):
            prog["next_topic"] += 1
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path})
        json.dump(prog, open(a.progress, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    md = report_md(plan, topic, genre, state["model"], total, unit, notes, path)
    open("director_report.md", "w", encoding="utf-8").write(md)
    print(md)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"plan_file={path}\n")


if __name__ == "__main__":
    main()
