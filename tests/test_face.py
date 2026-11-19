"""Keeping the streamer's face in the clip (the split layout), how loud moments are judged live, and the language a
live stream is written down in.

Runs against the code directly, with its own empty data folder. No video is made and no AI is called."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-face-")
sys.path.insert(0, ".")
import collections, threading
from unittest.mock import patch
import numpy as np
from studio import creators, facecam, jobs, live, render
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

# ---- which layout a clip is made in
cam = [1013, 54, 267, 179]
check("split is a layout people can pick", "split" in render.LAYOUTS)
for lay in ("blur", "zoom", "crop"):
    check(f"{lay}: with a webcam and keep-face on, the face goes on top", render.face_layout(lay, cam, True), "split")
    check(f"{lay}: with keep-face off, the chosen layout stays", render.face_layout(lay, cam, False), lay)
    check(f"{lay}: without a webcam, the chosen layout stays", render.face_layout(lay, None, True), lay)
check("gameplay (facecam in the corner) is kept as chosen", render.face_layout("gameplay", cam, True), "gameplay")
check("a speck of a box is not a webcam", render.face_layout("crop", [0, 0, 4, 4], True), "crop")
check("keep-face is on for videos by default", jobs.DEFAULTS["keep_face"], True)
check("keep-face is on for creators by default", creators.DEFAULTS["keep_face"], True)
check("keep-face must be on or off", jobs.clean_options({"keep_face": False}), {"keep_face": False})
try:
    jobs.clean_options({"keep_face": "yes"})
    check("keep-face 'yes' is refused", False, True)
except ValueError:
    check("keep-face 'yes' is refused", True, True)

# ---- the split picture: face on top at its own shape, the game below, the full 1080x1920
check("a 16:9 webcam gets a 608 px face panel", render.split_top([0, 0, 1600, 900]), 608)
check("a very wide webcam is not squeezed thinner than 540 px", render.split_top([0, 0, 1000, 200]), 540)
check("a tall webcam is not given more than 860 px", render.split_top([0, 0, 300, 600]), 860)
f = render._filter("split", 608, cam)
check("the face is cut from where the webcam is", "crop=267:179:1013:54" in f)
check("face and game are stacked into the full frame",
      all(s in f for s in ("scale=1080:608", "scale=1080:1312", "vstack=inputs=2")))
check("split without a webcam does not stack anything", "vstack" not in render._filter("blur", 1000, None))

# ---- a webcam the AI pointed at is grown to the still picture around it, and no further
still = np.full((facecam.H, facecam.W), 40.0, dtype=np.float32)      # the game: everything moves
still[10:50, 250:320] = 2.0                                          # the webcam, top right: hardly moves
reg = facecam._still_region(still, (0.85, 0.08, 0.08, 0.1), 40.0)   # a rough box, inside the webcam
check("a rough box grows to the whole still webcam", reg, (250.0, 10.0, 320.0, 50.0))
check("a box on the moving game finds nothing", facecam._still_region(still, (0.2, 0.5, 0.1, 0.1), 40.0), None)

# ---- live: loud moments are judged with the best moment asked for, and with room for the clip
def session(**settings):
    s = live.Session.__new__(live.Session)
    s.lock = threading.RLock()
    s.settings = dict(creators.DEFAULTS, **settings)
    s.job = {"id": "j", "creator": None, "clips": [], "title": "T", "uploader": "U", "ai": {}}
    s.words = [{"w": f"word{i}", "s": float(i), "e": i + 0.5} for i in range(0, 400)]
    s.db, s.chat, s.chat_users = [-30.0] * 400, collections.Counter(), {}
    s.chat_on = s.chat_total = 0
    s.t_done, s.verdicts, s.checks, s.tokens, s.skipped, s.rejected = 400.0, [], 0, 0, 0, 0
    s.started_at, s.game, s.last_check, s.ended = 0.0, "", 0.0, False
    return s

s = session(max_len=50)
check("a jump with 35-50 s clips gets a window that can hold 50 s", s.jump_window(300.0), (265.0, 320.0))
s = session(max_len=20)
check("short clips keep the half minute before a jump", s.jump_window(300.0), (270.0, 320.0))

asked = {}
def judge(*a, **k):
    asked.update(must=k.get("must"))
    return [], 10
for why, want in (("Jump: loudness +457%", True), (None, False), ('Someone said "clip"', False)):
    s = session()
    with patch.object(live.brain, "judge_transcript", side_effect=judge), patch.object(live.usage, "set_context"):
        s._ai_check((250.0, 320.0) if why else None, why)
    check(f"{why or 'a scheduled check'}: asked for the best moment = {want}", asked.get("must"), want)

# ---- a webcam the AI confirmed counts as sure, even when the streamer never stops moving
busy = np.full((facecam.H, facecam.W), 60.0, dtype=np.float32)     # a fast game
busy[80:120, 0:65] = 20.0                                           # a webcam, left: moves, but less than the game
with patch.object(facecam, "_snap", side_effect=lambda path, box, w, h, span=0.0, at=0.5: box):
    sure = facecam._finish("x", {"x": 0, "y": 80, "x1": 65, "y1": 120}, 320, 180, busy, seen=True)
    measured = facecam._finish("x", {"x": 0, "y": 80, "x1": 65, "y1": 120}, 320, 180, busy)
check("an AI-confirmed webcam of a lively streamer is sure", sure["conf"], "high")
check("the same box found by measuring alone is not", measured["conf"], "low")

# ---- the face found late: the clips made before it are made again with the face on top
s = session()
s.facecam_box = [0, 306, 258, 162]
done_clip = {"id": "clip001", "status": "done", "layout": "blur", "src": __file__}
gameplay = {"id": "clip002", "status": "done", "layout": "gameplay", "src": __file__}
now_clip = {"id": "clip003", "status": "rendering", "layout": "blur", "src": __file__}
s.job["clips"] = [done_clip, gameplay, now_clip]
with patch.object(live.jobs, "request_render") as rr:
    s._face_found_late(now_clip)
check("an earlier clip is made again with the face on top", [c.args for c in rr.call_args_list], [("j", "clip001", {"layout": "split"})])
check("...and knows where the face is", done_clip.get("facecam"), [0, 306, 258, 162])

# ---- videos in another language are written down by Whisper; the language chosen beats a guess
from studio import fastwhisper
tmpd = __import__("pathlib").Path(tempfile.mkdtemp())
speech = np.full(16000 * 60, 2000, dtype=np.int16)
said = [{"w": f"शब्द{i}", "s": float(i), "e": i + 0.5} for i in range(40)]
def run(lang_opt, heard="hi"):
    job = {"options": {"lang": lang_opt}, "progress": 0.0}
    with patch.object(fastwhisper, "available", return_value=True), patch.object(fastwhisper, "free_the_card"), \
         patch.object(fastwhisper, "language", return_value=heard) as guess, \
         patch.object(fastwhisper, "transcribe", return_value=list(said)) as tr, patch.object(jobs, "_set"):
        (tmpd / "whisper.json").unlink(missing_ok=True)
        out = jobs._whisper_transcript(job, tmpd, speech, 100.0)
    return out, guess.call_count, tr.call_args, job
out, guessed, call, job = run("hi", heard="en")
check("Hindi chosen: Whisper writes it in Hindi, with no guessing", (out and out[1], guessed, call and call.args[1]), ("hi", 0, "hi"))
check("...the words are put on the video's clock", out[0][0]["s"], 100.0)
check("...and the video says where its transcript came from", (job.get("transcript_from"), job.get("lang")), ("whisper", "hi"))
out, guessed, call, _ = run("en")
check("English chosen: PeakCut's own model, Whisper not used", (out, call), (None, None))
out, guessed, call, _ = run("auto", heard="en")
check("found out to be English: PeakCut's own model", (out, guessed), (None, 1))
out, guessed, call, _ = run("auto", heard="hi")
check("found out to be Hindi: Whisper", out and out[1], "hi")
check("a creator's language becomes their videos' language", creators.DEFAULTS["speech_lang"] == "en" and jobs.clean_options({"lang": "hi"}) == {"lang": "hi"})

# ---- a moment picked near the end of what is recorded grows backwards to the shortest clip, payoff kept last
from studio import brain
ws_ = [{"w": f"w{i}.", "s": 245.0 + i * 2.5, "e": 245.0 + i * 2.5 + 2.0} for i in range(22)]   # a sentence every 2.5 s
s_, e_ = brain._snap({"start": 286.3, "end": 288.2}, brain.make_lines(ws_), ws_, 35, 50, 300.0)
check("a 2 s pick near the live edge becomes a 35 s clip, not nothing", round(e_ - s_), lambda v: 35 <= v <= 42)
check("...that still ends on the moment the AI picked", e_, lambda v: 287.5 <= v <= 290.0)
s_, e_ = brain._snap({"start": 100.0, "end": 102.0}, brain.make_lines(ws_), ws_, 35, 50, 400.0)
check("with room after it, a pick still grows forward as before", s_, lambda v: 99.0 <= v <= 100.0)

prompts = []
with patch.object(brain, "ask_ai", side_effect=lambda p, **k: (prompts.append(p), ([], "m", 1))[1]):
    lines_ = [{"w": f"word{i}.", "s": float(i), "e": i + 0.5} for i in range(60)]
    brain.judge_transcript(lines_, 0, 60, must=True)
    brain.judge_transcript(lines_, 0, 60, must=False)
check("a check that must pick a moment is not also told to return nothing",
      "Return [] if nothing is worth clipping" not in prompts[0] and "even if it is only decent" in prompts[0])
check("an ordinary check may still return nothing", "Return [] if nothing is worth clipping" in prompts[1])

# ---- the language a live stream is written down in
s = session()
s.lang, s.lang_votes, s.lang_pieces = None, {}, 0
with patch.object(live.subtitles, "language_votes") as votes:
    got = s._language(np.full(16000, 3000, dtype=np.int16))
check("streams are English by default, with no Whisper call", (got, votes.call_count), ("en", 0))
check("the default is English for creators", creators.DEFAULTS["speech_lang"], "en")
s = session(speech_lang="hi")
s.lang, s.lang_votes, s.lang_pieces = None, {}, 0
check("a language set in Setup is used as it is", s._language(np.zeros(16000, dtype=np.int16)), "hi")
s = session(speech_lang="auto")
s.lang, s.lang_votes, s.lang_pieces = None, {}, 0
answers = iter([{"en": 0.6, "hi": 0.4}] + [{"hi": 0.8, "en": 0.2}] * 5)
loud = np.full(16000, 3000, dtype=np.int16)
with patch.object(live.subtitles, "whisper_installed", return_value=True), \
     patch.object(live.subtitles, "language_votes", side_effect=lambda a: next(answers)), patch.object(live.creators, "log"):
    first = s._language(loud)
    for _ in range(5):
        last = s._language(loud)
check("auto: English is used until the language is decided", first, "en")
check("auto: several pieces vote, and the language most heard wins", (last, s.job.get("lang")), ("hi", "hi"))
check("languages are checked", (creators._clean({"speech_lang": "HI"})["speech_lang"], creators._clean({"speech_lang": "x y"})["speech_lang"]),
      ("hi", "en"))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
