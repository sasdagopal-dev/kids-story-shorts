#!/usr/bin/env python3
"""story_director.py (v4) - Gemini is the DIRECTOR of an animal adventure Short.

  python3 story_director.py                      # next story idea from animal_studio.json
  python3 story_director.py --topic "..."        # your own story idea
  python3 story_director.py --validate-only plans/episode_x.json

Output: plans/episode_YYYY-MM-DD.json and director_report.md

New in v4
* animal adventure stories (a gentle life lesson, no finance needed)
* the video length is FLEXIBLE: target_seconds is calculated from the story, no fixed 2.5 minutes
* every quoted line has an exact speaker ("quote_speakers"), the speaker is the ONLY animal that talks,
  talk timing counts pauses, walkers arrive before the dialogue starts
* animals never stand behind each other (camera view), boulders never hide an animal
"""
import argparse, datetime, json, math, os, random, re, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shorts_core as core

VERSION = "v5 (season, one call, tolerant checks, busy-model rotation)"
BASE = "https://generativelanguage.googleapis.com/v1beta"
SLEEP = time.sleep


class GErr(Exception):
    def __init__(self, msg, code=None, retryable=True, wait=None, text=""):
        super().__init__(msg)
        self.code, self.retryable, self.wait, self.text = code, retryable, wait, text


class QuotaOut(Exception):
    """Every Gemini model has used up its daily quota: more attempts are pointless."""


class Reject(Exception):
    """The story is not good enough. The reason goes back to Gemini."""


# ------------------------------------------------------------------ Gemini
def gcall(model, body, key):
    req = urllib.request.Request(
        f"{BASE}/models/{model}:generateContent", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
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


def short_err(e):
    try:
        msg = json.loads(e.text).get("error", {}).get("message", "")
        return f"HTTP {e.code}: {msg[:90]}"
    except Exception:
        return str(e)[:110]


def ask_json(prompt, models, key, state, temperature=0.85, cycles=3):
    """Ask Gemini. A busy model (503) is not retried again and again: we move to the next model at once, and after a full
    round of busy models we wait a little longer and start a new round (up to `cycles` rounds)."""
    last = None
    for cycle in range(1, cycles + 1):
        alive = [m for m in models if m not in state["dead"]]
        if not alive:
            break
        transient = False
        for model in alive:
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
                    if e.code == 429:
                        state["quota_hit"] = True
                    print(f"  {model}: not usable today ({'daily quota used up' if e.code == 429 else 'not found'})", flush=True)
                    continue
                if e.code == 400 or (not e.retryable and e.code is not None):
                    print(f"  {model}: refused the request ({short_err(e)})", flush=True)
                    continue
                transient = True
                if e.code == 429 and e.wait and e.wait <= 45:          # per-minute limit: a short wait is enough
                    print(f"  {model}: per-minute limit, waiting {e.wait:.0f}s", flush=True)
                    SLEEP(e.wait + 2)
                else:
                    print(f"  {model}: busy ({short_err(e)}), trying the next model", flush=True)
        if not transient or cycle == cycles:
            break
        wait = min(90, 15 * cycle) + random.uniform(0, 5)
        print(f"  all models are busy, waiting {wait:.0f}s, then round {cycle + 1}/{cycles}", flush=True)
        SLEEP(wait)
    if state.get("quota_hit") and all(m in state["dead"] for m in models):
        raise QuotaOut(str(last)[:200])
    raise RuntimeError(f"no Gemini model answered: {str(last)[:160]}")


# ------------------------------------------------------------------ text helpers
def count_units(text, unit):
    if unit == "chars":
        return len(text)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text))


