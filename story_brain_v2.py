#!/usr/bin/env python3
"""
STORY BRAIN v2  (money basics + fintech safety, 3D animated episodes)

  GEMINI_API_KEY=... python3 story_brain_v2.py --config channel_v2.json

What it does
  1. Picks today's topic from channel_v2.json (or --topic "..."; or --story file.txt).
  2. Reads your asset packs: which characters and animations really exist.
  3. Asks Gemini to write the episode as a SHOT LIST (narration, characters, action, shot type, look, card, prop).
  4. Checks everything:
       - real characters and real animations only (fixes small mistakes itself)
       - characters named in the narration are really IN that shot (picture matches the words)
       - Safe-Finance rules (no tips, no promised returns, no real brand names)
       - total length under 170 seconds
     and asks Gemini again only when something big is wrong.
  5. Turns each shot type into exact positions and camera moves and writes the scene plan.

Gemini never writes coordinates, so it cannot break the 3D scene.
Test without internet:  --mock-response answer.json
"""
import argparse
import datetime
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

DEFAULT_MODELS = "gemini-flash-latest,gemini-2.5-flash,gemini-2.5-flash-lite"

# ------------------------------------------------------------------ what the 3D engine can show
ENVS = {
    "meadow_day": "sunny farm meadow with grass and flowers: village life, farming, happy or hopeful mood",
    "town_day": "town street with buildings: market, bank, shops, phone payments, city life",
    "forest_day": "bright sunny forest: peaceful or adventurous mood",
    "forest_sunset": "golden sunset: calm, emotional, thoughtful or lesson-learned mood",
    "fire_red": "red burning sky: danger, scam, fraud, crash, disaster, panic",
    "winter_night": "snowy blue night: hardship, poverty, debt trouble, fear or sadness",
}
SHOT_TYPES = {
    "establishing": (1, 5, "wide calm view of 1-5 characters standing, eating or resting"),
    "empty_pan": (0, 0, "scenery only, no characters (time passing, mood, weather)"),
    "follow": (1, 4, "1-4 characters walking/running left to right, the camera follows the first"),
    "chase": (2, 5, "the FIRST character runs away, the OTHERS chase it"),
    "two_shot": (2, 3, "2-3 characters facing each other (talking, arguing); the first stands on the left"),
    "close_up": (1, 2, "1-2 characters facing the camera, for emotion or a short speech"),
    "hero_rock": (1, 1, "ONE powerful character standing on a rock, seen from a low angle"),
    "group": (3, 6, "3-6 characters standing together or apart"),
    "fight": (2, 4, "2-4 characters fighting (use attack actions)"),
}
PROPS = {
    "coins": "stacks of gold coins that GROW during the shot (saving, earning, wealth building)",
    "growing_tree": "a small tree that GROWS big during the shot (patience, compounding, long-term growth)",
}
PROP_OK_TYPES = ("establishing", "group", "two_shot", "close_up")

TINTS = {"dark": [0.15, 0.14, 0.16], "white": [2.3, 2.3, 2.5], "grey": [0.55, 0.55, 0.60]}
SIZES = {"Deer": 2.0, "Stag": 2.3, "Wolf": 1.9, "Fox": 1.1, "ShibaInu": 1.1, "Husky": 1.3, "Alpaca": 1.6,
         "Donkey": 1.8, "Cow": 2.4, "Bull": 2.5, "Horse": 2.5, "Horse_White": 2.6}
HUMAN_HEIGHT = {"King": 1.85, "Farmer": 1.75}

# Safe-Finance rules: unregistered people must not give investment advice, tips or performance claims
BANNED_PATTERNS = [
    r"guarantee", r"sure[- ]?shot", r"risk[- ]?free", r"double (?:your|the|my) money", r"get rich quick",
    r"multibagger", r"target price", r"stock tips?", r"hot tips?", r"insider", r"100\s*% (?:safe|secure|sure)",
    r"\b(?:buy|sell|invest in) (?:this|these|that|the|our) (?:stock|share|fund|coin|crypto|scheme)s?\b",
    r"will (?:give|earn|make|pay) you \d+\s*%", r"\d+\s*% (?:returns? )?(?:every|per|each) (?:month|week|day)",
    r"you should (?:buy|sell|invest in)",
]
BRAND_WORDS = ["reliance", "tcs", "infosys", "hdfc", "icici", "sbi", "adani", "zerodha", "groww", "upstox",
               "paytm", "phonepe", "gpay", "google pay", "bitcoin", "ethereum", "dogecoin", "nifty", "sensex",
               "paisabazaar", "bajaj", "kotak", "axis bank", "lic of india", "bhim", "paypal", "mastercard",
               "rupay", "whatsapp", "telegram", "binance", "wazirx", "coindcx", "razorpay", "amazon", "flipkart"]

# used when the asset packs are not found
_SMALL = ["Attack", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2", "Idle_2_HeadLow",
          "Idle_HitReact1", "Idle_HitReact2", "Jump_ToIdle", "Walk"]
_BIG = ["Attack_Headbutt", "Attack_Kick", "Death", "Eating", "Gallop", "Gallop_Jump", "Idle", "Idle_2",
        "Idle_Headlow", "Idle_HitReact1", "Idle_HitReact2", "Jump_toIdle", "Walk"]
_HUMAN = ["Death", "Gun_Shoot", "HitRecieve", "HitRecieve_2", "Idle", "Idle_Gun", "Idle_Gun_Pointing",
          "Idle_Gun_Shoot", "Idle_Neutral", "Idle_Sword", "Interact", "Kick_Left", "Kick_Right", "Punch_Left",
          "Punch_Right", "Roll", "Run", "Run_Back", "Run_Left", "Run_Right", "Run_Shoot", "Sword_Slash",
          "Walk", "Wave"]
BUILTIN = {}
for _n in ("Wolf", "Fox", "Husky", "ShibaInu"):
    BUILTIN[_n] = {"kind": "animal", "animations": _SMALL}
for _n in ("Alpaca", "Bull", "Cow", "Deer", "Donkey", "Horse", "Horse_White", "Stag"):
    BUILTIN[_n] = {"kind": "animal", "animations": _BIG}
for _n in ("Adventurer", "Beach", "Casual_2", "Casual_Hoodie", "Farmer", "King", "Punk", "Spacesuit",
           "Suit", "Swat", "Worker"):
    BUILTIN[_n] = {"kind": "human", "animations": _HUMAN}

