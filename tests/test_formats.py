"""Posting to YouTube as a Short or as a normal video. A normal video is a 16:9 copy of the clip, because YouTube
makes anything vertical and under three minutes a Short. Each post remembers which it is.

Runs against the code directly, with its own empty data folder. Nothing is uploaded and no video is made."""
import os, re, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-formats-")
sys.path.insert(0, ".")
from pathlib import Path
from unittest.mock import patch
from studio import accounts, creators, ctx, posting, render
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

# ---- the text of a 16:9 copy is laid out on a 16:9 canvas, centred on it
words = [{"w": "hello", "s": 1.0, "e": 1.4}, {"w": "there", "s": 1.4, "e": 1.9}]
tall = render.build_ass(words, 0, 5, "BIG NEWS", "bounce", [], 300, 1380)
wide = render.build_ass(words, 0, 5, "BIG NEWS", "bounce", [], render.WIDE_HOOK_Y, render.WIDE_CAP_Y, render.WIDE_PW, render.WIDE_PH)
check("the vertical clip keeps its 1080x1920 canvas", "PlayResX: 1080\nPlayResY: 1920" in tall)
check("the wide copy uses a 16:9 canvas", f"PlayResX: {render.WIDE_PW}\nPlayResY: {render.WIDE_PH}" in wide)
check("the wide canvas really is 16:9", abs(render.WIDE_PW / render.WIDE_PH - 16 / 9) < 0.01)
check("the heading is centred on the wide canvas", f"\\pos({render.WIDE_PW // 2}," in wide)
xs = [int(x) for x in re.findall(r"\\pos\((\d+),\d+\)\\fs", wide)]
check("the captions sit around the middle of the wide canvas", xs, lambda v: v and all(abs(x - render.WIDE_PW / 2) < 700 for x in v))
check("the vertical captions are where they were", [int(x) for x in re.findall(r"\\pos\((\d+),\d+\)\\fs", tall)],
      lambda v: v and all(abs(x - 540) < 500 for x in v))
check("the wide video is 1920x1080, with bars only if needed", render._filter("wide", 1080),
      lambda f: "scale=1920:1080:force_original_aspect_ratio=decrease" in f and "pad=1920:1080" in f)

# ---- the choice is checked, remembered on each post, and a Short and a video are two different posts
check("a creator's YouTube choice defaults to Shorts", creators.DEFAULTS.get("yt_format"), "short")
check("a creator can choose normal videos", creators._clean({**creators.DEFAULTS, "yt_format": "video"})["yt_format"], "video")
check("anything else falls back to Shorts", creators._clean({**creators.DEFAULTS, "yt_format": "film"})["yt_format"], "short")

yt = {"id": "yt_1", "platform": "youtube", "name": "Chan"}
ig = {"id": "ig_1", "platform": "instagram", "name": "@me"}
job = {"id": "j1", "creator": None}
clip = {"id": "clip01", "title": "T", "score": 80}
posting._posts.clear()
with ctx.as_user("u_test"), patch.object(accounts, "get", side_effect=lambda a: {"yt_1": yt, "ig_1": ig}.get(a)), \
     patch.object(posting, "_mirror"), patch.object(posting, "_save"):
    made = posting.enqueue(job, clip, ["yt_1", "ig_1"])
    check("with no choice a YouTube post is a Short", made[0]["yt_format"], "short")
    check("an Instagram post has no YouTube choice", made[1]["yt_format"], None)
    made = posting.enqueue(job, clip, ["yt_1"], yt_format="video")
    check("the same clip can also go up as a normal video", [p["yt_format"] for p in made], ["video"])
    made = posting.enqueue(job, clip, ["yt_1"], yt_format="video")
    check("but not as a normal video twice", made, [])
    made = posting.enqueue(job, clip, ["yt_1"], yt_format="short")
    check("nor as a Short twice", made, [])
    made = posting.enqueue(dict(job, id="j2"), clip, ["yt_1"], yt_format="sideways")
    check("an unknown choice becomes a Short", [p["yt_format"] for p in made], ["short"])

# ---- posting: a normal video sends the 16:9 copy without #Shorts, a Short sends the clip with it
sent = []
def fake_upload(acc, path, title, desc, tags, privacy, short=True):
    sent.append({"path": Path(path).name, "desc": desc, "short": short}); return "vid", "url"
jobrec = {"id": "j9", "creator": None, "dir": tempfile.mkdtemp(), "clips": [{"id": "clip01", "status": "done", "file": "clip01.mp4"}]}
for fmt in ("video", "short"):
    p = {"id": "p" + fmt, "job": "j9", "clip": "clip01", "account": "yt_1", "account_name": "Chan", "platform": "youtube",
         "creator": None, "title": "T", "tries": 0, "yt_format": fmt}
    with patch.object(posting.jobs, "get", return_value=jobrec), patch.object(accounts, "get", return_value=yt), \
         patch.object(posting.jobs, "wide_file", return_value=Path("clip01_wide.mp4")), \
         patch.object(accounts, "youtube_upload", side_effect=fake_upload), patch.object(posting, "_save"), patch.object(posting, "_mirror"):
        posting._post(p)
    check(f"{fmt}: the post went up", p.get("status"), "posted")
check("a normal video uploads the 16:9 copy", sent[0]["path"], "clip01_wide.mp4")
check("a normal video is not tagged #Shorts", "#Shorts" not in sent[0]["desc"] and sent[0]["short"] is False)
check("a Short uploads the vertical clip with #Shorts", sent[1]["path"] == "clip01.mp4" and "#Shorts" in sent[1]["desc"])

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