def lang_ok(text, language, cast):
    t = text
    for c in cast:
        t = re.sub(r"\b" + re.escape(c["name"]) + r"\b", "", t, flags=re.I)
    return lang_ratio(t, language) >= 0.6


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
def shot_cap(hi, unit, is_last):
    """Highest allowed size of one shot. More words are fine: the last shot carries the outro and the
    closing sentence, so it gets a lot of room (v5 allowed only hi+4 there in repair_plan = the rejects you saw)."""
    if unit == "words":
        return int(hi * 1.9) + (45 if is_last else 8)
    return int(hi * 1.9) + (400 if is_last else 40)


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

    def cast_id(ref):
        e = cast_by_id.get(str(ref).lower()) or cast_by_name.get(str(ref).lower())
        return e["id"] if e else None

    shots = []
    prev_look = "meadow_day"
    for n, s in enumerate(raw["shots"], start=1):
        if not isinstance(s, dict):
            raise Reject(f"shot {n} is not an object")
        text = (s.get("narration") or "").strip()
        if not text:
            raise Reject(f"shot {n} has no narration")
        if not lang_ok(text, language, cfg["cast"]):
            raise Reject(f"shot {n} narration must be written in {language}")
        units = count_units(text, unit)
        lo, hi = tg["shot"]
        if strict and not (int(lo * 0.5) <= units <= shot_cap(hi, unit, n == want)):
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
                          **({"face": c["face"]} if c.get("face") in cast_by_id else {}),
                          **({"tint": entry["tint"]} if entry.get("tint") else {})})
        for _, entry in names_in(text, cfg["cast"]):
            if len(chars) < 4 and all(x["id"] != entry["id"] for x in chars):
                near = chars[0]["pos"] if chars else [0, 2]
                chars.append({"id": entry["id"], "model": entry["model"], "pos": free_spot(chars, near, 1.6),
                              "beats": [{"action": "listen"}], **({"tint": entry["tint"]} if entry.get("tint") else {})})
                notes.append(f"shot {n}: {entry['name']} is named in the story, added to the picture")
        if not chars:
            raise Reject(f"shot {n} has no valid characters (use only the cast ids)")
        chars = chars[:4]

        props = []
        for p in (s.get("props") or [])[:3]:
            if isinstance(p, dict) and p.get("type") == "rock":
                pp = p.get("pos") if isinstance(p.get("pos"), (list, tuple)) and len(p["pos"]) >= 2 else [0, 4]
                props.append({"type": "rock", "pos": [round(core.clamp(num(pp[0], 0), -5, 5), 2), round(core.clamp(num(pp[1], 4), 0, 9), 2)],
                              "size": round(core.clamp(num(p.get("size"), 1.5), 0.6, 3.0), 2)})
        cam_in = s.get("camera") if isinstance(s.get("camera"), dict) else {}
        focus = cam_in.get("focus") if cam_in.get("focus") in [c["id"] for c in chars] else chars[0]["id"]
        cam = {"framing": cam_in.get("framing") if cam_in.get("framing") in core.FRAMINGS else "medium",
               "angle": cam_in.get("angle") if cam_in.get("angle") in core.ANGLES else "eye",
               "move": cam_in.get("move") if cam_in.get("move") in core.MOVES else "static",
               "focus": focus,
               "side": cam_in.get("side") if cam_in.get("side") in core.SIDES else "center"}
        hint = cast_id(s.get("speaker")) if s.get("speaker") else None
        given = s.get("quote_speakers") if isinstance(s.get("quote_speakers"), list) else None
        if given is not None:
            given = [cast_id(g) for g in given]
            if any(g is None for g in given):
                given = None
        tone = s.get("tone") if isinstance(s.get("tone"), str) else ""
        shot = {"_hint": hint, "_given": given, "id": n,
                "title": (s.get("title") if isinstance(s.get("title"), str) and 2 <= len(s["title"]) <= 40 else f"Shot {n}"),
                "look": look, "narration": text, "tone": tone[:80], "visual": str(s.get("visual") or "")[:220],
                "camera": cam, "characters": chars}
        if props:
            shot["props"] = props
        if s.get("thumb"):
            shot["thumb"] = True
        shots.append(shot)

    if language == "English":
        for sh in shots:
            fix_actions(sh, cfg, notes)

    for sh in shots:
        dialogue_choreo(sh, cfg, notes, sh.pop("_hint", None), sh.pop("_given", None))

    cuts = 0
    for sh in shots:                                     # conversations: the camera cuts to whoever speaks
        ids = [x["id"] for x in sh.get("speakers", [])]
        if len(set(ids)) >= 2 and len(ids) >= 2 and cuts < 6 and len(sh["characters"]) >= 2:
            sh["cuts"] = True
            cuts += 1
    # ---- animals must not overlap, walk through each other, or hide each other from the camera
    for sh in shots:
        az = core.az_of(sh["camera"]["side"])
        if not core.fix_collisions(sh["characters"], 6.5, az=az):
            notes.append(f"shot {sh['id']}: animals are very close, check the picture")
        for pr in sh.get("props", []):                   # a boulder is only allowed where an animal stands on it
            on_it = any(math.hypot(c["pos"][0] - pr["pos"][0], c["pos"][1] - pr["pos"][1]) < 0.9 * pr["size"]
                        and not any(core.ACTIONS[b["action"]]["speed"] > 0 for b in c["beats"]) for c in sh["characters"])
            if not on_it:
                far = max(c["pos"][1] for c in sh["characters"])
                pr["pos"][1] = round(min(9.0, far + 1.6 * pr["size"] + 1.0), 2)
                notes.append(f"shot {sh['id']}: boulder moved behind the animals so it hides nobody")
        for c in sh["characters"]:
            c.pop("z", None)
            for pr in sh.get("props", []):
                if pr["type"] == "rock" and math.hypot(c["pos"][0] - pr["pos"][0], c["pos"][1] - pr["pos"][1]) < 0.9 * pr["size"] \
                        and not any(core.ACTIONS[b["action"]]["speed"] > 0 for b in c["beats"]):
                    c["z"] = round(0.7 * pr["size"] * 0.92, 2)

    closeups = 0
    for sh in shots:
        cam = sh["camera"]
        if cam["framing"] in ("close", "extreme_close"):
            closeups += 1
            if closeups > 4:
                cam["framing"] = "medium"
                notes.append(f"shot {sh['id']}: too many close-ups in one story, framing changed to medium")
        fchar = next(c for c in sh["characters"] if c["id"] == cam["focus"])
        if cam["move"] == "follow" and not any(core.ACTIONS[b["action"]]["speed"] > 0 and "to" in b for b in fchar["beats"]):
            cam["move"] = "push_in"
    moving_shots = sum(1 for sh in shots if any(core.ACTIONS[b["action"]]["speed"] > 0 and "to" in b
                                                 for c in sh["characters"] for b in c["beats"]))
    if moving_shots > 10:                                   # calm the last ones down instead of asking Gemini again
        extra_moves = moving_shots - 10
        for sh in reversed(shots):
            if extra_moves <= 0:
                break
            movers = [c for c in sh["characters"] if any(core.ACTIONS[b["action"]]["speed"] > 0 and "to" in b for b in c["beats"])]
            if not movers:
                continue
            speakers = {x["id"] for x in sh.get("speakers", [])}
            for c in movers:
                dest = [b["to"] for b in c["beats"] if b.get("to")][-1]
                c["pos"] = [round(core.clamp(dest[0], -2.4, 2.4), 2), round(core.clamp(dest[1], 0.0, 5.5), 2)]
                c["beats"] = [{"action": "talk" if c["id"] in speakers else "listen"}]
            if sh["camera"]["move"] == "follow":
                sh["camera"]["move"] = "push_in"
            extra_moves -= 1
        notes.append(f"{moving_shots} shots had movement, calmed down to 10")

    looks_used = [sh["look"] for sh in shots]
    if len(set(looks_used)) < 3:
        notes.append("the story uses few different places (looks)")
    if sum(1 for sh in shots if len(sh["characters"]) >= 3) < 3:
        notes.append("few group shots with 3 animals")
    framings = {sh["camera"]["framing"] for sh in shots}
    if len(framings) < 3:                                   # fix the camera variety ourselves
        for i, sh in enumerate(shots):
            if i % 4 == 1:
                sh["camera"]["framing"] = "medium"
            elif i % 4 == 3 and sum(1 for x in shots if x["camera"]["framing"] in ("close", "extreme_close")) < 4:
                sh["camera"]["framing"] = "close"
        notes.append("camera variety added")
    for i in range(2, len(shots)):
        a, b, c = (shots[i - 2]["camera"], shots[i - 1]["camera"], shots[i]["camera"])
        if (a["framing"], a["move"]) == (b["framing"], b["move"]) == (c["framing"], c["move"]):
            c["move"] = "push_in" if c["move"] != "push_in" else "pull_out"
            notes.append(f"shot {i + 1}: three equal camera setups in a row, camera move changed")

    omap = cfg.get("outro") or {}
    outro = omap.get(language, "") if isinstance(omap, dict) else ""
    if outro:
        last = shots[-1]["narration"]
        kws = ("subscribe",) if language == "English" else ("\u0938\u092c\u094d\u0938\u0915\u094d\u0930\u093e\u0907\u092c",)
        if not any(k in last.lower() for k in kws):
            shots[-1]["narration"] = last.rstrip() + " " + outro
            notes.append("subscribe outro added to the last shot")
    # ---- optional closing sentence (only when the config has one)
    dmap = cfg.get("disclaimer") or {}
    disc = dmap.get(language, "") if isinstance(dmap, dict) else ""
    if disc:
        last = shots[-1]["narration"]
        key_words = ("education", "advice") if language == "English" else ("शिक्षा", "सलाह")
        if not all(k in last.lower() for k in key_words):
            shots[-1]["narration"] = last.rstrip() + " " + disc
            notes.append("closing sentence added to the last shot")

    total = sum(count_units(sh["narration"], unit) for sh in shots)
    tlo, thi = tg["total"]
    if strict and not (0.9 * tlo <= total <= 1.2 * thi):
        raise Reject(f"total narration is {total} {unit}, it must be between {tlo} and {thi} "
                     f"({'shorten' if total > thi else 'lengthen'} it)")

    # ---- FLEXIBLE length: the video is as long as the story needs (voice + a little air per shot)
    rate = float(tg.get("rate", 2.4 if unit == "words" else 9.5))        # spoken units per second
    est = total / rate
    target = core.clamp(est + len(shots) * 0.8, float(cfg.get("min_seconds", 105)), float(cfg.get("max_seconds", 178)))
    target = int(round(target))

    thumbs = [i for i, sh in enumerate(shots) if sh.get("thumb")]
    for i in thumbs[1:]:
        del shots[i]["thumb"]
    if not thumbs:
        shots[len(shots) // 2]["thumb"] = True

    title = raw.get("title") if isinstance(raw.get("title"), str) else ""
    if not title.strip():
        raise Reject("the 'title' is missing")
    ep_cfg = cfg.get("_episode")
    suffix = (f" | {cfg.get('series', {}).get('name', '')} Ep {ep_cfg['number']} #shorts" if ep_cfg else cfg.get("title_suffix", ""))
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
    plan = {"version": 4, "language": language, "title": title + suffix,
            "description": str(raw.get("description") or "").strip(), "tags": tags,
            "twist": str(raw.get("twist") or "")[:200], "episode": (ep_cfg or {}).get("number"),
            "viewer_question": str(raw.get("viewer_question") or "")[:200],
            "beat_sheet": {str(k)[:30]: str(v)[:300] for k, v in (raw.get("beat_sheet") or {}).items()} if isinstance(raw.get("beat_sheet"), dict) else {}, "recap": str(raw.get("recap") or "")[:300], "cliffhanger": str(raw.get("cliffhanger") or "")[:300],
            "disclaimer": disc, "credits": cfg.get("credits", ""), "story_mode": cfg.get("story_mode", "adventure"),
            "made_for_kids": bool(cfg.get("made_for_kids", True)),
            "target_seconds": target, "flexible_length": True, "estimated_voice_seconds": round(est, 1),
            "cast": [cast_by_id[i] for i in used_ids], "shots": shots}
    return plan, notes, total


def fix_actions(sh, cfg, notes):
    chars = {c["id"]: c for c in sh["characters"]}

    def has_move(c):
        return any(core.ACTIONS[b["action"]]["speed"] > 0 and "to" in b for b in c["beats"])

    for sent in sentences(sh["narration"]):
        named = [(p, e) for p, e in names_in(sent, cfg["cast"]) if e["id"] in chars]
        for action, rx in VERB_RULES:
            m = re.search(rx, sent, re.I)
            if not m:
                continue
            if action in ("run", "walk", "shock", "sad", "celebrate"):          # figures of speech are not actions
                after = sent[m.end():m.end() + 24].lower()
                prior = sent[max(0, m.start() - 24):m.start()].lower()
                if re.match(r"\s+(a\s+|the\s+)?(shiver|chill|cold|tingle)", after) or re.match(r"\s+(down|through|up|over)\b", after) \
                        or re.search(r"(heart|mind|thoughts?|pulse|blood|eyes|tears?|stomach)\W+(\w+\W+)?$", prior):
                    continue
            before = [e for p, e in named if p < m.start()]
            if not named:
                continue
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
                side = 1 if c["pos"][0] >= anchor[0] else -1
                dest = [round(core.clamp(anchor[0] + side * 1.7, -2.4, 2.4), 2), round(anchor[1], 2)]
                c["pos"] = [round(core.clamp(dest[0] + side * 5.0, -7.0, 7.0), 2), dest[1]]
                c["beats"] = [{"action": action, "to": dest, "at": 0.0}, {"action": "talk" if TALK_RE.search(sent) else "idle"}]
                notes.append(f"shot {sh['id']}: the story says {c['id']} {action}s, movement added")
            elif action not in cur and not has_move(c):
                c["beats"] = [{"action": action}]
                notes.append(f"shot {sh['id']}: the story says {c['id']} is '{action}', action set")
            break


# ------------------------------------------------------------------ who speaks, and when
SPEECH = r"(said|asked|replied|shouted|whispered|growled|snarled|called|cried|explained|declared|told|answered|warned|roared|yelled|pleaded|begged|muttered|sighed|gasped|announced|added|continued|cheered|laughed|barked|howled|bellowed|chuckled)"
QUOTE_RE = re.compile(r'["\u201c](.+?)["\u201d]')


def quote_speakers(text, cast, hint=None, default=None, given=None):
    """[(start_char, end_char, speaker_id)] for every quoted line.
    If the director gave an exact list (one id per quote, same order) it is used. Otherwise the speaker is the animal
    named with a speech verb next to the quote."""
    quotes = [(m.start(), m.end()) for m in QUOTE_RE.finditer(text)]
    cast_ids = {c["id"] for c in cast}
    out = []
    if given and len(given) == len(quotes) and all(g in cast_ids for g in given):
        out = [(st, en, g) for (st, en), g in zip(quotes, given)]
    else:
        verb_end = re.compile(r"\b" + SPEECH + r"\b(\s+\S+){0,3}[\s,:\-]*$", re.I)
        verb_any = re.compile(r"\b" + SPEECH + r"\b", re.I)
        before_full, speakers, gap_used = [], [None] * len(quotes), set()
        prev_end = 0
        for i, (st, en) in enumerate(quotes):
            seg = text[prev_end:st]
            k = max(seg.rfind(". "), seg.rfind("! "), seg.rfind("? "))
            sent = seg[k + 2:] if k >= 0 else seg
            before_full.append(seg)
            m = verb_end.search(sent.rstrip() + " ")
            if m:
                named = [e for p_, e in names_in(sent, cast) if p_ < m.start()]
                if named:
                    speakers[i] = named[-1]["id"]
                    gap_used.add(i)
            prev_end = en
        for i, (st, en) in enumerate(quotes):
            if speakers[i] is not None:
                continue
            nxt = quotes[i + 1][0] if i + 1 < len(quotes) else len(text)
            after = text[en:min(nxt, en + 70)]
            k = min([x for x in (after.find(". "), after.find("! "), after.find("? ")) if x >= 0] or [len(after)])
            after = after[:k]
            if (i + 1) not in gap_used and verb_any.search(after):
                named = names_in(after, cast)
                vm = verb_any.search(after)
                if named and vm:
                    before_v = [e for p_, e in named if p_ < vm.start()]
                    speakers[i] = (before_v[-1] if before_v else named[0][1])["id"]
                    continue
            named = names_in(before_full[i], cast)
            if named:
                speakers[i] = named[-1][1]["id"]
        last = None
        for i, (st, en) in enumerate(quotes):
            spk = speakers[i] or hint or last or default
            last = spk
            out.append((st, en, spk))
    plain = re.compile(r"\b(said|asked|replied|explained|declared|told|answered|warned|announced|whispered|shouted|called|pleaded|begged|added|continued)\b", re.I)
    pos = 0
    for sent in re.split(r"(?<=[.!?])\s+", text):
        st = text.find(sent, pos)
        pos = st + len(sent)
        if not sent or any(q[0] <= st < q[1] or st <= q[0] < pos for q in quotes):
            continue
        m = plain.search(sent)
        if not m:
            continue
        before = [e for p_, e in names_in(sent, cast) if p_ < m.start()]
        if before and len(sent[sent.rfind(before[-1]["name"]):m.start()].split()) <= 4:
            out.append((st, pos, before[-1]["id"]))
    return sorted(out, key=lambda x: x[0])


def _arrival(c, dur=6.5):
    """When (fraction of the shot) does this animal stop walking?"""
    ends = [s["t1"] for s in core.build_schedule(c, dur) if s["moving"]]
    return (max(ends) / dur) if ends else 0.0


def dialogue_choreo(sh, cfg, notes, hint, given=None):
    """Every quoted line is spoken by ONE animal. That animal has action talk exactly while its line is read,
    all others listen. Walkers arrive before the talking starts. The camera looks at the main speaker."""
    text = sh["narration"]
    neutral = {"idle", "listen", "talk"}
    qs = quote_speakers(text, cfg["cast"], hint, sh["camera"]["focus"], given)
    if not qs:
        for c in sh["characters"]:
            if all(b["action"] in neutral for b in c["beats"]):
                c["beats"] = [{"action": "listen"}]
        return
    cast_by_id = {c["id"]: c for c in cfg["cast"]}
    chars = sh["characters"]
    for _, _, spk in qs:
        if spk in cast_by_id and all(c["id"] != spk for c in chars) and len(chars) < 4:
            e = cast_by_id[spk]
            chars.append({"id": spk, "model": e["model"], "pos": free_spot(chars, chars[0]["pos"], 1.6),
                          "beats": [{"action": "listen"}], **({"tint": e["tint"]} if e.get("tint") else {})})
            notes.append(f"shot {sh['id']}: {e['name']} speaks in this shot, added to the picture")
    floor = min(0.55, max([_arrival(c) for c in chars] + [0.0]))
    segs, last = [], 0.0
    for st, en, spk in qs:
        f = core.speech_fraction(text, st)
        frac = 0.0 if f < 0.04 else 0.03 + 0.88 * f
        if floor > 0 and frac < floor + 0.02:
            frac = floor + 0.02
        frac = round(max(frac, last), 2)
        last = frac
        segs.append((frac, spk, en - st))
    for c in chars:
        first = c["beats"][0]
        pre = None
        if first["action"] not in neutral:                     # keep the walk-in or the feeling the director chose
            pre = {"action": first["action"], **({"to": first["to"]} if "to" in first else {})}
        minf = 0.0
        if pre is not None:
            if "to" in pre:
                minf = min(0.55, _arrival({"model": c["model"], "pos": c["pos"], "beats": [pre]}) + 0.02)
            else:
                minf = 0.18
        events = [(0.0, "listen")]
        for frac, spk, _ in segs:
            act = "talk" if spk == c["id"] else "listen"
            if abs(events[-1][0] - frac) < 1e-6:
                events[-1] = (frac, act)
            else:
                events.append((frac, act))
        if pre is not None:
            cur, keep = events[0][1], []
            for fr, act in events:
                if fr <= minf:
                    cur = act
                else:
                    keep.append((fr, act))
            events = [(minf, cur)] + keep
        comp = []
        for fr, act in events:
            if comp and comp[-1][1] == act:
                continue
            comp.append((fr, act))
        allowed = 3 - (1 if pre is not None else 0)
        if len(comp) > allowed:
            comp = comp[:allowed - 1] + [comp[-1]] if allowed >= 2 else [comp[0]]
        new = [dict(pre)] if pre is not None else []
        for fr, act in comp:
            b = {"action": act}
            if fr > 0:
                b["at"] = round(fr, 2)
            new.append(b)
        c["beats"] = new
    weight = {}
    for _, spk, ln in segs:
        weight[spk] = weight.get(spk, 0) + ln
    main = max(weight, key=weight.get)
    if any(c["id"] == main for c in chars):
        sh["camera"]["focus"] = main
    sh["speakers"] = [{"id": spk, "at": fr} for fr, spk, _ in segs]
    sh["quotes"] = [{"start": st, "end": en, "id": spk} for st, en, spk in qs if text[st:st + 1] in ("\"", "\u201c")]


# ------------------------------------------------------------------ prompt
def build_prompt(cfg, topic, genre, history, feedback, story=None):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    series = cfg.get("series", {})
    dmap = cfg.get("disclaimer") or {}
    disc = dmap.get(language, "") if isinstance(dmap, dict) else ""
    cast = "\n".join(f"- id \"{c['id']}\" = {c['name']} (model {c['model']}{', black fur' if c.get('tint') and c['tint'][0] < 1 else (', white fur' if c.get('tint') else '')}): {c['role']}. On screen: {c.get('behaviour', '-')}"
                     for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" + (f" | what happened: {h['recap']}" if h.get("recap") else "") +
                     (f" | ended with: {h['cliffhanger']}" if h.get("cliffhanger") else "") for h in history[-6:]) or "- (this is episode 1)"
    n = cfg.get("shots", 20)
    hero, other = cfg["cast"][0], cfg["cast"][min(1, len(cfg["cast"]) - 1)]
    example = {
        "id": 9, "title": "The Frozen River", "look": "storm_dusk",
        "visual": f"{other['name']} stands tall on a boulder above the roaring river while {hero['name']} looks up, scared.",
        "narration": f"{other['name']} lifted his head. \"The river will not wait for you, {hero['name']}.\"" if language == "English"
        else f"{other['name']} ने सिर उठाया। “नदी तुम्हारा इंतज़ार नहीं करेगी, {hero['name']}.”",
        "tone": "deep, slow, serious",
        "quote_speakers": [other["id"]],
        "camera": {"framing": "medium", "angle": "low", "move": "push_in", "focus": other["id"], "side": "left"},
        "props": [{"type": "rock", "pos": [-1.0, 3.6], "size": 1.4}],
        "characters": [
            {"id": other["id"], "pos": [-1.0, 3.6], "face": hero["id"], "beats": [{"action": "talk"}]},
            {"id": hero["id"], "pos": [1.2, 3.0], "face": other["id"], "beats": [{"action": "sad", "at": 0.5}]}]}
    closing = (f"Shot {n}: a warm closing image, a friendly 'follow for the next adventure' line" + (f" and this sentence: {disc}" if disc else "") + ".")
    full = f"""You are an award-winning animation director and a gripping storyteller for adult learners.
Make a vertical (9:16) YouTube Short of about {int(cfg.get('target_seconds', 150))} seconds (the real length follows the story, a little shorter or longer is fine). It is episode {len(history) + 1} of the series "{series.get('name', '')}": a dramatic, funny and touching ANIMAL ADVENTURE, like a mini movie. The animals live like animals in nature (herd, pack, den, river, drought, storm, hunters, rivals, rescue, migration, lost young ones) and the story secretly carries ONE gentle life lesson (courage, friendship, honesty, teamwork, patience, kindness ...). It must feel like a story people cannot stop watching, never like a lecture.
Audience: families and children in India and the US. Narration language: {language}.
Story idea of today: {topic}
Story style today: {genre}

THE WORLD (series bible, keep it consistent)
{series.get('bible', '')}

STORY SO FAR (do not repeat these; you may refer to them briefly so the series feels alive):
{past}
{('FIX THIS PROBLEM FROM YOUR LAST ANSWER: ' + feedback) if feedback else ''}

MAKE IT GRIPPING
1. First shot: a dramatic hook that raises a question (danger, a secret, a missing friend, a ticking clock). Never start with calm description.
2. A clear hero, a danger or rival with a clear goal, real stakes (what will be lost), a ticking clock ("before the river rises", "before night falls").
3. Open a question early and pay it off late. Use a twist around shots 11-13. Use curiosity lines such as "But nobody knew that ...".
4. Feelings: fear, pride, shock, relief, laughter. Give each animal its own way of speaking (the proud one, the funny one, the wise old one). Short spoken lines in quotes. Simple vivid words, short sentences, a storyteller's rhythm.
5. The lesson appears through events and one wise character's short rule, never through a speech.
6. Structure for {n} shots: 1-2 hook; 3-5 the world, the hero and the problem; 6-8 pressure and temptation; 9-12 the mistake or the danger and the painful consequence; 13-16 the hero learns and applies the lesson, the twist; 17-18 the hero wins in a clever, believable way; 19 the heart of the story in one simple line plus a one-line teaser for the next episode; {closing}
7. Family friendly: no blood, no gore, no real brands. Animals may be scared or hurt (hurt = hit-react), but the tone stays warm.

LENGTH
- Exactly {n} shots. Each shot is ONE visible moment of 4 to 9 seconds told by 1 or 2 short sentences ({tg['shot'][0]} to {tg['shot'][1]} {unit}). The whole narration must be {tg['total'][0]} to {tg['total'][1]} {unit} in total. Count carefully.

CAST (use ONLY these ids; 2 to 5 different animals in the whole story, same animal = same role every time)
{cast}

DIRECTION RULES (the renderer follows them exactly)
- Every verb in the narration that an animal does must be visible. "ran" = run with a destination "to". "walked in" = walk with "to". "said" = that animal's action is talk and the others listen. Surprise = shock. Sadness or worry = sad. Eating = eat. Hit = kick or fight, hurt = hurt.
- Actions allowed: {', '.join(core.ACTIONS)}. Only walk and run move; they REQUIRE "to":[x,y]. Walking is 1.4 m/s and running 5.5 m/s.
- CALM MOVEMENT: at most 10 of the {n} shots may have a walking or running animal. In all other shots the animals stand still (talk, listen, shock, sad ...). No pacing, no wandering.
- Each animal has 1 to 3 "beats" in time order. "at" = when the beat starts as a fraction of the shot (0 = when the narration starts, 0.5 = half way). Start every action when the narration mentions it. An animal that walks in must arrive BEFORE it talks.
- The frame is VERTICAL and narrow. Keep every animal at x from -2.4 to 2.4 and y from 0 to 5.5 (y = distance from the camera). Animals are BIG: a horse is 2.4 m long. Place animals ACROSS the picture (left and right of each other, depth difference at most 1.5 m) so that nobody stands directly behind another animal: the camera must see every animal fully. VARY the number of animals: 1 animal alone for strong emotion (at least 2 shots), 2 for a conversation, 3 for group moments (at least 4 shots), 4 only for a pack or crowd scene. An animal may start off-screen (x up to +-7) only if its first beat is a walk or run to a point inside.
- props: optional boulder {{"type":"rock","pos":[x,y],"size":1.0-2.5}}. Use a boulder ONLY when an animal stands on top of it (same x,y as the animal): good for a wise elder or a leader watching from above.
- WHO SPEAKS: every quoted line must be written between double quotes and must name the speaker right next to it with a speech verb ("said Ragnar", "Milo whispered"). Give each shot "quote_speakers": the ids of the speaker of each quoted line, in the order the lines appear ([] if nobody speaks). Only that animal talks while its line is read; all the others listen. Never let another animal move or talk while a line is spoken. Also give each shot "speaker" = the id of the first speaker (null if nobody) and "tone" = 2-4 words telling the voice actor how to read the shot (for example "whispered, scared", "proud, loud", "warm, gentle").
- "face": the id of the animal this one looks at.
- camera = {{"framing": one of {core.FRAMINGS}, "angle": one of {core.ANGLES}, "move": one of {core.MOVES}, "focus": id of the animal the camera is about, "side": one of {core.SIDES}}}.
  Direct like a film: open with a wide shot; most shots wide or medium (at least 12 of {n}); close or extreme_close only for the strongest emotion (at most 4 shots in the whole story); low angle for powerful animals, high angle for small or scared ones; push_in slowly on dramatic lines, pull_out for the consequence, follow ONLY when the focus animal walks or runs; static otherwise. Never repeat the same framing and move three times in a row.
- look = one of {list(core.LOOKS)}. Meanings: meadow_day (normal day), forest_day, flower_field (happy, spring), misty_morning (calm, mystery), autumn_gold (change, warm), golden_sunset (ending of a day, hope), riverbank_dawn, snow_pines (cold, lonely, hard times), rainy_forest (sadness, trouble), storm_dusk (danger), ember_sky (fiery red sky = big danger), night_moon (secret, fear, quiet). Use AT LEAST 4 different looks in the story, change the look whenever the place or the time changes, and never use one look for more than 8 shots.
- Put "thumb": true on the single most dramatic shot.

SELF-CHECK before you answer: shots = {n}; total {unit} in range; every named animal is in that shot's characters; every walk/run has "to"; at most 10 moving shots; at most 4 close-ups; nobody stands behind another animal; quote_speakers matches the quotes one by one.

Return ONLY JSON:
{{"title": "<curiosity-hook {language} title, max 60 characters: a question or a surprising claim a US adult beginner would click, plain words, include the money or investing idea, honest to the story>", "description": "<2-3 sentence {language} description>", "tags": ["<10 short tags about investing and personal finance for US adults, no kids tags>"], "recap": "<one sentence: what happened in this episode>", "cliffhanger": "<one sentence teaser for the next episode>", "shots": [ ... {n} shots ... ]}}
One shot looks like this:
{json.dumps(example, ensure_ascii=False)}
"""
    if story:
        i, j = full.index("MAKE IT GRIPPING"), full.index("CAST (use ONLY")
        brief = [{"id": s["id"], "narration": s["narration"], "quote_speakers": s["quote_speakers"], "tone": s.get("tone", "")}
                 for s in story["shots"]]
        full = (full[:i] + "THE STORY IS FINISHED AND LOCKED. Do NOT change a single word of the narration. You are only the DIRECTOR now.\n"
                "For every shot copy id, narration (exactly), quote_speakers, tone and speaker, and add look, visual, camera, props, characters, thumb. "
                "Make the picture show what the words say, put the emotional peak shots in close-ups, and use wide shots for places and danger.\n"
                "FINISHED STORY:\n" + json.dumps(brief, ensure_ascii=False) + "\n\n" + full[j:])
    return full


# ------------------------------------------------------------------ pass 1 and 2: the story itself
def pick_episode(cfg, idx):
    """The planned episode of the season for position idx (or None when the season is over / no season is defined)."""
    season = cfg.get("season") or {}
    eps = season.get("episodes") or []
    if idx < 0 or idx >= len(eps):
        return None
    ep = dict(eps[idx])
    ep["number"] = idx + 1
    ep["total"] = len(eps)
    ep["previous_cliffhanger"] = eps[idx - 1]["cliffhanger"] if idx > 0 else ""
    return ep


def build_story_prompt(cfg, topic, genre, history, feedback):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    series = cfg.get("series", {})
    season = cfg.get("season") or {}
    ep = cfg.get("_episode")
    n = cfg.get("shots", 20)
    cast = "\n".join(f"- id \"{c['id']}\" = {c['name']} ({c['model']}): {c['role']}. Speaks: {c.get('voice_style', 'natural')}. Catchphrase: {c.get('catchphrase', '-')}. Behaves: {c.get('behaviour', '-')}"
                     for c in cfg["cast"])
    past = "\n".join(f"- Ep {h.get('episode', '?')}: {h.get('title', '')}" + (f" | happened: {h['recap']}" if h.get("recap") else "") +
                     (f" | ended on: {h['cliffhanger']}" if h.get("cliffhanger") else "") + (f" | clue planted: {h['clue']}" if h.get("clue") else "")
                     for h in history[-4:]) or "- (nothing yet: this is the very first episode)"
    twists = "\n".join(f"- {h['twist']}" for h in history[-12:] if h.get("twist")) or "- (none yet)"
    dmap = cfg.get("disclaimer") or {}
    disc = dmap.get(language, "") if isinstance(dmap, dict) else ""
    omap = cfg.get("outro") or {}
    outro = omap.get(language, "") if isinstance(omap, dict) else ""
    fix = ("FIX THIS PROBLEM FROM YOUR LAST ANSWER: " + feedback) if feedback else ""
    money = cfg.get("story_mode", "adventure") == "money"
    if ep:
        opening = (f"Answer the previous cliffhanger in a surprising way in shots 1-2 (previous episode ended on: {ep['previous_cliffhanger']}). Include ONE short catch-up line so a new viewer understands."
                   if ep["previous_cliffhanger"] else "This is the first episode: shot 1 starts in the middle of the mystery, the vanished food, with no calm introduction.")
        episode_block = (f"SEASON \"{season.get('name', '')}\": {season.get('premise', '')}\n"
                         f"HIDDEN TRUTH (for your planning ONLY, never reveal it before episode {season.get('reveal_episode', 11)}): {season.get('secret', '')}\n"
                         f"THIS IS EPISODE {ep['number']} OF {ep['total']}.\n"
                         f"- Money idea of this episode: {ep['concept']}\n- Story beat you must deliver: {ep['beat']}\n- Clue to plant (visible but innocent): {ep['clue']}\n"
                         f"- The episode MUST END on this cliffhanger: {ep['cliffhanger']}\n- Opening: {opening}")
    else:
        episode_block = (f"Story idea of today: {topic}\nThis is a continuing series. Open by answering the cliffhanger of the last episode listed below in a surprising way (one short catch-up line), "
                         "plant one new clue, and end on a new cliffhanger.")
    lesson = (f"INVESTING AND MONEY LEARNING (the purpose of the series; US audience of adult beginners; it must never feel like school):\n"
              "- The money idea is the KEY of the plot: the problem is caused by ignoring it, and the hero wins by using it. Never explain it in a speech.\n"
              "- Inside the story the animals use animal money (nuts, berries, honey, a hollow-tree storehouse) and small clear numbers (10 nuts, save 3). "
              "Show the wrong way costing something real, then the right way with a clear, CORRECT calculation.\n"
              "- Accuracy matters more than drama. Use only well-established ideas (compound growth, diversification, index funds in general, 401(k) employer match, IRA in general, emergency fund, inflation, fees, risk and market ups and downs, dollar-cost averaging, credit card interest, scams). "
              "Never name real banks, brokers, apps, brands, tickers or specific funds. Never promise or guarantee returns, never say 'buy this', always keep the idea that investing can lose money. Do not give tax or legal specifics beyond general ideas.\n"
              "- Shot 18: the hero uses the idea with the animal numbers. Shot 19: starts with 'Today we learned' and gives exactly three short points in plain words; "
              "at least one point must translate the idea into real US dollars using the real-world example given in the money idea above, said as a clearly hypothetical example ('for example', 'if'), never as a promise.\n") if money else (
              "LESSON: one gentle life lesson, shown by events and one short wise line, never a speech.")
    lang_note = ("Write natural spoken Hindi in Devanagari, short sentences, simple words a child knows, no heavy Sanskrit words. Write the animal names exactly as in the cast list, in Latin letters (Milo, Drakon, Gulab)."
                 if language == "Hindi" else "Write natural spoken English with simple words, short sentences and a storyteller's rhythm.")
    end_extra = ((" Then this exact outro sentence: " + outro) if outro else "") + ((" Then this exact closing sentence: " + disc) if disc else "")
    return f"""You are the head writer of a hit animated mystery series for US adults (ages 20 to 45) who are beginners in personal finance and investing, like a mix of a detective show and a witty cartoon, that teaches real money and investing skills through animal characters. Write ONLY the story (no camera, no positions) of one episode: a vertical YouTube Short of {n} shots, language {language}. {lang_note}
SERIES "{series.get('name', '')}". World: {series.get('bible', '')}
{episode_block}
Style flavour: {genre}

PREVIOUSLY (do not repeat, only build on it):
{past}
Twists already used (invent a DIFFERENT one):
{twists}
{fix}

CAST (use only these ids; each animal speaks in its own style; a catchphrase at most once; 2 to 5 animals in an episode):
{cast}

HOW TO MAKE IT GRIPPING (follow all of these):
1. COLD OPEN: shot 1 drops us into the middle of danger or mystery in the first 3 seconds. No scene setting, no 'once upon a time'.
2. MICRO-HOOKS: end EVERY shot (except 19) on something that pulls forward: an unanswered question, a strange noise, a reveal, a choice, a rising clock. Never end a shot on a calm, finished thought.
3. TICKING CLOCK: state a deadline with a number early ('before sunset', 'three sunrises left') and remind us of it twice.
4. RULE OF THREE: the hero tries and fails twice (each failure costs something real), the third attempt wins using the money idea.
5. DRAMATIC IRONY: in one short narrator line let the viewer see a danger or a clue that the hero does not see yet.
6. CLUE AND RED HERRING: plant the planned clue (visible but innocent, mentioned in one short line) and one false suspect.
7. MIDPOINT TWIST around shots 10-12 that flips what we believed. It must be fair, hinted before.
8. DARKEST MOMENT around shots 14-15: the hero loses something and wants to give up. Then the money idea turns it around.
9. A rival with charm and a logic of his own. The hero's own flaw (impatience, pride) causes part of the trouble.
10. HUMOUR: a specific, visual comic moment every 4-5 shots, usually Bholu or Tinku. Funny, not silly.
11. DIALOGUE: at least 8 shots with a spoken line in double quotes and the speaker named next to it ("said Ragnar"). Lines are short (5-12 words), with subtext, in each animal's own voice. The narrator never explains what a line already shows.
12. SPECIFIC: concrete numbers and objects (7 nuts, 3 days, the cracked acorn), never vague words.
13. NEVER USE: 'little did he know', 'suddenly', 'everything changed', 'and they lived happily ever after', vague morals, a lecture.
14. Keep it clean: no blood, no real brands; tension and humour, not horror.

{lesson}

STRUCTURE OF THE {n} SHOTS:
1-2 cold open and catch-up | 3-5 the problem, the stakes and the clock | 6-8 first attempt fails (the wrong money move) | 9-11 consequences grow, the clue appears | 12 midpoint twist |
13-15 second failure, darkest moment | 16-17 the hero discovers the money idea through action | 18 the hero wins cleverly using it, with numbers |
19 'Today we learned' with three short points | {n} one question for the viewers (a choice about today's money idea, to answer in the comments), then the cliffhanger of the episode as one short mystery line.{end_extra}

LENGTH: exactly {n} shots, each {tg['shot'][0]} to {tg['shot'][1]} {unit}; the whole narration {tg['total'][0]} to {tg['total'][1]} {unit}. The last shot may be longer. Count carefully.

PLAN FIRST: fill in "beat_sheet" before you write the shots, then make the shots follow it exactly.

Return ONLY JSON: {{"beat_sheet": {{"previously": "<how this episode opens by answering the last cliffhanger>", "hook": "<the first-3-seconds hook>", "want": "<what the hero wants>", "obstacle": "<what stands in the way>", "money_in_plot": "<how the money idea causes the problem and then solves it>", "clue": "<the clue planted>", "red_herring": "<the false suspect>", "midpoint_twist": "<the twist>", "darkest_moment": "<the low point>", "cliffhanger": "<the ending hook>"}},
"title": "<curiosity title in {language}, max 55 characters, without the series name or an episode number>", "description": "<2-3 sentences>", "tags": ["<10 short tags about investing and personal finance for US adults, no kids tags>"], "logline": "<one sentence>", "twist": "<the twist in one sentence>", "recap": "<one sentence: what happened>", "cliffhanger": "<the cliffhanger in one sentence>", "viewer_question": "<the question for the comments>",
"shots": [{{"id": 1, "narration": "...", "quote_speakers": ["<id of each quoted line in order>"], "speaker": "<id or null>", "tone": "<2-4 words for the voice actor>"}}, ... {n} shots ...]}}
"""


def check_story(story, cfg, strict=True):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    n = int(cfg.get("shots", 20))
    if not isinstance(story, dict) or not isinstance(story.get("shots"), list):
        raise Reject("the JSON must be an object with a 'shots' list")
    if len(story["shots"]) != n:
        raise Reject(f"you wrote {len(story['shots'])} shots, I need exactly {n}")
    cid = {c["id"].lower(): c["id"] for c in cfg["cast"]}
    cid.update({c["name"].lower(): c["id"] for c in cfg["cast"]})
    total, talking = 0, 0
    for i, s in enumerate(story["shots"], start=1):
        if not isinstance(s, dict) or not str(s.get("narration") or "").strip():
            raise Reject(f"shot {i} has no narration")
        s["id"] = i
        s["narration"] = s["narration"].strip()
        if not lang_ok(s["narration"], language, cfg["cast"]):
            raise Reject(f"shot {i} narration must be written in {language}")
        u = count_units(s["narration"], unit)
        lo, hi = tg["shot"]
        if strict and not (int(lo * 0.5) <= u <= shot_cap(hi, unit, i == n)):
            raise Reject(f"shot {i} narration has {u} {unit}, each shot must have {lo} to {hi} {unit}")
        total += u
        nq = len(QUOTE_RE.findall(s["narration"]))
        raw_q = s.get("quote_speakers") if isinstance(s.get("quote_speakers"), list) else []
        qs = [cid.get(str(x).lower()) for x in raw_q]
        if nq != len(qs) or any(x is None for x in qs):
            qs = []          # tolerant: repair_plan finds the speaker from the text (name next to the speech verb)
        s["quote_speakers"] = qs
        talking += 1 if nq else 0
    tlo, thi = tg["total"]
    if strict and not (0.9 * tlo <= total <= 1.2 * thi):
        raise Reject(f"total narration is {total} {unit}, it must be between {tlo} and {thi}")
    if strict and talking < 3:
        raise Reject(f"only {talking} shots have spoken lines in quotes, I need at least 3: more dialogue, less narrator")
    return story


def polish_story(story, cfg, models, key, state):
    """Pass 2: a strict editor scores the story and rewrites the weak shots."""
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    prompt = f"""You are a ruthless story editor for children's animation. Read this {language} story (JSON).
Score 1-10: hook (first 3 seconds), emotion, humor, surprise of the twist, clarity, ending that echoes the start.
Then REWRITE every weak part so each score is at least 8: sharper hook, funnier or more touching dialogue in each animal's own voice, fewer explaining narrator sentences, a fair twist, a warm looping ending.
KEEP: the same number of shots and ids, each shot {tg['shot'][0]} to {tg['shot'][1]} {tg['unit']}, total {tg['total'][0]} to {tg['total'][1]} {tg['unit']}, spoken lines in double quotes with the speaker named next to them, and "quote_speakers" (one id per quoted line, in order) for every shot, only these ids: {', '.join(c['id'] for c in cfg['cast'])}.
Return ONLY the full improved story as JSON in exactly the same format (title, description, tags, logline, twist, recap, cliffhanger, shots with id, narration, quote_speakers, speaker, tone).

STORY:
{json.dumps(story, ensure_ascii=False)}
"""
    try:
        better = ask_json(prompt, models, key, state, temperature=0.6)
        return check_story(better, cfg, strict=True)
    except (Reject, RuntimeError) as e:
        print("  editor pass skipped:", str(e)[:140])
        return story



def build_unified_prompt(cfg, topic, genre, history, feedback):
    """ONE prompt for the whole job: the story (craft rules, characters, lesson) AND the direction (camera, actions, looks)."""
    story_part = build_story_prompt(cfg, topic, genre, history, feedback)
    head = story_part[:story_part.index("Return ONLY JSON")]
    direct = build_prompt(cfg, topic, genre, history, "", None)
    rules = direct[direct.index("DIRECTION RULES"):direct.index("SELF-CHECK")]
    ret = direct[direct.index("Return ONLY JSON"):]
    n = cfg.get("shots", 20)
    head = head.replace("Write ONLY the story (no camera, no positions) of a vertical YouTube Short", "Write AND direct a vertical YouTube Short")
    return (head + "\nNow the DIRECTION. You are also the director: for every shot you give the narration and also what is seen.\n"
            + rules + f"SELF-CHECK before you answer: shots = {n}; narration {cfg['targets'][cfg.get('language', 'English')]['unit']} in range; at least 8 shots with spoken lines; "
            "quote_speakers matches the quotes one by one; every named animal is in that shot's characters; every walk/run has \"to\"; at most 10 moving shots; "
            "at most 4 close-ups; at least 4 different looks; some shots with 1 animal, some with 3; nobody stands behind another animal.\n\n"
            + ret.replace('{"title":', story_part[story_part.index('{"beat_sheet"'):story_part.index('"title":')] + '"title":', 1)
            .replace('"recap":', '"logline": "<one sentence>", "twist": "<the twist in one sentence>", "viewer_question": "<question for the comments>", "recap":', 1))


# ------------------------------------------------------------------ continuity pass
def critic_pass(plan, cfg, models, key, state):
    brief = [{"id": s["id"], "narration": s["narration"], "focus": s["camera"]["focus"],
              "characters": [{"id": c["id"], "pos": c["pos"], "beats": c["beats"]} for c in s["characters"]]}
             for s in plan["shots"]]
    prompt = f"""You are the continuity checker of an animated story. Compare each shot's NARRATION with what the animals DO.
A shot is WRONG if a quoted line is spoken by one animal (look at the name next to the speech verb) but another animal is the one with action talk, or if the narration says an animal does something (runs, walks in, speaks, is shocked, eats, is sad, celebrates, fights ...) but that animal's beats show something else, or if an animal that acts in the narration is missing, or if an animal walks or moves while another animal's line is spoken.
Actions allowed: {', '.join(core.ACTIONS)}. walk and run need "to":[x,y]. Allowed ids: {', '.join(c['id'] for c in cfg['cast'])}. At most 4 characters per shot, x -2.4..2.4, y 0..5, animals at least 1.3 m apart and never directly behind each other, beats in time order with "at" 0..0.95.
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
    for s in raw["shots"]:                                   # give the exact speakers back to the repair step
        s["quote_speakers"] = [x["id"] for x in s.get("speakers", [])] if s.get("speakers") else []
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
    md = (f"### Director report\n\n**{plan['title']}**\n\n- Plan file: `{path}`\n- Story idea: {topic}\n- Style: {genre}\n"
          f"- Gemini model: {model}\n- Shots: {len(plan['shots'])} | narration: {total} {unit} | estimated voice "
          f"{plan['estimated_voice_seconds']} s | planned video length {plan['target_seconds']} s (flexible)\n\n"
          "| # | Look | Camera | Who does what | Tone | Narration |\n|---|---|---|---|---|---|\n")
    for s in plan["shots"]:
        cam = s["camera"]
        who = "; ".join(f"{c['id']}: " + ">".join(b["action"] for b in c["beats"]) for c in s["characters"])
        if s.get("speakers"):
            who += " | SPEAKS: " + ", ".join(f"{x['id']}@{x['at']}" for x in s["speakers"])
        md += (f"| {s['id']} | {s['look']} | {cam['framing']}/{cam['angle']}/{cam['move']} on {cam['focus']} | "
               f"{who} | {s.get('tone', '')} | {s['narration'].replace('|', '/')} |\n")
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
    ap.add_argument("--calls", choices=["single", "multi"], default="", help="single = ONE Gemini call for story and direction (default), multi = writer, editor, director")
    ap.add_argument("--story-mode", default="", help="money or adventure (default: from the config)")
    ap.add_argument("--models", default="")
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument("--validate-only", default="")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    if a.language:
        cfg["language"] = a.language
    if a.story_mode:
        cfg["story_mode"] = a.story_mode
    a.calls = a.calls or cfg.get("pipeline", "single")
    language = cfg["language"]
    unit = cfg["targets"][language]["unit"]

    seasons = len((cfg.get("season") or {}).get("episodes") or [])
    print(f"story_director.py {VERSION} | story mode: {cfg.get('story_mode', 'adventure')} | season episodes in config: {seasons} | language: {language}", flush=True)
    if cfg.get("story_mode") == "money" and not seasons:
        print("WARNING: animal_studio.json has no 'season'. It is an OLD file. Copy the newest animal_studio.json into the repo.", flush=True)
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
    topics = cfg.get("themes_" + cfg.get("story_mode", "adventure")) or cfg.get("themes") or cfg.get("topics") or []
    season_done = sum(1 for h in prog["history"] if h.get("episode"))
    cfg["_episode"] = None if (a.topic or cfg.get("story_mode") != "money") else pick_episode(cfg, season_done)
    hist = [h for h in prog["history"] if h.get("episode")] if cfg["_episode"] else prog["history"]
    if cfg["_episode"]:
        topic = cfg["_episode"]["concept"]
        print(f"Season episode {cfg['_episode']['number']} of {cfg['_episode']['total']}: {topic}")
    elif a.topic:
        topic = a.topic
    elif prog["next_topic"] < len(topics):
        topic = topics[prog["next_topic"]]
    else:
        topic = "Invent a NEW animal adventure that is different from the earlier stories list"
    genres = cfg.get("genres") or ["a fable"]
    genre = genres[len(prog["history"]) % len(genres)]
    print("Idea:", topic, "| style:", genre, "| language:", language)

    models = pick_models(key, a.models)
    print("Gemini models:", ", ".join(models))
    state = {"dead": set(), "model": ""}
    feedback, dir_feedback, story, plan, notes, total = "", "", None, None, [], 0
    last_raw = None
    for attempt in range(1, a.attempts + 1):
        print(f"Director attempt {attempt}/{a.attempts}", flush=True)
        stage = "story"
        try:
            if a.calls == "single":
                print("  ONE call: story and direction together", flush=True)
                raw = ask_json(build_unified_prompt(cfg, topic, genre, hist, feedback), models, key, state)
                last_raw = raw
                check_story(raw, cfg)
                plan, notes, total = repair_plan(raw, cfg, strict=True)
                break
            if story is None:
                print("  pass 1: writing the story", flush=True)
                story = check_story(ask_json(build_story_prompt(cfg, topic, genre, hist, feedback), models, key, state), cfg)
                print("  pass 2: editing the story", flush=True)
                story = polish_story(story, cfg, models, key, state)
            stage = "direction"
            print("  pass 3: directing the story", flush=True)
            raw = ask_json(build_prompt(cfg, topic, genre, hist, dir_feedback, story), models, key, state)
            if not isinstance(raw, dict) or not isinstance(raw.get("shots"), list) or len(raw["shots"]) != len(story["shots"]):
                raise Reject(f"return exactly {len(story['shots'])} shots, one for every shot of the finished story")
            for rs, ss in zip(raw["shots"], story["shots"]):          # the words are locked
                rs["narration"], rs["quote_speakers"], rs["tone"] = ss["narration"], ss["quote_speakers"], ss.get("tone", "")
                if ss.get("speaker"):
                    rs["speaker"] = ss["speaker"]
            for k in ("title", "description", "tags", "twist", "recap", "cliffhanger"):
                raw[k] = story.get(k, raw.get(k))
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
            print("  rejected:", e, flush=True)
            if a.calls == "single" and last_raw is not None and attempt >= a.attempts - 1:
                try:                                   # last chance: use the story Gemini already wrote, repair it, do not burn more quota
                    check_story(last_raw, cfg, strict=False)
                    plan, notes, total = repair_plan(last_raw, cfg, strict=False)
                    notes.append("accepted by the lenient fallback: " + str(e))
                    print("  lenient fallback used, the story is usable", flush=True)
                    break
                except Reject as e2:
                    print("  lenient fallback also failed:", e2, flush=True)
            if stage == "story" or a.calls == "single":
                feedback, story = str(e), None
            else:
                dir_feedback = str(e)
            plan = None
        except QuotaOut as e:
            print(f"STOP: every Gemini model has used up its daily quota ({str(e)[:120]}). Nothing more can be done today: run again after the quota resets "
                  "(Google resets free-tier daily quotas at midnight Pacific time) or switch on billing for the key.", flush=True)
            sys.exit(75)
        except RuntimeError as e:
            print("  ", e, flush=True)
            if attempt == a.attempts:
                break
            SLEEP(30)
    feedback = feedback or dir_feedback
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
        if not a.topic and (prog["next_topic"] < len(topics) or cfg.get("_episode")):
            prog["next_topic"] += 1
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path,
                                "recap": plan.get("recap", ""), "cliffhanger": plan.get("cliffhanger", ""), "twist": plan.get("twist", ""),
                                "episode": plan.get("episode"), "clue": (plan.get("beat_sheet") or {}).get("clue", "")})
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
