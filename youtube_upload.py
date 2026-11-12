#!/usr/bin/env python3
"""youtube_upload.py - uploads the finished Short to YouTube (as a PRIVATE video by default), with captions and thumbnail.

  python3 youtube_upload.py --dir final/out [--privacy private|unlisted|public] [--publish-at 2026-11-12T08:00:00Z] [--dry-run]

Needs the repository secrets YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (and YT_CHANNEL_ID to check the channel).
Reads from --dir: final_shorts.mp4, youtube_meta.json (title, description, tags, category, kids flag), subtitles.srt, thumbnail.jpg.
Only the video upload is required; captions, thumbnail and the channel check are best effort (they need extra permissions).
Writes youtube_result.json (video id and link). Exit codes: 0 done or skipped (no secrets), 1 failed.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request

TOKEN_URL = os.environ.get("YT_TEST_BASE", "https://oauth2.googleapis.com") + "/token"
API = os.environ.get("YT_TEST_BASE", "https://www.googleapis.com")


class YTError(Exception):
    pass


def http(req, timeout=120):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def explain(status, body):
    try:
        err = json.loads(body.decode("utf-8", "replace")).get("error")
        if isinstance(err, dict):
            reasons = ",".join(x.get("reason", "") for x in err.get("errors", []) if isinstance(x, dict))
            return f"HTTP {status}: {err.get('message', '')} [{reasons}]"
        return f"HTTP {status}: {err}"
    except Exception:
        return f"HTTP {status}: {body[:200]!r}"


def get_token(cid, secret, refresh):
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
    status, _, body = http(urllib.request.Request(TOKEN_URL, data=data, method="POST"))
    if status != 200:
        txt = body.decode("utf-8", "replace")
        hint = ""
        if "invalid_grant" in txt:
            hint = (" The refresh token is no longer valid. If your Google OAuth app is still in 'Testing' mode, its tokens expire after 7 days: "
                    "publish the app (set it to 'In production') and create a new refresh token.")
        raise YTError(f"could not sign in to YouTube ({status}): {txt[:200]}.{hint}")
    return json.loads(body)["access_token"]


def check_channel(token, want):
    if not want:
        return
    status, _, body = http(urllib.request.Request(f"{API}/youtube/v3/channels?part=id&mine=true", headers={"Authorization": f"Bearer {token}"}))
    if status != 200:
        print(f"  channel check skipped ({explain(status, body)[:120]})", flush=True)
        return
    ids = [i.get("id") for i in json.loads(body).get("items", [])]
    if ids and want not in ids:
        raise YTError(f"the signed-in channel is {ids[0]} but YT_CHANNEL_ID is {want}. Nothing was uploaded. Create the refresh token while choosing the right channel.")
    print("  channel check: ok", flush=True)


def clean_meta(m, privacy, publish_at):
    tags, total = [], 0
    for t in m.get("tags") or []:
        t = str(t).replace("<", "").replace(">", "")[:30]
        if t and total + len(t) + 1 < 450:
            tags.append(t)
            total += len(t) + 1
    snippet = {"title": str(m["title"]).replace("<", "").replace(">", "")[:100], "description": str(m.get("description", ""))[:4900],
               "tags": tags, "categoryId": str(m.get("categoryId", "1"))}
    lang = m.get("language")
    if lang:
        snippet["defaultLanguage"] = lang
        snippet["defaultAudioLanguage"] = lang
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(m.get("selfDeclaredMadeForKids", True)),
              "containsSyntheticMedia": bool(m.get("containsSyntheticMedia", True)), "embeddable": True}
    if publish_at and privacy == "private":
        status["publishAt"] = publish_at
    return {"snippet": snippet, "status": status}


def start_session(token, meta, size):
    def attempt(body):
        req = urllib.request.Request(f"{API}/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                                              "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
        return http(req)
    status, headers, body = attempt(meta)
    if status == 400 and "containsSyntheticMedia" in body.decode("utf-8", "replace"):          # an older API without this field
        meta["status"].pop("containsSyntheticMedia", None)
        status, headers, body = attempt(meta)
    if status != 200:
        raise YTError("could not start the upload: " + explain(status, body))
    loc = {k.lower(): v for k, v in headers.items()}.get("location")
    if not loc:
        raise YTError("YouTube did not give an upload address")
    return loc


def send_video(url, path, token):
    size = os.path.getsize(path)
    offset = 0
    for attempt in range(1, 7):
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(len(data))}
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
        try:
            status, _, body = http(urllib.request.Request(url, data=data, method="PUT", headers=headers), timeout=900)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            status, body = None, str(e).encode()
        if status in (200, 201):
            return json.loads(body)
        if status is not None and status not in (500, 502, 503, 504):
            raise YTError("the upload failed: " + explain(status, body))
        print(f"  upload interrupted ({status}), checking where to continue (try {attempt}/6)", flush=True)
        time.sleep(min(30, 2 ** attempt))
        try:
            st, hd, bd = http(urllib.request.Request(url, data=b"", method="PUT", headers={"Authorization": f"Bearer {token}", "Content-Length": "0",
                                                                                          "Content-Range": f"bytes */{size}"}))
            if st in (200, 201):
                return json.loads(bd)
            rng = {k.lower(): v for k, v in hd.items()}.get("range")
            offset = int(rng.split("-")[-1]) + 1 if rng else 0
        except Exception:
            pass
    raise YTError("the upload kept failing")


def set_thumbnail(token, vid, path):
    with open(path, "rb") as f:
        data = f.read()
    status, _, body = http(urllib.request.Request(f"{API}/upload/youtube/v3/thumbnails/set?videoId={vid}&uploadType=media", data=data, method="POST",
                                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg", "Content-Length": str(len(data))}))
    print("  thumbnail:", "ok" if status == 200 else "skipped (" + explain(status, body)[:140] + ")", flush=True)


def add_captions(token, vid, path, language):
    boundary = "shortsboundary7f3a"
    meta = json.dumps({"snippet": {"videoId": vid, "language": language, "name": "Subtitles", "isDraft": False}}).encode("utf-8")
    srt = open(path, "rb").read()
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta +
            f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + srt + f"\r\n--{boundary}--".encode())
    status, _, rb = http(urllib.request.Request(f"{API}/upload/youtube/v3/captions?uploadType=multipart&part=snippet", data=body, method="POST",
                                                headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}",
                                                         "Content-Length": str(len(body))}))
    print("  captions:", "ok" if status == 200 else "skipped (" + explain(status, rb)[:140] + ")", flush=True)


def add_to_playlist(token, vid, playlist):
    body = json.dumps({"snippet": {"playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": vid}}}).encode("utf-8")
    status, _, rb = http(urllib.request.Request(f"{API}/youtube/v3/playlistItems?part=snippet", data=body, method="POST",
                                                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}))
    print("  playlist:", "ok" if status == 200 else "skipped (" + explain(status, rb)[:140] + ")", flush=True)


def first_comment(token, vid, text):
    body = json.dumps({"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": text[:500]}}}}).encode("utf-8")
    status, _, rb = http(urllib.request.Request(f"{API}/youtube/v3/commentThreads?part=snippet", data=body, method="POST",
                                                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}))
    print("  first comment:", "ok (pin it in YouTube Studio)" if status == 200 else "skipped (" + explain(status, rb)[:140] + ")", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="out")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    ap.add_argument("--publish-at", default="", help="ISO time, only with private, YouTube publishes it then")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--no-thumbnail", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cid, secret = os.environ.get("YT_CLIENT_ID", "").strip(), os.environ.get("YT_CLIENT_SECRET", "").strip()
    refresh, channel = os.environ.get("YT_REFRESH_TOKEN", "").strip(), os.environ.get("YT_CHANNEL_ID", "").strip()
    video = os.path.join(a.dir, "final_shorts.mp4")
    meta_path = os.path.join(a.dir, "youtube_meta.json")
    if not (os.path.exists(video) and os.path.exists(meta_path)):
        sys.exit(f"FAIL: {video} or {meta_path} is missing")
    m = json.load(open(meta_path, encoding="utf-8"))
    body = clean_meta(m, a.privacy, a.publish_at)
    size = os.path.getsize(video)
    print(f"Video: {video} ({size / 1e6:.1f} MB) | title: {body['snippet']['title']} | privacy: {a.privacy} | made for kids: {body['status']['selfDeclaredMadeForKids']}", flush=True)
    if a.dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=1)[:1500])
        return
    if not (cid and secret and refresh):
        print("SKIPPED: the secrets YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN are not all set, so nothing was uploaded.")
        return
    try:
        token = get_token(cid, secret, refresh)
        check_channel(token, channel)
        url = start_session(token, body, size)
        print("  uploading...", flush=True)
        res = send_video(url, video, token)
        vid = res["id"]
        print(f"UPLOADED: https://www.youtube.com/watch?v={vid} (https://studio.youtube.com/video/{vid}/edit)", flush=True)
        json.dump({"id": vid, "url": f"https://www.youtube.com/shorts/{vid}", "privacy": a.privacy}, open(os.path.join(a.dir, "youtube_result.json"), "w"))
        thumb, srt = os.path.join(a.dir, "thumbnail.jpg"), os.path.join(a.dir, m.get("captionFile", "subtitles.srt"))
        if not a.no_thumbnail and os.path.exists(thumb):
            set_thumbnail(token, vid, thumb)
        if not a.no_captions and os.path.exists(srt):
            add_captions(token, vid, srt, m.get("language", "en"))
        playlist = os.environ.get("YT_PLAYLIST_ID", "").strip()        # optional secret: keeps the series together, people binge it
        if playlist:
            add_to_playlist(token, vid, playlist)
        if m.get("firstComment") and not m.get("selfDeclaredMadeForKids", True):     # comments are switched off on made-for-kids videos
            first_comment(token, vid, m["firstComment"])
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"### Uploaded to YouTube ({a.privacy})\nhttps://studio.youtube.com/video/{vid}/edit\n")
    except YTError as e:
        sys.exit(f"FAIL: {e}")


if __name__ == "__main__":
    main()
