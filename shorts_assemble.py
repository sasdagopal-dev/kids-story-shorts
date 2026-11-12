#!/usr/bin/env python3
"""shorts_assemble.py (v5) - final video, subtitle FILES, optional burned-in captions, and upload data.

  python3 shorts_assemble.py --plan scene_plan_timed.json --timeline timeline.json \
      --video out/video_all.mp4 --out out/final_shorts.mp4 [--music music/bg.mp3] [--burn-captions]

Makes in the output folder:
  final_shorts.mp4     vertical 1080x1920, exactly the planned length
  subtitles.srt / .vtt subtitle files for YouTube (upload them with the video as captions)
  youtube_meta.json    title, description, tags, flags for the later upload step
  thumbnail.jpg        frame from the most eye-catching shot

v5: money stories get finance hashtags and the Education category; story mode and the kids setting come from the plan

v4 changes
* voice is loudness-normalised (-16 LUFS) and the music DUCKS (gets quieter) while someone speaks
* no fade to black at the end by default, so the Short loops cleanly (--fades brings them back)
* optional big burned-in captions (--burn-captions), off by default as before
* animal-channel metadata (category, hashtags), and the "made for kids" flag is now explicit and your decision
* warns when a shot has no voice, and when the video is longer than the 3 minute Shorts limit
"""
import argparse, json, os, re, shutil, subprocess, sys, textwrap


def run(cmd, cwd=None):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd[:14]), "..." if len(cmd) > 14 else "", flush=True)
    subprocess.run(cmd, check=True, cwd=cwd)