SYNONYMS = {
    "lion": "Wolf", "tiger": "Wolf", "bear": "Wolf", "leopard": "Wolf", "jackal": "Wolf", "hyena": "Wolf",
    "dog": "ShibaInu", "puppy": "ShibaInu", "cat": "Fox", "rabbit": "Fox", "hare": "Fox", "squirrel": "Fox",
    "goat": "Alpaca", "sheep": "Alpaca", "camel": "Alpaca", "pig": "Cow", "buffalo": "Bull", "ox": "Bull",
    "elephant": "Bull", "pony": "Horse", "zebra": "Horse", "reindeer": "Stag", "antelope": "Deer",
    "fawn": "Deer", "man": "Farmer", "boy": "Casual_Hoodie", "villager": "Farmer", "father": "Farmer",
    "soldier": "Swat", "guard": "Swat", "police": "Swat", "reporter": "Suit", "businessman": "Suit",
    "banker": "Suit", "merchant": "Suit", "labourer": "Worker", "laborer": "Worker", "traveller": "Adventurer",
    "traveler": "Adventurer", "hero": "Adventurer", "prince": "King", "emperor": "King", "queen": "King",
}

WORDS_PER_SECOND = {"en": 2.5, "hi": 2.06}


# ------------------------------------------------------------------ reading the asset packs (no Blender needed)
def read_gltf_json(path):
    try:
        if path.lower().endswith(".gltf"):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        with open(path, "rb") as fh:
            if fh.read(4) != b"glTF":
                return None
            fh.read(8)
            clen, _t = struct.unpack("<II", fh.read(8))
            return json.loads(fh.read(clen).decode("utf-8"))
    except Exception:
        return None


def short(name):
    """'AnimalArmature|Idle.001' -> 'Idle'"""
    name = str(name).split("|")[-1].split(".")[0]
    return name.replace("_AnimalArmature", "").replace("AnimalArmature", "").strip("_ ")


def build_vocab(assets_dir):
    vocab = {}
    if assets_dir and os.path.isdir(assets_dir):
        for root, dirs, files in os.walk(assets_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "out"]
            for f in files:
                if not f.lower().endswith((".glb", ".gltf")):
                    continue
                j = read_gltf_json(os.path.join(root, f))
                if not j or not j.get("skins") or not j.get("animations"):
                    continue
                anims = sorted(set(a for a in (short(a.get("name", "")) for a in j["animations"]) if a))
                stem = os.path.splitext(f)[0]
                kind = "human" if ("Sword_Slash" in anims or "Punch_Left" in anims) else "animal"
                vocab.setdefault(stem, {"kind": kind, "animations": anims})
    if not vocab:
        print("NOTE: no asset packs found here, using the built-in character list.", flush=True)
        vocab = {k: dict(v) for k, v in BUILTIN.items()}
    return vocab


PROMPT_ACTIONS = {"idle", "idle_2", "idle_2_headlow", "idle_headlow", "idle_neutral", "eating", "walk", "gallop",
                  "gallop_jump", "run", "interact", "wave", "hitrecieve", "hitrecieve_2", "idle_hitreact1",
                  "attack", "attack_headbutt", "attack_kick", "death", "punch_left", "punch_right", "sword_slash"}


def vocab_text(vocab):
    groups = {}
    for name, info in sorted(vocab.items()):
        shown = [a for a in info["animations"] if a.lower() in PROMPT_ACTIONS] or info["animations"]
        groups.setdefault((info["kind"], tuple(shown)), []).append(name)
    return "\n".join("- %s models: %s\n  actions: %s" % (kind.upper(), ", ".join(names), ", ".join(anims))
                     for (kind, anims), names in groups.items())


