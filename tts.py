#!/usr/bin/env python3
"""
Hindi narration for every shot of a scene plan.

For each shot that has "narration_hi", this makes one audio file, measures its length,
and writes a TIMED plan where every shot is long enough for its voice.

  python3 tts.py --plan scene_plan_2min.json --gender male

Engines
  edge    Microsoft Edge voices (hi-IN-MadhurNeural male, hi-IN-SwaraNeural female).
          No key needed. Unofficial service: it can fail some days.
  gemini  Gemini text-to-speech. Needs the secret GEMINI_API_KEY.
  auto    (default) try edge first, then gemini if a key exists.

Outputs
  audio/shot_01.mp3 ...      one file per shot
  scene_plan_timed.json      the plan with real shot lengths
  tts_summary.md             table of audio lengths
  narration_preview.mp3      all narration in one file, to listen to
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave

VOICES = {
    "male": {"edge": "hi-IN-MadhurNeural", "gemini": "Charon"},
    "female": {"edge": "hi-IN-SwaraNeural", "gemini": "Kore"},
}
GEMINI_STYLE = ("Narrate this in Hindi as a calm, dramatic storyteller for a children's "
                "adventure film. Speak clearly and not too fast: ")


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(r.stdout.strip())


# ---------------------------------------------------------------- engines
async def _edge_save(text, voice, rate, path):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def synth_edge(text, voice, rate, path, tries=4):
    last = None
    for i in range(tries):
        try:
            asyncio.run(_edge_save(text, voice, rate, path))
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return
            last = RuntimeError("audio file was empty")
        except Exception as e:  # edge-tts raises several different errors
            last = e
        time.sleep(2 + 2 * i)
    raise RuntimeError("edge-tts failed: %s" % last)


def synth_gemini(text, voice_name, model, api_key, path, tries=4):
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {
        "contents": [{"parts": [{"text": GEMINI_STYLE + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}},
        },
    }
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
            pcm = base64.b64decode(b64)
            with wave.open(path, "wb") as wf:      # Gemini returns raw 24 kHz 16-bit mono
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(pcm)
            return
        except urllib.error.HTTPError as e:
            last = "HTTP %s %s" % (e.code, e.read()[:200])
            time.sleep(20 if e.code == 429 else 4)
        except Exception as e:
            last = e
            time.sleep(4)
    raise RuntimeError("gemini tts failed: %s" % last)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_2min.json")
    ap.add_argument("--out-dir", default="audio")
    ap.add_argument("--timed-plan", default="scene_plan_timed.json")
    ap.add_argument("--engine", default="auto", choices=["auto", "edge", "gemini"])
    ap.add_argument("--gender", default="male", choices=["male", "female"])
    ap.add_argument("--rate", default="-8%", help="speaking speed for edge, e.g. -8%% is a bit slower")
    ap.add_argument("--gemini-model", default=os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"))
    ap.add_argument("--pad", type=float, default=0.9, help="extra seconds added to each shot after its voice")
    ap.add_argument("--reuse-audio", action="store_true", help="use audio files that already exist")
    ap.add_argument("--summary", default="tts_summary.md")
    ap.add_argument("--preview", default="narration_preview.mp3")
    args = ap.parse_args()

    with open(args.plan, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    os.makedirs(args.out_dir, exist_ok=True)
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    voice = VOICES[args.gender]
    order = ["edge", "gemini"] if args.engine == "auto" else [args.engine]

    rows, audio_files = [], []
    for shot in plan["shots"]:
        sid = int(shot["id"])
        planned = float(shot["seconds"])
        shot["seconds_planned"] = planned
        text = (shot.get("narration_hi") or "").strip()
        if not text:
            rows.append((sid, shot.get("title", ""), "-", 0.0, planned, planned))
            continue

        base = os.path.join(args.out_dir, "shot_%02d" % sid)
        path, used = None, None
        if args.reuse_audio:
            for ext in (".mp3", ".wav"):
                if os.path.exists(base + ext):
                    path, used = base + ext, "reused"
                    break
        if path is None:
            errors = []
            for eng in order:
                try:
                    if eng == "edge":
                        path = base + ".mp3"
                        synth_edge(text, voice["edge"], args.rate, path)
                    else:
                        if not key:
                            raise RuntimeError("the secret GEMINI_API_KEY is not set")
                        path = base + ".wav"
                        synth_gemini(text, voice["gemini"], args.gemini_model, key, path)
                    used = eng
                    break
                except Exception as e:
                    errors.append("%s: %s" % (eng, e))
                    path = None
            if path is None:
                sys.exit("Could not make the voice for shot %d.\n  %s" % (sid, "\n  ".join(errors)))

        dur = probe_duration(path)
        shot["audio_file"] = path
        shot["audio_seconds"] = round(dur, 2)
        shot["seconds"] = round(max(planned, dur + args.pad), 2)
        audio_files.append(path)
        rows.append((sid, shot.get("title", ""), used, dur, planned, shot["seconds"]))
        print("shot %02d | %-8s | voice %.1f s | planned %.1f s | final %.1f s"
              % (sid, used, dur, planned, shot["seconds"]), flush=True)

    with open(args.timed_plan, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)

    total = sum(r[5] for r in rows)
    voice_total = sum(r[3] for r in rows)
    lines = ["## Hindi narration result", "",
             "Voice: **%s** (%s)" % (args.gender, voice["edge"] if args.engine != "gemini" else voice["gemini"]),
             "",
             "Total voice: **%.0f s**. Final video length: **about %.0f s (%.1f minutes)**."
             % (voice_total, total, total / 60.0), "",
             "| Shot | Title | Engine | Voice (s) | Planned (s) | Final (s) |", "|---|---|---|---|---|---|"]
    for sid, title, eng, dur, planned, final in rows:
        lines.append("| %d | %s | %s | %.1f | %.1f | %.1f |" % (sid, title, eng, dur, planned, final))
    with open(args.summary, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)

    # one preview file with every voice line in a row, so you can listen to the whole story
    if audio_files:
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for f in audio_files:
            cmd += ["-i", f]
        parts = ["[%d:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur=0.6[a%d]" % (i, i)
                 for i in range(len(audio_files))]
        graph = ";".join(parts) + ";" + "".join("[a%d]" % i for i in range(len(audio_files))) \
            + "concat=n=%d:v=0:a=1[out]" % len(audio_files)
        cmd += ["-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", args.preview]
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
