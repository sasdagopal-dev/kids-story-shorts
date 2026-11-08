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
    if strict and moving_shots > 10:
        raise Reject(f"{moving_shots} shots have walking or running animals. Too much movement: at most 10 shots may move, "
                     f"in all other shots the animals stand still and the story is told by the narration and the camera")

    if strict:
        looks_used = [sh["look"] for sh in shots]
        if len(set(looks_used)) < 3 or max(looks_used.count(x) for x in set(looks_used)) > 10:
            raise Reject("use more variety of places and times: at least 4 different looks, none for more than 8 shots")
        if sum(1 for sh in shots if len(sh["characters"]) >= 3) < 3:
            raise Reject("show more group moments: at least 3 shots with 3 animals in the picture (and some shots with 1 animal alone for emotion)")
    framings = {sh["camera"]["framing"] for sh in shots}
    if strict and len(framings) < 3:
        raise Reject("use more camera variety: at least 3 different framings (close, medium, wide)")
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
    if strict and not (tlo <= total <= thi):
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
    plan = {"version": 4, "language": language, "title": title + suffix,
            "description": str(raw.get("description") or "").strip(), "tags": tags,
            "twist": str(raw.get("twist") or "")[:200], "recap": str(raw.get("recap") or "")[:300], "cliffhanger": str(raw.get("cliffhanger") or "")[:300],
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
    full = f"""You are an award-winning animation director and a gripping storyteller for families.
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
{{"title": "<gripping honest {language} title, max 60 characters>", "description": "<2-3 sentence {language} description>", "tags": ["<10 short tags>"], "recap": "<one sentence: what happened in this episode>", "cliffhanger": "<one sentence teaser for the next episode>", "shots": [ ... {n} shots ... ]}}
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
def build_story_prompt(cfg, topic, genre, history, feedback):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    series = cfg.get("series", {})
    n = cfg.get("shots", 20)
    cast = "\n".join(f"- id \"{c['id']}\" = {c['name']} ({c['model']}): {c['role']}. Speaks: {c.get('voice_style', 'natural')}. Catchphrase: {c.get('catchphrase', '-')}. Behaves: {c.get('behaviour', '-')}"
                     for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" + (f" | {h['recap']}" if h.get("recap") else "") for h in history[-6:]) or "- (episode 1)"
    twists = "\n".join(f"- {h['twist']}" for h in history[-12:] if h.get("twist")) or "- (none yet)"
    dmap = cfg.get("disclaimer") or {}
    disc = dmap.get(language, "") if isinstance(dmap, dict) else ""
    fix = ("FIX THIS PROBLEM FROM YOUR LAST ANSWER: " + feedback) if feedback else ""
    if cfg.get("story_mode", "adventure") == "money":
        lesson_rule = ("7. MONEY LESSON (the hidden purpose): teach ONE beginner money-awareness idea, today: " + topic + ". Show it with ANIMAL-NATIVE money, never with human shops or banks: "
                       "food stored for winter = savings, a berry bank in a hollow tree, a hive that grows = compound growth, a lender wolf who asks 'two nuts back for one' = interest and debt trap, "
                       "a trickster selling 'free gold leaves' = scam, a drought or flood = emergency fund, a seed that becomes a tree = patience and long-term growth, three baskets = needs, wants, savings. "
                       "Use small numbers a child can follow (Rs 10, Rs 20, 5 nuts). One wise animal says the rule in one short sentence. Never name real banks, apps, brands, stocks or funds, "
                       "never promise returns, never tell anyone to buy anything.")
        heart = "a plain recap of the money lesson in simple words, starting with 'Today we learned' (in the story language), as three short points"
    else:
        lesson_rule = "7. LESSON: one gentle life lesson, shown by events and one short wise line, never a speech."
        heart = "the lesson of the story in one simple line, starting with 'Today we learned' (in the story language)"
    omap = cfg.get("outro") or {}
    outro = omap.get(language, "") if isinstance(omap, dict) else ""
    end_extra = ((" and this exact outro sentence: " + outro) if outro else "") + ((" and this exact closing sentence: " + disc) if disc else "")
    return f"""You are a master children's-film writer. Write ONLY the story (no camera, no positions) of a vertical YouTube Short, {n} shots, language {language}.
Series "{series.get('name', '')}". World: {series.get('bible', '')}
Story idea: {topic}
Style: {genre}
Earlier episodes (do not repeat):
{past}
Twists already used (invent a DIFFERENT one):
{twists}
{fix}

CAST (use only these ids; each animal speaks in its own style and may use its catchphrase at most once):
{cast}

CRAFT RULES
1. HOOK: shot 1 must raise a question in the first 3 seconds (danger, secret, missing friend, ticking clock). No calm description.
2. WANT AND OBSTACLE: the hero wants something specific, something stands in the way, and the stakes are clear and emotional.
3. OPEN LOOPS: every 4 shots, open a new small question or raise the danger, so nobody swipes away. Pay them off later.
4. TWIST around shots 11-13 that the viewer does not expect but that is fair (hinted earlier).
5. DIALOGUE: at least 8 of the {n} shots contain a spoken line in double quotes with the speaker named next to it ("said Ragnar"). Short, funny or touching lines in each animal's own voice. Do not let a narrator explain what a line already shows.
6. EMOTION: show fear, pride, shock, laughter, relief through actions and short lines, not by naming the feeling.
{lesson_rule}
8. ENDING: shot {n - 1} = {heart} + a one-line teaser. Shot {n} = a warm, funny or surprising last line that echoes shot 1 (so the video loops){end_extra}, {'(the last shot may be a little longer than the others because of these sentences)' if end_extra else ''}.
9. Simple vivid words, short sentences, storyteller rhythm. Family friendly, no blood, no real brands.
10. LENGTH: exactly {n} shots, each {tg['shot'][0]} to {tg['shot'][1]} {unit}; the whole narration {tg['total'][0]} to {tg['total'][1]} {unit}. Count carefully.

GOOD STYLE (only the style, do not copy):
"Tiny Milo stared at the dry riverbed. Behind him, the whole herd was already leaving." / Ragnar did not even turn his head. "Water does not run away, little fox," said Ragnar. "It hides. Like courage." / "Then I will find it," whispered Milo, and his voice shook.

Return ONLY JSON: {{"title": "<gripping honest {language} title, max 60 characters>", "description": "<2-3 sentences>", "tags": ["<10 short tags>"], "logline": "<one sentence>", "twist": "<the twist in one sentence>", "recap": "<one sentence: what happened>", "cliffhanger": "<one sentence teaser>",
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
        if lang_ratio(s["narration"], language) < 0.75:
            raise Reject(f"shot {i} narration must be written in {language}")
        u = count_units(s["narration"], unit)
        lo, hi = tg["shot"]
        extra = (14 if unit == "words" else 90) if i == n else 4 if unit == "words" else 20
        if strict and not (lo - 2 <= u <= hi + extra):
            raise Reject(f"shot {i} narration has {u} {unit}, each shot must have {lo} to {hi} {unit}")
        total += u
        nq = len(QUOTE_RE.findall(s["narration"]))
        raw_q = s.get("quote_speakers") if isinstance(s.get("quote_speakers"), list) else []
        qs = [cid.get(str(x).lower()) for x in raw_q]
        if nq != len(qs) or any(x is None for x in qs):
            raise Reject(f"shot {i}: it has {nq} quoted line(s) but quote_speakers lists {len(qs)} valid id(s); list the speaker id of every quoted line, in order")
        s["quote_speakers"] = qs
        talking += 1 if nq else 0
    tlo, thi = tg["total"]
    if strict and not (tlo <= total <= thi):
        raise Reject(f"total narration is {total} {unit}, it must be between {tlo} and {thi}")
    if strict and talking < 6:
        raise Reject(f"only {talking} shots have spoken lines in quotes, I need at least 8: more dialogue, less narrator")
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
    topics = cfg.get("themes_" + cfg.get("story_mode", "adventure")) or cfg.get("themes") or cfg.get("topics") or []
    if a.topic:
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
    for attempt in range(1, a.attempts + 1):
        print(f"Director attempt {attempt}/{a.attempts}", flush=True)
        stage = "story"
        try:
            if story is None:
                print("  pass 1: writing the story", flush=True)
                story = check_story(ask_json(build_story_prompt(cfg, topic, genre, prog["history"], feedback), models, key, state), cfg)
                print("  pass 2: editing the story", flush=True)
                story = polish_story(story, cfg, models, key, state)
            stage = "direction"
            print("  pass 3: directing the story", flush=True)
            raw = ask_json(build_prompt(cfg, topic, genre, prog["history"], dir_feedback, story), models, key, state)
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
            if stage == "story":
                feedback, story = str(e), None
            else:
                dir_feedback = str(e)
            plan = None
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
        if not a.topic and prog["next_topic"] < len(topics):
            prog["next_topic"] += 1
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path,
                                "recap": plan.get("recap", ""), "cliffhanger": plan.get("cliffhanger", ""), "twist": plan.get("twist", "")})
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