# ------------------------------------------------------------------ config and topic of the day
def load_config(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def pick_topic(cfg, date_str, override):
    if override:
        return override.strip(), 0
    topics = cfg.get("topics") or []
    if not topics:
        sys.exit("No topic given and the config file has no 'topics'.")
    try:
        start = datetime.date.fromisoformat(cfg.get("start_date", "2026-11-06"))
    except Exception:
        start = datetime.date(2026, 11, 6)
    day = datetime.date.fromisoformat(date_str) if date_str else datetime.datetime.utcnow().date()
    n = max(0, (day - start).days)
    return topics[n % len(topics)], n + 1


# ------------------------------------------------------------------ the prompt
EPISODE_PROMPT = """You are the head writer of "{channel}", a YouTube Shorts channel that teaches money basics and
digital-money (fintech) safety to ordinary people in India through short 3D animated stories.
Write ONE episode. Answer with JSON only.

TOPIC OF THIS EPISODE: {topic}

LANGUAGE: {language_rules}

LENGTH: about {seconds} seconds = about {shots} shots and about {words} narration words in total.
Every shot has ONE narration line (max {maxw} words). A few shots may have an empty narration ("").

STORY SHAPE (make it feel like a tiny movie, not a lecture)
1. Hook (first shot): a surprising question, a funny mistake or a real problem, in under 12 words.
2. Meet the characters fast: each one has a clear WANT and a clear FEELING (proud, scared, greedy, hopeful).
3. The mistake: a character does the wrong thing and we see what it costs, with a small number on a card.
4. The turning point: a wiser character shows the better way with one simple example (imaginary round numbers).
5. The result: show the happy change, then 2-3 very simple takeaways.
6. Last shot: one memorable line (max 10 words) + invite viewers to watch the next episode.
Put short dialogue inside the narration so the characters feel alive. Use warm humour and real emotion.
Vary the story each time: different cast, different looks, different shot types.

THE PICTURE MUST MATCH THE WORDS (very important: viewers see ONLY the animation, so it must tell the story)
- ONE moment per shot. Each narration line describes ONE thing we can SEE: who does what. Do not pack two events
  into one line. Show thoughts, numbers and rules with a "card" instead.
- Name the character who acts, with a verb that matches the action you choose:
    standing or talking        -> Idle_Neutral (humans) / Idle (animals), shot type two_shot or close_up
    walks, goes, travels       -> Walk, shot type follow  (use follow ONLY when the narration says someone goes or walks)
    runs, flees, rushes        -> Run (humans) / Gallop (animals), shot type follow or chase
    chases someone             -> shot type chase, the character who RUNS AWAY is the FIRST actor
    gives, pays, counts, taps  -> Interact (humans only), shot type two_shot or close_up
    greets or cheers           -> Wave (humans only)
    sad or worried (animal)    -> Idle_2_HeadLow or Idle_Headlow
    shocked, tricked, scared   -> HitRecieve (humans) / Idle_HitReact1 (animals)
    eats                       -> Eating (animals)
- NEVER use Walk, Run or Gallop in a shot unless the narration says the character moves.
- NEVER use Attack, Death, Punch or Sword actions unless the story really shows a fight or a loss. Avoid fights.
- If the narration names a character, that character MUST be in "actors" of the same shot. List the speaker or the
  one who acts FIRST. Use at most 3 characters in a talking shot.
- Pick the "env" from the mood: danger/scam/fraud = fire_red, hardship/debt/fear = winter_night,
  hope/farming/saving = meadow_day, bank/shop/phone payment = town_day, lesson learned/calm = forest_sunset.
- Change the shot type often (close_up, two_shot, follow, chase, group, establishing) so the picture changes
  every 5-6 seconds. Use empty_pan only for time passing.
- Use the "coins" prop only when the narration talks about saving or earning money, and "growing_tree" only when it
  talks about growth, patience or time.
- For fintech topics, imagine animals and people as the story world of phones, payments, loans and scams:
  a sly fox "caller" who tricks people, a wolf "lender" with hidden fees, a banker who explains safe habits.
  Never show real app names or logos.

CAST (use these names; the model is fixed for each name):
{cast}
You may add one extra animal from the model list if the story needs it (name it with a single simple word).

SAFE-FINANCE RULES (very important, the channel follows Indian rules for unregistered educators)
- General education only. Use imaginary people and imaginary round numbers with the rupee sign (example: Rs 100 a day).
- NEVER give buy/sell advice or tips. NEVER name real companies, banks, apps, stocks, mutual funds, indexes
  (like Nifty/Sensex), cryptocurrencies or any brand. NEVER promise, guarantee or predict returns.
  NEVER say "double your money", "risk-free", "sure-shot", "100% safe" or "get rich quick".
- Show that every choice has risk. Add ONE short natural line: check that a lender or platform is registered
  (RBI for loans and payments, SEBI for investing), read the rules, and ask a registered adviser for personal advice.
- For scam topics: teach the warning signs and what to do (stop, do not share OTP or PIN, call the official number,
  report on the national cyber crime helpline 1930 or cybercrime.gov.in).

WHAT YOU CAN SHOW (use ONLY these)
Characters and their actions:
{vocab}

Looks ("env"):
{envs}

Shot types ("type") and how many characters each needs:
{types}

Small props ("prop", optional, only for types establishing, group, two_shot, close_up):
{props}

"card" = a short on-screen text (max 8 words) that highlights the key idea or a number, for example
"Rs 100 a day = Rs 36,500 a year". Use a card in about half of the shots. Use null when not needed.

ANSWER FORMAT (JSON, nothing else)
{{
  "title_text": "short on-screen title of the episode",
  "outro_text": "one short line inviting viewers to watch the next episode",
  "yt_title": "YouTube title, max 80 characters, honest, no clickbait",
  "yt_description": "2-3 plain sentences describing the lesson",
  "yt_tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
  "shots": [
    {{"title": "2-4 English words", "env": "meadow_day", "type": "two_shot",
      "narration": "...", "card": "Rs 100 a day", "prop": "coins",
      "actors": [{{"name": "Hari", "action": "Idle_Neutral"}}]}}
  ]
}}
{extra}
"""

STORY_PROMPT_EXTRA = """
SOURCE TEXT (turn this into the episode instead of inventing one; keep its meaning):
\"\"\"
{story}
\"\"\"
"""


def make_prompt(cfg, topic, vocab, seconds, lang, story="", extra=""):
    shots = max(8, int(round(seconds / 6.3)))
    words = int(round(seconds * (1.75 if lang == "en" else 1.8)))
    cast_lines = []
    for c in cfg.get("cast", []):
        cast_lines.append("- %s = model %s. %s" % (c["name"], c["model"], c.get("role", "")))
    if lang == "en":
        language_rules = ("Simple spoken English (short sentences, easy words, friendly storyteller voice). "
                          "Write numbers and the rupee sign the way a person would say them.")
        maxw = 22
    else:
        language_rules = ("Simple spoken Hindi in Devanagari script. Write names in Devanagari too. "
                          "No English letters in the narration.")
        maxw = 16
    body = EPISODE_PROMPT.format(
        channel=cfg.get("channel_name", "Money Stories"), topic=topic, language_rules=language_rules,
        seconds=seconds, shots=shots, words=words, maxw=maxw,
        cast="\n".join(cast_lines) or "- (no fixed cast: choose from the models below)",
        vocab=vocab_text(vocab), envs="\n".join("- %s: %s" % kv for kv in ENVS.items()),
        types="\n".join("- %s (%d-%d characters): %s" % (k, v[0], v[1], v[2]) for k, v in SHOT_TYPES.items()),
        props="\n".join("- %s: %s" % kv for kv in PROPS.items()), extra=extra)
    if story:
        body += STORY_PROMPT_EXTRA.format(story=story.strip()[:9000])
    return body


# ------------------------------------------------------------------ talking to Gemini (retries + model fallback)
def call_gemini(prompt, key, models, rounds=3):
    errors = []
    for rnd in range(rounds):
        for model in models:
            url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.75, "responseMimeType": "application/json"}}
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json", "x-goog-api-key": key})
                    with urllib.request.urlopen(req, timeout=240) as resp:
                        data = json.load(resp)
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                    if text.strip():
                        print("Gemini model used: %s" % model, flush=True)
                        return text
                    errors.append("%s: empty answer" % model)
                except urllib.error.HTTPError as e:
                    detail = e.read()[:300].decode("utf-8", "ignore")
                    errors.append("%s: HTTP %s %s" % (model, e.code, detail))
                    if e.code in (400, 401, 403, 404):
                        break
                    time.sleep(20 if e.code == 429 else 8)
                except Exception as e:
                    errors.append("%s: %s" % (model, e))
                    time.sleep(8)
        if rnd < rounds - 1:
            print("Gemini round %d failed, waiting before trying again ..." % (rnd + 1), flush=True)
            time.sleep(45 * (rnd + 1))
    raise RuntimeError("Gemini did not answer.\n  " + "\n  ".join(errors[-6:]))


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ------------------------------------------------------------------ checking and fixing the answer
def has_devanagari(t):
    return any("\u0900" <= c <= "\u097f" for c in t)


def fix_model(name, vocab, notes):
    if name in vocab:
        return name
    low = {k.lower(): k for k in vocab}
    n = str(name).strip()
    if n.lower() in low:
        return low[n.lower()]
    for k in low:
        if k in n.lower() or n.lower() in k:
            return low[k]
    syn = SYNONYMS.get(n.lower())
    if syn and syn in vocab:
        notes.append("'%s' is not available, used '%s'" % (n, syn))
        return syn
    fallback = "Wolf" if "Wolf" in vocab else sorted(vocab)[0]
    notes.append("'%s' is not available, used '%s'" % (n, fallback))
    return fallback


