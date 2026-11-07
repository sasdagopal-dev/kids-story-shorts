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
                          **({"face": c["face"]} if c.get("face") in cast_by_id else {}),
                          **({"tint": entry["tint"]} if entry.get("tint") else {})})
        # an animal named in the narration must be in the picture
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
        hint = s.get("speaker") if s.get("speaker") in cast_by_id else None
        shot = {"_hint": hint, "id": n, "title": (s.get("title") if isinstance(s.get("title"), str) and 2 <= len(s["title"]) <= 40 else f"Shot {n}"),
                "look": look, "narration": text, "visual": str(s.get("visual") or "")[:220],
                "camera": cam, "characters": chars}
        if props:
            shot["props"] = props
        if s.get("thumb"):
            shot["thumb"] = True
        shots.append(shot)

    # ---- the verbs of the narration must be visible (English only)
    if language == "English":
        for sh in shots:
            fix_actions(sh, cfg, notes)

    for sh in shots:
        dialogue_choreo(sh, cfg, notes, sh.get("_hint"))
        sh.pop("_hint", None)

    # ---- animals must not overlap or walk through each other (real body sizes, depth first)
    for sh in shots:
        if not core.fix_collisions(sh["characters"], 6.5):
            notes.append(f"shot {sh['id']}: animals are very close, check the picture")
        for c in sh["characters"]:                       # standing on a rock?
            c.pop("z", None)
            for pr in sh.get("props", []):
                if pr["type"] == "rock" and math.hypot(c["pos"][0] - pr["pos"][0], c["pos"][1] - pr["pos"][1]) < 0.9 * pr["size"] \
                        and not any(core.ACTIONS[b["action"]]["speed"] > 0 for b in c["beats"]):
                    c["z"] = round(0.7 * pr["size"] * 0.92, 2)

    # ---- calm direction: few close-ups, "follow" only when the focus animal really moves
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
    if strict and moving_shots > 9:
        raise Reject(f"{moving_shots} shots have walking or running animals. Too much movement: at most 8 shots may move, "
                     f"in all other shots the animals stand still and the story is told by the narration and the camera")

    # ---- camera variety
    framings = {sh["camera"]["framing"] for sh in shots}
    if strict and len(framings) < 3:
        raise Reject("use more camera variety: at least 3 different framings (close, medium, wide)")
    for i in range(2, len(shots)):
        a, b, c = (shots[i - 2]["camera"], shots[i - 1]["camera"], shots[i]["camera"])
        if (a["framing"], a["move"]) == (b["framing"], b["move"]) == (c["framing"], c["move"]):
            c["move"] = "push_in" if c["move"] != "push_in" else "pull_out"
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
            "recap": str(raw.get("recap") or "")[:300], "cliffhanger": str(raw.get("cliffhanger") or "")[:300],
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



# ------------------------------------------------------------------ who speaks, and when
SPEECH = r"(said|asked|replied|shouted|whispered|growled|snarled|called|cried|explained|declared|told|answered|warned|roared|yelled|pleaded|begged|muttered|sighed|gasped|announced|added|continued|cheered|laughed|barked)"
QUOTE_RE = re.compile(r'["\u201c](.+?)["\u201d]')


def quote_speakers(text, cast, hint=None, default=None):
    """[(start_char, end_char, speaker_id)] for every quoted line. The speaker is the animal named with a
    speech verb next to the quote: 'Drakon snarled, "..."', '"..." said Drakon', '"..." Ragnar growled'."""
    quotes = [(m.start(), m.end()) for m in QUOTE_RE.finditer(text)]
    verb_end = re.compile(r"\b" + SPEECH + r"\b(\s+\S+){0,3}[\s,:\-]*$", re.I)     # 'told Milo,' is allowed
    verb_any = re.compile(r"\b" + SPEECH + r"\b", re.I)
    before_txt, before_full, speakers, gap_used = [], [], [None] * len(quotes), set()
    prev_end = 0
    for i, (st, en) in enumerate(quotes):
        seg = text[prev_end:st]
        k = max(seg.rfind(". "), seg.rfind("! "), seg.rfind("? "))
        sent = seg[k + 2:] if k >= 0 else seg
        before_txt.append(sent)
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
        if (i + 1) not in gap_used and (verb_any.search(after) or False):
            named = names_in(after, cast)
            vm = verb_any.search(after)
            if named and vm:
                before_v = [e for p_, e in named if p_ < vm.start()]
                speakers[i] = (before_v[-1] if before_v else named[0][1])["id"]
                continue
        named = names_in(before_full[i], cast)              # e.g. 'Milo gasped. "Who are you?"'
        if named:
            speakers[i] = named[-1][1]["id"]
    last = None
    out = []
    for i, (st, en) in enumerate(quotes):
        spk = speakers[i] or hint or last or default
        last = spk
        out.append((st, en, spk))
    # reported speech without quotes: 'Ragnar explained the rule: ...', 'Dev told them to save'
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