def probe(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", path]).decode().strip())


def srt_time(t, sep=","):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def caption_chunks(text, max_line=26):
    """Split a narration into short caption texts (<= 2 lines). Short lines read better on a phone."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?\u0964])\s+", text) if p.strip()]
    chunks = []
    for p in parts:
        nlines = max(1, -(-len(p) // max_line))
        width = -(-len(p) // nlines) + 5
        lines = textwrap.wrap(p, width) or [p]
        for i in range(0, len(lines), 2):
            chunks.append("\n".join(lines[i:i + 2]))
    return chunks


def build_captions(plan, tl):
    tempo = tl.get("tempo", 1.0)
    cues = []
    for s, t in zip(plan["shots"], tl["shots"]):
        start = t["start_time"] + t["lead"]
        span = t["voice_seconds"] / tempo
        chunks = caption_chunks(s["narration"].strip())
        total = sum(len(c) + 8 for c in chunks) or 1            # a pause after every caption
        cur = start
        for c in chunks:
            d = span * (len(c) + 8) / total
            cues.append((cur, min(cur + d - 0.04, t["start_time"] + t["duration"]), c))
            cur += d
    return cues


def measure_loudnorm(path, target=-14.0, tp=-1.0, lra=11.0):
    """Two-pass loudnorm: measure the narration, then build an exact (linear) filter. A quiet voice becomes as loud as any other.
    Falls back to a single-pass filter if the measurement fails."""
    single = f"loudnorm=I={target}:TP={tp}:LRA={lra}"
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", f"loudnorm=I={target}:TP={tp}:LRA={lra}:print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=300)
        txt = r.stderr
        j = json.loads(txt[txt.rindex("{"):txt.rindex("}") + 1])
        vals = {k: float(j[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
        if vals["input_i"] < -70:
            return single
        print(f"narration loudness {vals['input_i']:.1f} LUFS -> target {target} LUFS (gain {target - vals['input_i']:+.1f} dB)", flush=True)
        return (f"loudnorm=I={target}:TP={tp}:LRA={lra}:measured_I={vals['input_i']}:measured_TP={vals['input_tp']}:"
                f"measured_LRA={vals['input_lra']}:measured_thresh={vals['input_thresh']}:offset={vals['target_offset']}:linear=true")
    except Exception as e:
        print(f"loudness measurement skipped ({str(e)[:80]}), using single pass", flush=True)
        return single


def build_description(plan, money):
    """Search-friendly first line, the story, a question for comments (engagement), a teaser for the next episode
    (people come back), then the disclaimer, credits and hashtags. #Shorts comes first (YouTube shows the first 3 above the title)."""
    topic = (plan.get("topic") or "").split(":")[0].strip()
    series = ""
    ep = plan.get("episode")
    if isinstance(ep, dict):
        series = ep.get("series") or ""
    first = plan.get("logline") or ""
    if not first and topic:
        first = f"Learn this investing and money lesson in a short animated story: {topic}."
    parts = [first, plan.get("description", "")]
    if plan.get("viewer_question"):
        parts.append("\U0001F4AC " + str(plan["viewer_question"]).strip())
    if plan.get("cliffhanger"):
        parts.append("\u25B6 Next episode: " + str(plan["cliffhanger"]).strip() + "\nSubscribe so you do not miss it!")
    parts += [plan.get("disclaimer", ""), plan.get("credits", "")]
    hashtags = ("#Shorts #Investing #PersonalFinance #FinancialLiteracy #MoneyTips" if money
                else "#Shorts #AnimalStories #CartoonStories")
    return "\n\n".join(x.strip() for x in parts if x and x.strip()) + "\n\n" + hashtags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="scene_plan_timed.json")
    ap.add_argument("--timeline", default="timeline.json")
    ap.add_argument("--video", required=True, help="joined silent video from the render parts")
    ap.add_argument("--out", required=True)
    ap.add_argument("--music", default="")
    ap.add_argument("--upscale", default="1080x1920", help="final size, empty = keep the render size")
    ap.add_argument("--out-fps", type=int, default=30)
    ap.add_argument("--music-level", type=float, default=0.22, help="music volume while nobody speaks (it ducks under the voice)")
    ap.add_argument("--fades", action="store_true", help="fade in/out of the picture (off = clean loop)")
    ap.add_argument("--burn-captions", action="store_true", help="burn big captions into the picture")
    ap.add_argument("--made-for-kids", choices=["yes", "no"], default="",
                    help="YouTube 'made for kids' declaration; default: plan['made_for_kids'] or yes")
    ap.add_argument("--category", default="", help="YouTube category id; default: 27 Education for money stories, 1 Film & Animation otherwise")
    a = ap.parse_args()

    plan = json.load(open(a.plan, encoding="utf-8"))
    tl = json.load(open(a.timeline, encoding="utf-8"))
    target = float(tl["total_seconds"])
    if target > 180:
        print(f"WARNING: {target:.0f} s is longer than the 3 minute Shorts limit, YouTube will treat it as a normal video.")
    if not os.path.exists(a.video):
        sys.exit(f"FAIL: video not found: {a.video}")
    out_path = os.path.abspath(a.out)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    # ---- narration: every shot padded to its exact length, then joined
    tempo = tl.get("tempo", 1.0)
    parts, silent = [], []
    for t in tl["shots"]:
        wav = os.path.join(work, f"shot_{int(t['id']):02d}.wav")
        lead_ms = int(round(t["lead"] * 1000))
        if t.get("audio") and os.path.exists(t["audio"]):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(t["audio"]), "-af",
                 f"atempo={tempo:.4f},adelay={lead_ms}|{lead_ms},apad=whole_dur={t['duration']:.3f}",
                 "-t", f"{t['duration']:.3f}", "-ar", "44100", "-ac", "2", wav])
        else:
            silent.append(t["id"])
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{t['duration']:.3f}", wav])
        parts.append(f"file '{wav}'")
    if silent:
        print(f"WARNING: no voice for shots {silent}, they are silent in the video.")
    open(os.path.join(work, "list.txt"), "w").write("\n".join(parts))
    narr = os.path.join(work, "narration.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(work, "list.txt"),
         "-c", "copy", narr])
    nd = probe(narr)
    if abs(nd - target) > 0.3:
        sys.exit(f"FAIL: narration is {nd:.2f} s but the plan says {target:.2f} s")

    # ---- captions (also used for the files)
    cues = build_captions(plan, tl)
    srt_path = os.path.join(out_dir, "subtitles.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (s, e, c) in enumerate(cues, start=1):
            f.write(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{c}\n\n")
    with open(os.path.join(out_dir, "subtitles.vtt"), "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for s, e, c in cues:
            f.write(f"{srt_time(s, '.')} --> {srt_time(e, '.')}\n{c}\n\n")

    # ---- video + audio
    def encode(burn):
        vf = []
        if a.upscale:
            w, h = a.upscale.split("x")
            vf.append(f"scale={w}:{h}:flags=lanczos")
        vf.append(f"fps={a.out_fps}")
        if burn:
            shutil.copy(srt_path, os.path.join(work, "cap.srt"))
            vf.append("subtitles=cap.srt:force_style='Alignment=2,Bold=1,FontSize=11,Outline=3,Shadow=0,MarginV=95,"
                      "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&'")
        if a.fades:
            vf += ["fade=t=in:st=0:d=0.3", f"fade=t=out:st={target - 0.5:.2f}:d=0.5"]
        inputs = ["-i", os.path.abspath(a.video), "-i", narr]
        music = os.path.abspath(a.music) if a.music else ""
        tail = f"alimiter=limit=0.95,afade=t=out:st={target - 0.25:.2f}:d=0.25"
        vnorm = measure_loudnorm(narr)
        if music and os.path.exists(music):
            inputs += ["-stream_loop", "-1", "-i", music]
            fc = (f"[1:a]{vnorm},aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[v][vs];"
                  f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={a.music_level},afade=t=in:d=1.5,atrim=0:{target:.3f}[m0];"
                  f"[m0][vs]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=500[md];"
                  f"[v][md]amix=inputs=2:duration=longest:normalize=0,{tail}[a]")
        else:
            fc = f"[1:a]{vnorm},{tail}[a]"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
               "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-t", f"{target:.3f}", "-movflags", "+faststart", out_path]
        run(cmd, cwd=work if burn else None)

    if a.burn_captions:
        try:
            encode(True)
        except subprocess.CalledProcessError:
            print("WARNING: burning captions failed (ffmpeg without libass or font?), making the video without them.")
            encode(False)
    else:
        encode(False)
    fd = probe(out_path)
    if abs(fd - target) > 0.6:
        sys.exit(f"FAIL: final video is {fd:.2f} s but must be {target:.2f} s")

    # ---- thumbnail and upload data
    th = next((t for t in tl["shots"] if t.get("thumb")), tl["shots"][len(tl["shots"]) // 2])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{th['start_time'] + th['duration'] / 2:.2f}", "-i", out_path,
         "-frames:v", "1", "-q:v", "2", os.path.join(out_dir, "thumbnail.jpg")])
    tags = list(plan.get("tags") or [])
    if plan.get("story_mode") == "money":
        fixed = ["investing for beginners", "personal finance", "financial literacy", "money tips", "stock market for beginners",
                 "index funds", "compound interest", "401k", "roth ira", "emergency fund", "how to invest", "animated finance"]
        seen, merged = set(), []
        for t in fixed + [str(x) for x in tags]:
            if t.lower() not in seen:
                seen.add(t.lower()); merged.append(t)
        tags = merged
    kids = a.made_for_kids or ("yes" if plan.get("made_for_kids", True) else "no")
    money = plan.get("story_mode") == "money"
    desc = build_description(plan, money)
    category = a.category or ("27" if money else "1")
    meta = {"title": plan["title"][:100], "description": desc,
            "tags": tags, "categoryId": category, "language": "en" if plan.get("language") == "English" else "hi",
            "privacyStatus": "private", "selfDeclaredMadeForKids": kids == "yes",
            "containsSyntheticMedia": True, "captionFile": "subtitles.srt", "thumbnail": "thumbnail.jpg",
            "durationSeconds": round(fd, 2), "firstComment": str(plan.get("viewer_question") or "").strip()}
    json.dump(meta, open(os.path.join(out_dir, "youtube_meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"DONE: {out_path} ({fd:.2f} s), {len(cues)} caption cues, made-for-kids: {kids}, mode: {plan.get('story_mode', 'adventure')}")


if __name__ == "__main__":
    main()