def fix_action(model, action, vocab, notes):
    anims = vocab[model]["animations"]
    kind = vocab[model]["kind"]
    a = str(action or "").strip()
    low = {x.lower(): x for x in anims}
    if a in anims:
        return a
    if a.lower() in low:
        return low[a.lower()]
    starts = sorted([x for x in anims if x.lower().startswith(a.lower())], key=len) if a else []
    if starts:
        return starts[0]
    word = a.lower()
    table = [
        (("run", "gallop", "flee", "escape", "chase", "sprint"), "Run" if kind == "human" else "Gallop"),
        (("walk", "stroll", "move", "go"), "Walk"),
        (("attack", "fight", "bite", "strike", "hit"), "Sword_Slash" if kind == "human" else "Attack"),
        (("jump", "leap"), "Gallop_Jump" if kind == "animal" else "Roll"),
        (("eat", "graze", "drink"), "Eating" if kind == "animal" else "Interact"),
        (("sad", "afraid", "scared", "fear", "cry", "low"), "Idle_Headlow" if "Idle_Headlow" in anims else "Idle_2"),
        (("wave", "greet", "hello"), "Wave"),
        (("work", "handle", "give", "take", "count", "pay", "plant"), "Interact"),
        (("talk", "speak", "say", "stand", "look", "watch", "think"), "Idle_Neutral" if kind == "human" else "Idle"),
        (("die", "fall", "dead"), "Death"),
    ]
    for keys, target in table:
        if any(k in word for k in keys):
            found = [x for x in anims if x.lower().startswith(target.lower())]
            if found:
                return sorted(found, key=len)[0]
    notes.append("%s: action '%s' not found, used Idle" % (model, a))
    idle = [x for x in anims if x.lower().startswith("idle")]
    return sorted(idle, key=len)[0] if idle else anims[0]


def adapt_type(stype, n_actors):
    if stype not in SHOT_TYPES:
        stype = "two_shot" if n_actors == 2 else ("close_up" if n_actors == 1 else "establishing")
    lo, hi, _ = SHOT_TYPES[stype]
    if n_actors == 0:
        return "empty_pan"
    if stype == "empty_pan":
        return "establishing"
    if n_actors < lo:
        if stype in ("two_shot", "fight", "chase"):
            return "close_up"
        if stype == "group":
            return "establishing"
    return stype


WARNING_WORDS = ["scam", "fake", "fraud", "beware", "careful", "trap", "lie", "lies", "never", "don't", "do not",
                 "too good", "no guarantee", "not guaranteed", "cannot", "can't", "no one can", "cheat", "avoid",
                 "warning", "danger", "run away", "myth", "suspicious", "trick"]
HARD_PATTERNS = [r"will (?:give|earn|make|pay) you \d+\s*%", r"\d+\s*% (?:returns? )?(?:every|per|each) (?:month|week|day)"]


def compliance_problems(texts):
    """Safe-Finance check on every sentence viewers will hear or read.
    Scam phrases are allowed only when the sentence is clearly a warning;
    promises of returns and real brand names are never allowed."""
    found = []
    for where, t in texts:
        low = t.lower()
        warning = any(w in low for w in WARNING_WORDS)
        for pat in (HARD_PATTERNS if warning else BANNED_PATTERNS):
            if re.search(pat, low):
                found.append("%s uses a banned phrase (%s): \"%s\"" % (where, pat, t[:80]))
        for b in BRAND_WORDS:
            if re.search(r"(?<![a-z])" + re.escape(b) + r"(?![a-z])", low):
                found.append("%s names a real brand/index ('%s'): \"%s\"" % (where, b, t[:80]))
    return found


def cast_key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# ------------------------------------------------------------------ the director: picture must follow the words
def _rx(words):
    return re.compile(r"\b(?:%s)\b" % "|".join(words), re.I)


RE_RUN = _rx([r"ran", r"run(?:s|ning)?", r"rush(?:ed|es|ing)?", r"fled", r"flee(?:s|ing)?", r"escap\w+", r"sprint\w*",
              r"hurr(?:y|ied|ies|ying)", r"rac(?:e|ed|es|ing)", r"dash(?:ed|es|ing)?", r"bolt(?:ed)?", r"gallop\w*",
              r"chas(?:e|ed|es|ing)", r"pursu\w+"])
RE_WALK = _rx([r"walk(?:ed|s|ing)?", r"went to", r"go(?:es)? to", r"going to", r"came to", r"arriv\w+",
               r"head(?:ed|ing) (?:to|for|home|out)", r"journey\w*", r"travel\w*", r"stroll\w*", r"march\w*",
               r"reach(?:ed|es) the", r"set out", r"left (?:the|for|home)", r"trot\w*", r"rode", r"rid(?:e|ing)"])
RE_CHASE = _rx([r"chas(?:e|ed|es|ing)", r"pursu\w+", r"after (?:him|her|them|it)", r"ran after", r"hunt\w*",
                r"(?:ran|run|fled|flee|escaped|escape)(?: away)? from"])
RE_FIGHT = _rx([r"fight\w*", r"fought", r"attack\w*", r"punch\w*", r"kick\w*", r"slash\w*", r"battle\w*", r"clash\w*",
                r"struck", r"hit"])
RE_DIE = _rx([r"died", r"dead", r"death", r"collaps\w+", r"fell", r"faint\w*", r"dying"])
RE_EAT = _rx([r"ate", r"eat(?:s|ing)?", r"graz\w+", r"food", r"meal", r"lunch", r"dinner", r"feast\w*", r"snack\w*",
              r"chew\w*"])
RE_SHOCK = _rx([r"shock\w*", r"panic\w*", r"gasp\w*", r"trick(?:ed|s)?", r"cheat(?:ed|s)?", r"fooled", r"robbed", r"stole",
                r"stolen", r"vanish\w*", r"disappear\w*", r"scream\w*"])
RE_SAD = _rx([r"sad", r"cried", r"cry", r"crying", r"worried", r"worry", r"afraid", r"scared", r"fear\w*", r"regret\w*",
              r"alone", r"hopeless", r"trapped", r"stuck", r"tears"])
RE_HANDLE = _rx([r"gave", r"give[sn]?", r"giving", r"took", r"take[sn]?", r"pay", r"paid", r"paying", r"count\w*",
                 r"saved?", r"saving", r"deposit\w*", r"hand(?:ed|s)?", r"lend\w*", r"lent", r"borrow\w*", r"scan\w*",
                 r"tap(?:ped|s)?", r"sign(?:ed|s)?", r"shar(?:e|ed|es|ing)", r"plant\w*", r"bought", r"buy\w*", r"sold",
                 r"sell\w*", r"spen[dt]\w*", r"withdr\w+", r"transfer\w*", r"click\w*", r"press\w*", r"typed?",
                 r"enter(?:ed|s)?", r"check(?:ed|s)?", r"open(?:ed|s)?", r"put"])