def dialogue_choreo(sh, cfg, notes, hint):
    """Every quoted line is spoken by the animal that the story names. That animal gets action talk exactly
    while its line is read, the others listen, and the camera looks at the main speaker."""
    text = sh["narration"]
    qs = quote_speakers(text, cfg["cast"], hint, sh["camera"]["focus"])
    if not qs:                                  # nobody speaks: only the narrator, so nobody talks on screen
        for c in sh["characters"]:
            if all(b["action"] in ("idle", "listen", "talk") for b in c["beats"]):
                c["beats"] = [{"action": "listen"}]
        return
    total = max(1, len(text))
    segs = []
    for st, en, spk in qs:
        frac = 0.0 if st / total < 0.04 else 0.03 + 0.88 * st / total
        segs.append((frac, spk, en - st))
    cast_by_id = {c["id"]: c for c in cfg["cast"]}
    chars = sh["characters"]
    for _, spk, _ in segs:
        if spk in cast_by_id and all(c["id"] != spk for c in chars) and len(chars) < 4:
            e = cast_by_id[spk]
            chars.append({"id": spk, "model": e["model"], "pos": free_spot(chars, chars[0]["pos"], 1.6),
                          "beats": [{"action": "listen"}], **({"tint": e["tint"]} if e.get("tint") else {})})
            notes.append(f"shot {sh['id']}: {e['name']} speaks in this shot, added to the picture")
    neutral = {"idle", "listen", "talk"}
    for c in chars:
        if any(b["action"] not in neutral for b in c["beats"]):
            continue                                        # keep movement and feelings the director chose
        events = [(0.0, "listen")]
        for frac, spk, _ in segs:
            act = "talk" if spk == c["id"] else "listen"
            if events and abs(events[-1][0] - frac) < 1e-6:
                events[-1] = (frac, act)
            else:
                events.append((frac, act))
        comp = []
        for fr, act in events:
            if comp and comp[-1][1] == act:
                continue
            comp.append((fr, act))
        if len(comp) > 3:
            comp = [comp[0], comp[1], comp[-1]]
        new = [{"action": act, **({"at": round(fr, 2)} if fr > 0 else {})} for fr, act in comp]
        if [b["action"] for b in new] != [b["action"] for b in c["beats"]]:
            c["beats"] = new
    weight = {}
    for _, spk, ln in segs:
        weight[spk] = weight.get(spk, 0) + ln
    main = max(weight, key=weight.get)
    if any(c["id"] == main for c in chars):
        sh["camera"]["focus"] = main
    sh["speakers"] = [{"id": spk, "at": round(fr, 2)} for fr, spk, _ in segs]