RE_GREET = _rx([r"hello", r"hi", r"namaste", r"welcome", r"greet\w*", r"wave[sd]?", r"waving", r"goodbye", r"bye",
                r"cheer\w*", r"hello"])
RE_CELEB = _rx([r"happy", r"joy\w*", r"celebrat\w*", r"proud", r"smil\w*", r"laugh\w*", r"danc\w*", r"won", r"success\w*",
                r"hooray", r"yay"])
RE_COINS = _rx([r"coins?", r"sav\w+", r"rs", r"rupees?", r"money", r"earn\w*", r"income", r"wealth", r"deposit\w*",
                r"interest", r"salary", r"cash", r"fund\w*"])
RE_GROW = _rx([r"grow\w*", r"grew", r"patien\w+", r"compound\w*", r"tree", r"seed\w*", r"years?", r"time",
               r"long[- ]term", r"plant\w*"])


def detect_mood(low):
    for name, rx in (("shock", RE_SHOCK), ("sad", RE_SAD), ("handle", RE_HANDLE), ("eat", RE_EAT),
                     ("greet", RE_GREET), ("celeb", RE_CELEB)):
        if rx.search(low):
            return name
    return "talk"


def is_move(action):
    return str(action).lower().startswith(("walk", "run", "gallop", "roll", "jump"))


def is_violent(action):
    return str(action).lower().startswith(("attack", "sword", "punch", "kick", "gun", "idle_gun", "idle_sword", "run_shoot"))


def mood_action(model, vocab, mood, notes):
    kind = vocab[model]["kind"]
    anims = vocab[model]["animations"]
    if kind == "human":
        table = {"shock": ("HitRecieve",), "sad": ("Idle", "Idle_Neutral"), "handle": ("Interact",),
                 "eat": ("Interact",), "greet": ("Wave",), "celeb": ("Wave",), "talk": ("Idle_Neutral", "Idle")}
    else:
        table = {"shock": ("Idle_HitReact1",), "sad": ("Idle_2_HeadLow", "Idle_Headlow", "Idle_2"),
                 "handle": ("Idle",), "eat": ("Eating",), "greet": ("Idle",), "celeb": ("Idle",), "talk": ("Idle",)}
    for cand in table.get(mood, table["talk"]):
        for x in sorted(anims, key=len):
            if x.lower() == cand.lower():
                return x
    return fix_action(model, "Idle", vocab, notes)


def move_action(model, vocab, fast, notes):
    human = vocab[model]["kind"] == "human"
    return fix_action(model, ("Run" if human else "Gallop") if fast else "Walk", vocab, notes)


def direct_shot(narr, rtype, actors, vocab, notes, i):
    """Makes the shot type and every character's action agree with the narration. Returns the shot type."""
    n = len(actors)
    if n == 0:
        return rtype
    low = narr.lower()
    new_type = rtype
    fight = bool(RE_FIGHT.search(low))
    run, walk = bool(RE_RUN.search(low)), bool(RE_WALK.search(low))
    moving_story = bool(narr) and (run or walk) and not fight
    moving_type = rtype in ("follow", "chase")

    if moving_story and rtype != "hero_rock":
        new_type = "chase" if (n >= 2 and RE_CHASE.search(low)) else "follow"
        wants_move = True
        if new_type != rtype:
            notes.append("shot %d: the story says someone moves, so shot type changed %s -> %s" % (i, rtype, new_type))
        if new_type == "chase":
            # the one who runs AWAY goes first: "A chased B" -> B first; "A ran from B" -> A first
            order = list(range(n))
            named = sorted([k for k in range(n) if actors[k]["name"].lower() in low],
                           key=lambda k: low.find(actors[k]["name"].lower()))
            if len(named) >= 2:
                chased_form = bool(re.search(r"chas(?:e|ed|es|ing)|pursu\w+", low)) and \
                    not re.search(r"\b(?:ran|run|fled|flee)\b", low)
                runner = named[1] if chased_form else named[0]
                order = [runner] + [k for k in range(n) if k != runner]
            actors[:] = [actors[k] for k in order]
    elif moving_type and narr:
        wants_move = False
        new_type = "two_shot" if n in (2, 3) else ("close_up" if n == 1 else "group")
        notes.append("shot %d: the story shows no travel, so %s changed to %s" % (i, rtype, new_type))
    else:
        wants_move = rtype in ("follow", "chase")

    if rtype == "fight" and narr and not fight:
        new_type = "two_shot" if n in (2, 3) else ("group" if n >= 4 else "close_up")
        notes.append("shot %d: no fight in the story, so 'fight' changed to %s" % (i, new_type))

    words = set(re.sub(r"[^a-z0-9]", " ", low).split())
    subjects = [a for a in actors if cast_key(a["name"]) in words] or actors[:1]
    mood = detect_mood(low)

    for a in actors:
        m = a["model"]
        act = a["action"]
        if wants_move:
            a["action"] = move_action(m, vocab, run, notes)
            continue
        if is_move(act):
            act = mood_action(m, vocab, "talk", notes)
            notes.append("shot %d: %s was walking on the spot, now standing" % (i, a["name"]))
        if is_violent(act) and (not fight or new_type != "fight"):
            act = mood_action(m, vocab, "talk", notes)
        if act.lower().startswith("death") and not RE_DIE.search(low):
            act = mood_action(m, vocab, "shock" if mood in ("shock", "sad") else "talk", notes)
        if act.lower().startswith("eating") and not RE_EAT.search(low):
            act = mood_action(m, vocab, "talk", notes)
        if act.lower().startswith("wave") and not (RE_GREET.search(low) or RE_CELEB.search(low)):
            act = mood_action(m, vocab, "talk", notes)
        if act.lower().startswith("hitrecieve") and not (RE_SHOCK.search(low) or RE_SAD.search(low)):
            act = mood_action(m, vocab, "talk", notes)
        a["action"] = act

    if not wants_move and narr and mood != "talk":
        for a in subjects:
            if a["action"].lower() in ("idle", "idle_neutral", "idle_2") or a["action"].lower().startswith("idle_2"):
                a["action"] = mood_action(a["model"], vocab, mood, notes)
    return new_type



def validate(answer, vocab, cfg, lang):
    """Returns (shots, meta, notes, hard_errors)."""
    notes, hard = [], []
    if not isinstance(answer, dict) or not isinstance(answer.get("shots"), list):
        return [], {}, notes, ["the answer has no 'shots' list"]
    cast = {cast_key(c["name"]): c for c in cfg.get("cast", [])}
    shots_in = answer["shots"]
    if len(shots_in) < 8:
        hard.append("only %d shots; at least 8 are needed" % len(shots_in))
    out = []
    for i, s in enumerate(shots_in, 1):
        if not isinstance(s, dict):
            continue
        env = s.get("env", "meadow_day")
        if env not in ENVS:
            notes.append("shot %d: look '%s' unknown, used meadow_day" % (i, env))
            env = "meadow_day"
        narr = str(s.get("narration") or s.get("narration_hi") or "").strip()
        if narr:
            if lang == "hi" and not has_devanagari(narr):
                hard.append("shot %d: narration is not in Hindi (Devanagari)" % i)
            if lang == "en" and has_devanagari(narr):
                hard.append("shot %d: narration must be in English" % i)
            if len(narr.split()) > 28:
                notes.append("shot %d: narration is long (%d words)" % (i, len(narr.split())))
        card = str(s.get("card") or "").strip()
        if card.lower() in ("null", "none"):
            card = ""
        if len(card.split()) > 10:
            card = " ".join(card.split()[:10])

        def build_actor(raw, action, tint_in, seen):
            key = cast_key(raw)
            member = cast.get(key)
            if member:
                model, name, tint = member["model"], re.sub(r"[^A-Za-z0-9_]", "", member["name"]), member.get("tint")
                if model not in vocab:
                    model = fix_model(model, vocab, notes)
            else:
                model = fix_model(raw, vocab, notes)
                name = re.sub(r"[^A-Za-z0-9_]", "", raw) or model
                tint = tint_in
            if name in seen:
                name = name + str(len(seen))
            seen.add(name)
            return {"name": name, "model": model, "action": fix_action(model, action, vocab, notes),
                    "tint": tint if tint in TINTS else None}

        actors, seen = [], set()
        for a in (s.get("actors") or []):
            if not isinstance(a, dict):
                continue
            raw = str(a.get("name") or a.get("model") or "").strip()
            if a.get("name") and not cast.get(cast_key(raw)) and a.get("model"):
                actors.append(build_actor(str(a.get("model")), a.get("action"), a.get("tint"), seen))
                actors[-1]["name"] = re.sub(r"[^A-Za-z0-9_]", "", raw) or actors[-1]["name"]
            else:
                actors.append(build_actor(raw, a.get("action"), a.get("tint"), seen))

        raw_type = str(s.get("type") or "")
        cap = SHOT_TYPES[raw_type][1] if raw_type in SHOT_TYPES and raw_type != "empty_pan" else 3

        # the picture must match the words: a cast member named in the narration must be in the shot
        if narr:
            words = set(re.sub(r"[^a-z0-9]", " ", narr.lower()).split())
            present = set(cast_key(x["name"]) for x in actors)
            added = 0
            for key, member in cast.items():
                if key in words and key not in present and len(actors) < cap and added < 2:
                    actors.append(build_actor(member["name"], "Idle", None, seen))
                    notes.append("shot %d: '%s' is named in the narration, so added to the picture" % (i, member["name"]))
                    present.add(key)
                    added += 1

        raw_type = direct_shot(narr, raw_type, actors, vocab, notes, i)
        stype = adapt_type(raw_type, len(actors))
        prop = s.get("prop") if s.get("prop") in PROPS else None
        if prop and narr:
            rx = RE_COINS if prop == "coins" else RE_GROW
            if not rx.search(narr.lower()):
                notes.append("shot %d: prop '%s' removed (the narration does not talk about it)" % (i, prop))
                prop = None
        if prop and stype not in PROP_OK_TYPES:
            notes.append("shot %d: prop '%s' removed (shot type %s)" % (i, prop, stype))
            prop = None
        out.append({"title": str(s.get("title") or "Shot %d" % i)[:40], "env": env, "type": stype,
                    "narration": narr, "card": card, "prop": prop, "actors": actors})
    meta = {k: answer.get(k) for k in ("title_text", "outro_text", "yt_title", "yt_description", "yt_tags")}
    texts = [("shot %d narration" % i, s["narration"]) for i, s in enumerate(out, 1) if s["narration"]]
    texts += [("shot %d card" % i, s["card"]) for i, s in enumerate(out, 1) if s["card"]]
    texts += [(k, str(meta.get(k) or "")) for k in ("title_text", "outro_text", "yt_title", "yt_description")]
    hard += compliance_problems(texts)
    return out, meta, notes, hard


# ------------------------------------------------------------------ shot type -> exact positions and camera
def make_actor(spec, vocab, start, end=None, heading=None):
    m = spec["model"]
    a = {"name": spec["name"], "model": m, "action": spec["action"],
         "start": list(start), "end": list(end if end is not None else start)}
    if vocab[m]["kind"] == "human":
        a["height"] = HUMAN_HEIGHT.get(m, 1.8)
    else:
        a["size"] = SIZES.get(m, 2.0)
    if heading is not None:
        a["heading"] = heading
    if spec.get("tint"):
        a["tint"] = TINTS[spec["tint"]]
    return a


HW = 0.3375       # half picture width per metre of camera distance (30 mm lens, 720x1280 portrait video)
ZOOM_END = 0.92   # the camera moves in a little during a shot


def is_animal(m, vocab):
    return vocab[m]["kind"] == "animal"


def body_w(m, vocab):      # width seen from the side
    return SIZES.get(m, 2.0) if is_animal(m, vocab) else 0.8


def front_w(m, vocab):     # width seen from the front
    return 0.45 * SIZES.get(m, 2.0) if is_animal(m, vocab) else 0.75


def subj_h(m, vocab):      # body height
    return 0.55 * SIZES.get(m, 2.0) if is_animal(m, vocab) else HUMAN_HEIGHT.get(m, 1.8)


def dist_for(half_width, zoom=ZOOM_END):
    return half_width / (HW * zoom)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def line_layout(widths, gap):
    xs, cur = [], 0.0
    for k, w in enumerate(widths):
        if k:
            cur += (widths[k - 1] + w) / 2.0 + gap
        xs.append(cur)
    return xs