# ------------------------------------------------------------------ prompt
def build_prompt(cfg, topic, genre, history, feedback):
    language = cfg.get("language", "English")
    tg = cfg["targets"][language]
    unit = tg["unit"]
    series = cfg.get("series", {})
    cast = "\n".join(f"- id \"{c['id']}\" = {c['name']} (model {c['model']}{', black fur' if c.get('tint') and c['tint'][0] < 1 else (', white fur' if c.get('tint') else '')}): {c['role']}"
                     for c in cfg["cast"])
    past = "\n".join(f"- {h.get('title', '')}" + (f" | what happened: {h['recap']}" if h.get("recap") else "") +
                     (f" | ended with: {h['cliffhanger']}" if h.get("cliffhanger") else "") for h in history[-6:]) or "- (this is episode 1)"
    n = cfg.get("shots", 20)
    example = {
        "id": 9, "title": "The Fang Den", "look": "ember_sky",
        "visual": "Drakon smiles coldly in front of the Fang Lending Den while Milo stands small and worried below him.",
        "narration": "Drakon smiled coldly. \"One hundred coins today, Milo, and two hundred by the next full moon.\"" if language == "English"
        else "ड्रैकन ठंडी मुस्कान के साथ बोला, आज सौ सिक्के, और अगले पूर्णिमा तक दो सौ।",
        "camera": {"framing": "medium", "angle": "low", "move": "push_in", "focus": "drakon", "side": "left"},
        "props": [{"type": "rock", "pos": [-1.0, 3.6], "size": 1.4}],
        "characters": [
            {"id": "drakon", "pos": [-1.0, 3.6], "face": "milo", "beats": [{"action": "talk"}]},
            {"id": "milo", "pos": [1.0, 1.6], "face": "drakon", "beats": [{"action": "sad", "at": 0.5}]}]}
    return f"""You are an award-winning animation director and a gripping storyteller who also teaches money awareness.
Make a {cfg.get('target_seconds', 150)}-second vertical (9:16) YouTube Short. It is episode {len(history) + 1} of the series "{series.get('name', '')}": a dramatic ANIMAL JUNGLE story, like a mini movie, that secretly teaches ONE money lesson. It must feel like a story people cannot stop watching, never like a lecture.
Audience: families and first-time earners in India. Narration language: {language}.
Lesson of today: {topic}
Story style today: {genre}

THE WORLD (series bible, keep it consistent)
{series.get('bible', '')}

STORY SO FAR (do not repeat these; you may refer to them briefly so the series feels alive):
{past}
{('FIX THIS PROBLEM FROM YOUR LAST ANSWER: ' + feedback) if feedback else ''}

MAKE IT GRIPPING
1. First shot: a dramatic hook that raises a question (danger, a secret, a threat, a ticking clock). Never start with calm description.
2. A clear hero (usually Milo or a friend), a villain or danger with a clear goal, real stakes (what will be lost), a ticking clock (e.g. "by the next full moon").
3. Open a question early and pay it off late. Use a twist around shots 11-13: something the hero did not expect. Use curiosity lines such as "But Milo did not know that ...".
4. Feelings: fear, pride, shock, relief. Short spoken lines in quotes. Simple vivid words, short sentences, a storyteller's rhythm.
5. The lesson appears through events and one wise character's short rule with small rupee numbers (Rs 100, Rs 20) the viewer can follow. Only shot 19 is a three-point recap, in plain words.
6. Shot 19 also gives a one-line teaser for tomorrow. Shot 20: friendly "subscribe for the next episode" and the education-only disclaimer.
7. Structure for {n} shots: 1-2 hook; 3-5 the world, hero and the problem; 6-8 pressure and temptation; 9-12 the mistake and the painful consequence; 13-16 the wise rule applied with numbers, the twist; 17-18 the hero wins in a clever, believable way; 19 recap and teaser; 20 disclaimer and subscribe.
8. Teach only today's lesson. Never name real banks, apps, brands, stocks or funds. Never promise returns. Never tell people to buy anything.

LENGTH (the voice must fill 2.5 minutes)
- Exactly {n} shots. Each shot is ONE visible moment of 4 to 9 seconds told by 1 or 2 short sentences ({tg['shot'][0]} to {tg['shot'][1]} {unit}). The whole narration must be {tg['total'][0]} to {tg['total'][1]} {unit} in total. Count carefully.

CAST (use ONLY these ids; 2 to 5 different animals in the whole story, same animal = same role every time)
{cast}

DIRECTION RULES (the renderer follows them exactly)
- Every verb in the narration that an animal does must be visible. "ran" = run with a destination "to". "walked in" = walk with "to". "said" = that animal's action is talk and the others listen. Surprise = shock. Sadness or worry = sad. Eating = eat. Hit = kick or fight, hurt = hurt.
- Actions allowed: {', '.join(core.ACTIONS)}. Only walk and run move; they REQUIRE "to":[x,y]. Walking is 1.4 m/s and running 5.5 m/s.
- CALM MOVEMENT: at most 8 of the {n} shots may have a walking or running animal. In all other shots the animals stand still (talk, listen, shock, sad ...). No pacing, no wandering, no movement without a story reason.
- Each animal has 1 to 3 "beats" in time order. "at" = when the beat starts as a fraction of the shot (0 = when the narration starts, 0.5 = half way). Start every action when the narration mentions it.
- The frame is VERTICAL and narrow. Keep every animal at x from -2.4 to 2.4 and y from 0 to 5.5 (y = distance from the camera). Animals are BIG: a horse is 2.4 m long. Never place two animals closer than their body lengths: put them diagonally (one nearer the camera, one farther, y at least 2 m apart) instead of side by side. At most 3 animals in a shot (4 only for a wolf pack scene). An animal may start off-screen (x up to +-7) only if its first beat is a walk or run to a point inside.
- props: optional boulder {{"type":"rock","pos":[x,y],"size":1.0-2.5}}. An animal standing at the same spot stands on top of it (good for Ragnar, Drakon or a legend watching from above).
- WHO SPEAKS: every quoted line must say who speaks right next to it, with a speech verb ("said Drakon", "Milo whispered"). Only that animal talks while the line is read; the others listen. Give each shot "speaker": the id of the animal that speaks the first quoted line (null if nobody speaks). Never let a different animal talk while Milo (or anyone else) is the one speaking.
- "face": the id of the animal this one looks at.
- camera = {{"framing": one of {core.FRAMINGS}, "angle": one of {core.ANGLES}, "move": one of {core.MOVES}, "focus": id of the animal the camera is about, "side": one of {core.SIDES}}}.
  Direct like a film: open with a wide shot; most shots wide or medium (at least 12 of {n}); close or extreme_close only for the strongest emotion (at most 4 shots in the whole story); low angle for powerful animals (Drakon, Ragnar), high angle for small or scared ones (Milo); push_in slowly on dramatic lines, pull_out for the consequence, follow ONLY when the focus animal walks or runs; static otherwise. Never repeat the same framing and move three times in a row.
- look = one of {list(core.LOOKS)}. Change the look only when the story moves to a new place or time (forest_day, golden_sunset, riverbank_dawn, storm_dusk, night_moon, ember_sky = fiery red sky for danger, meadow_day).
- Put "thumb": true on the single most dramatic shot.

SELF-CHECK before you answer: shots = {n}; total {unit} in range; every named animal is in that shot's characters; every walk/run has "to"; at most 8 moving shots; at most 4 close-ups; animals far enough apart; last shot has the disclaimer idea ({cfg['disclaimer'][language]}).

Return ONLY JSON:
{{"title": "<gripping honest {language} title, max 60 characters>", "description": "<2-3 sentence {language} description>", "tags": ["<10 short tags>"], "recap": "<one sentence: what happened in this episode>", "cliffhanger": "<one sentence teaser for the next episode>", "shots": [ ... {n} shots ... ]}}
One shot looks like this:
{json.dumps(example, ensure_ascii=False)}
"""


# ------------------------------------------------------------------ continuity pass
def critic_pass(plan, cfg, models, key, state):
    brief = [{"id": s["id"], "narration": s["narration"], "focus": s["camera"]["focus"],
              "characters": [{"id": c["id"], "pos": c["pos"], "beats": c["beats"]} for c in s["characters"]]}
             for s in plan["shots"]]
    prompt = f"""You are the continuity checker of an animated story. Compare each shot's NARRATION with what the animals DO.
A shot is WRONG if a quoted line is spoken by one animal (look at the name next to the speech verb) but another animal is the one with action talk, or if the narration says an animal does something (runs, walks in, speaks, is shocked, eats, is sad, celebrates, fights ...) but that animal's beats show something else, if a speaking animal is not the one with action talk, or if an animal that acts in the narration is missing.
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
        if s.get("speakers"):
            who += " | SPEAKS: " + ", ".join(f"{x['id']}@{x['at']}" for x in s["speakers"])
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
        prog["history"].append({"date": date, "title": plan["title"], "topic": topic, "file": path,
                                    "recap": plan.get("recap", ""), "cliffhanger": plan.get("cliffhanger", "")})
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