def group_layout(widths):
    """Positions (x, y) so that every character is visible and nobody overlaps."""
    n = len(widths)
    xs = line_layout(widths, 0.6)
    if n <= 4 and (xs[-1] - xs[0]) <= 7.5:
        cx = (xs[0] + xs[-1]) / 2.0
        return [(x - cx, 1.0 if k % 2 else 0.0) for k, x in enumerate(xs)]
    front = [k for k in range(n) if k % 2 == 0]
    back = [k for k in range(n) if k % 2 == 1]
    pos = {}
    for idxs, y, shift in ((front, 0.0, 0.0), (back, 1.9, 0.9)):
        if not idxs:
            continue
        rx = line_layout([widths[k] for k in idxs], 0.5)
        cx = (rx[0] + rx[-1]) / 2.0
        for k, x in zip(idxs, rx):
            pos[k] = (x - cx + shift, y)
    return [pos[k] for k in range(n)]


def frame_half(items, vocab, side=True, margin=0.4):
    """items: list of (spec, x). Returns the half width the camera must cover."""
    cx = sum(x for _s, x in items) / len(items)
    return max(abs(x - cx) + (body_w(sp["model"], vocab) if side else front_w(sp["model"], vocab)) / 2.0
               for sp, x in items) + margin


def expand(shot, idx, vocab):
    t, specs = shot["type"], shot["actors"]
    actors, cam, extra = [], {}, {}

    if t == "empty_pan":
        cam = {"look_at": [0, 12, 3.0], "pos": [6, -9, 2.2], "pos_end": [-6, -9, 2.2]}
    elif t in ("establishing", "group"):
        use = specs[:(5 if t == "establishing" else 6)]
        pos = group_layout([body_w(sp["model"], vocab) for sp in use])
        items = []
        for sp, (x, y) in zip(use, pos):
            actors.append(make_actor(sp, vocab, (round(x, 2), round(y, 2)), heading=0 if x < 0 else 180))
            items.append((sp, x))
        d = clamp(dist_for(frame_half(items, vocab)), 7.0, 17.0)
        cam = {"look_at": "all", "pos": [0, -d, 1.6 + 0.08 * d], "pos_end": [0, -d * ZOOM_END, 1.5 + 0.07 * d]}
    elif t == "follow":
        use = specs[:4]
        gap = max(body_w(sp["model"], vocab) for sp in use) / 2.0 + 1.0
        offs = [(0.0, 0.0), (-gap, 1.2), (-0.7 * gap, -1.2), (-2 * gap, 0.3)]
        items = []
        for k, sp in enumerate(use):
            ox, oy = offs[k]
            actors.append(make_actor(sp, vocab, (ox - 4.0, oy), (ox + 4.0, oy)))   # real distance is set later by retime_v2.py
            items.append((sp, ox))
        d = clamp(dist_for(frame_half(items, vocab, margin=0.8), 1.0), 6.5, 14.0)
        cam = {"look_at": "all", "offset": [0, -d, 1.8], "shake": 0.02}
    elif t == "chase":
        use = specs[:5]
        gap = max(body_w(sp["model"], vocab) for sp in use) / 2.0 + 1.6
        items = []
        for k, sp in enumerate(use):
            ox = 0.0 if k == 0 else -gap * k - 1.2
            oy = 0.0 if k == 0 else [0.5, -0.9, 1.6, -1.6][(k - 1) % 4]
            actors.append(make_actor(sp, vocab, (ox - 4.0, oy), (ox + 4.0, oy)))
            items.append((sp, ox))
        d = clamp(dist_for(frame_half(items, vocab, margin=1.0), 1.0), 8.0, 16.0)
        cam = {"look_at": "all", "offset": [0, -d, 2.0], "shake": 0.03}
    elif t == "two_shot":
        use = specs[:3]
        ws = [body_w(sp["model"], vocab) for sp in use]
        xs = line_layout(ws, 0.7)
        cx = (xs[0] + xs[-1]) / 2.0
        items = []
        for k, (sp, x) in enumerate(zip(use, xs)):
            actors.append(make_actor(sp, vocab, (round(x - cx, 2), 0.6 if k == 2 else 0.0), heading=0 if k == 0 else 180))
            items.append((sp, x - cx))
        d = max(5.5, dist_for(frame_half(items, vocab, margin=0.5)))
        cam = {"look_at": "all", "pos": [0, -d, 1.5], "pos_end": [0, -d * ZOOM_END, 1.4]}
    elif t == "close_up":
        use = specs[:2]
        hmax = max(subj_h(sp["model"], vocab) for sp in use)
        d = clamp(hmax / 0.48, 2.8, 5.5)
        if len(use) == 2:
            sep = (front_w(use[0]["model"], vocab) + front_w(use[1]["model"], vocab)) / 2.0 + 0.5
            items = [(use[0], -sep / 2.0), (use[1], sep / 2.0)]
            d = max(d, dist_for(frame_half(items, vocab, side=False, margin=0.4), 0.9))
            actors.append(make_actor(use[0], vocab, (round(-sep / 2.0, 2), 0.0), heading=270))
            actors.append(make_actor(use[1], vocab, (round(sep / 2.0, 2), 0.3), heading=270))
        else:
            actors.append(make_actor(use[0], vocab, (0, 0), heading=270))
        cz = 0.8 + 0.25 * min(hmax, 1.8)
        cam = {"look_at": "all", "pos": [0, -d, cz], "pos_end": [0, -d * 0.9, cz + 0.05]}
    elif t == "hero_rock":
        actors.append(make_actor(specs[0], vocab, (-2, 3.5, 1.6), heading=270))
        extra = {"rock": [-2, 3.5, 1.6], "river": False}
        cam = {"look_at": [-2, 3.5, 2.3], "pos": [-2, -5, 0.8], "pos_end": [-2, -3.5, 1.1]}
    elif t == "fight":
        use = specs[:4]
        ws = [body_w(sp["model"], vocab) for sp in use[:2]]
        sep = (ws[0] + ws[1]) / 2.0 + 0.3
        pl = [(-sep / 2.0, 0.0, 0), (sep / 2.0, 0.0, 180)]
        if len(use) > 2:
            pl.append((-sep / 2.0 - (body_w(use[2]["model"], vocab) + ws[0]) / 2.0 - 0.5, 1.4, 0))
        if len(use) > 3:
            pl.append((sep / 2.0 + (body_w(use[3]["model"], vocab) + ws[1]) / 2.0 + 0.5, 1.2, 180))
        items = []
        for sp, (x, y, h) in zip(use, pl):
            actors.append(make_actor(sp, vocab, (round(x, 2), y), heading=h))
            items.append((sp, x))
        d = max(6.0, dist_for(frame_half(items, vocab, margin=0.6)))
        cam = {"look_at": "all", "pos": [0, -d, 1.8], "pos_end": [0.5, -d * ZOOM_END, 1.7], "shake": 0.07}
    cam["lens"] = 30

    out = {"id": idx, "title": shot["title"], "seconds": 3 if shot["narration"] else 4,
           "env": shot["env"], "narration": shot["narration"], "actors": actors, "camera": cam}
    if shot.get("card"):
        out["card"] = shot["card"]
    if shot.get("prop"):
        pos = {"coins": {"establishing": [1.0, -1.8], "group": [1.0, -2.0], "two_shot": [0.75, -1.3],
                         "close_up": [1.3, -0.9]}.get(t, [1.0, -1.6]),
               "growing_tree": [0.8, 2.6]}[shot["prop"]]
        out["prop"] = {"type": shot["prop"], "pos": pos}
    out.update(extra)
    return out


def check_plan(plan):
    problems = []
    for s in plan["shots"]:
        names = [a["name"] for a in s["actors"]]
        look = s["camera"].get("look_at")
        if isinstance(look, str) and look != "all" and look not in names:
            problems.append("shot %d: camera looks at '%s' who is not in the shot" % (s["id"], look))
        if len(names) != len(set(names)):
            problems.append("shot %d: duplicate character names" % s["id"])
    return problems


def estimate_seconds(plan, lang):
    per_word = 1.0 / WORDS_PER_SECOND.get(lang, 2.0)
    total = 0.0
    for s in plan["shots"]:
        w = len(s["narration"].split())
        total += max(s["seconds"], w * per_word + 0.6) if w else s["seconds"]
    return total


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="channel_v2.json")
    ap.add_argument("--topic", default="", help="override today's topic")
    ap.add_argument("--story", default="", help="optional text file: turn this text into the episode")
    ap.add_argument("--date", default="", help="YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--assets", default=".")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--lang", default="", help="en or hi (default: from the config)")
    ap.add_argument("--out", default="scene_plan_auto.json")
    ap.add_argument("--report", default="plan_report.md")
    ap.add_argument("--meta", default="video_meta.json")
    ap.add_argument("--mock-response", default="", help="use this JSON file instead of calling Gemini (testing)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    lang = args.lang or cfg.get("language", "en")
    seconds = args.seconds or int(cfg.get("target_seconds", 150))
    topic, episode_no = pick_topic(cfg, args.date, args.topic)
    story = ""
    if args.story:
        with open(args.story, "r", encoding="utf-8") as fh:
            story = fh.read()
        topic = topic if args.topic else "the story below"
    print("Topic: %s | language: %s | length: %d s | episode %d" % (topic, lang, seconds, episode_no), flush=True)

    vocab = build_vocab(args.assets)
    print("Characters available: %d" % len(vocab), flush=True)
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
    base_prompt = make_prompt(cfg, topic, vocab, seconds, lang, story)

    shots, meta, notes, hard = [], {}, [], ["not started"]
    prompt = base_prompt
    for attempt in range(1, 5):
        if args.mock_response:
            raw = open(args.mock_response, "r", encoding="utf-8").read()
        else:
            if not key:
                sys.exit("GEMINI_API_KEY is missing. Get a free key at https://aistudio.google.com/apikey "
                         "and add it as a repository secret named GEMINI_API_KEY.")
            raw = call_gemini(prompt, key, models)
        try:
            shots, meta, notes, hard = validate(parse_json(raw), vocab, cfg, lang)
        except Exception as e:
            shots, meta, notes, hard = [], {}, [], ["the answer was not valid JSON (%s)" % e]
        if not hard:
            est = estimate_seconds({"shots": [expand(s, i, vocab) for i, s in enumerate(shots, 1)]}, lang)
            if est > 172:
                hard = ["the film would be about %d seconds; it must be under %d, use fewer shots/words"
                        % (est, min(170, seconds + 15))]
        if not hard:
            break
        print("Attempt %d problems:\n  - %s" % (attempt, "\n  - ".join(hard[:8])), flush=True)
        if args.mock_response:
            break
        prompt = base_prompt + "\nYOUR LAST ANSWER HAD PROBLEMS. FIX ALL OF THEM AND ANSWER AGAIN:\n- " + "\n- ".join(hard[:10]) + "\n"
    if hard:
        sys.exit("Could not get a safe, usable episode after several tries:\n- " + "\n- ".join(hard[:10]))

    disclaimer = cfg.get("disclaimer", "Educational content only. Not investment advice.")
    plan = {"title": "Episode %d: %s" % (episode_no, topic), "lang": lang,
            "title_text": meta.get("title_text") or topic,
            "outro_text": meta.get("outro_text") or "Watch the next episode!",
            "disclaimer": disclaimer, "fps": 12, "asset_facing": 90,
            "shots": [expand(s, i, vocab) for i, s in enumerate(shots, 1)]}
    problems = check_plan(plan)
    est = estimate_seconds(plan, lang)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)

    tags = [str(t).strip().lstrip("#") for t in (meta.get("yt_tags") or []) if str(t).strip()][:12]
    hashtags = "#Shorts #MoneyBasics #FinancialLiteracy " + " ".join("#" + re.sub(r"\W+", "", t) for t in tags[:3])
    video_meta = {"title": (meta.get("yt_title") or plan["title_text"])[:95], "topic": topic, "episode": episode_no,
                  "description": "%s\n\n%s\n\n%s" % (meta.get("yt_description") or "", disclaimer, hashtags),
                  "tags": tags + ["shorts", "money", "finance education"], "language": lang,
                  "made_for_kids": False, "contains_synthetic_media": True}
    with open(args.meta, "w", encoding="utf-8") as fh:
        json.dump(video_meta, fh, ensure_ascii=False, indent=1)

    words = sum(len(s["narration"].split()) for s in plan["shots"])
    lines = ["## Episode plan report", "",
             "Topic: **%s** (episode %d) | shots: **%d** | narration words: **%d** | estimated length: **about %.0f s**"
             % (topic, episode_no, len(plan["shots"]), words, est), "",
             "YouTube title: %s" % video_meta["title"], "",
             "| # | Look | Shot | Characters (model: action) | Narration | Card |", "|---|---|---|---|---|---|"]
    for s, raw_shot in zip(plan["shots"], shots):
        who = ", ".join("%s (%s: %s)" % (a["name"], a["model"], a["action"]) for a in s["actors"]) or "-"
        lines.append("| %d | %s | %s | %s | %s | %s |" % (s["id"], s["env"], raw_shot["type"], who,
                                                      s["narration"] or "(silent)", s.get("card", "")))
    if notes:
        lines += ["", "### Automatic fixes", ""] + ["- " + n for n in sorted(set(notes))]
    if problems:
        lines += ["", "### Problems", ""] + ["- " + p for p in problems]
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)
    if problems:
        sys.exit("The plan has problems (see the report).")


if __name__ == "__main__":
    main()
