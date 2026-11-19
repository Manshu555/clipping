"""Twitch: chat spikes, the clips they make, .srt subtitles and the watch command.

Runs against the code directly, with its own empty data folder. Twitch, Whisper and the AI are never called."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-twitch-")
sys.path.insert(0, ".")
from pathlib import Path
from unittest.mock import patch
from studio import creators, live, subtitles, watch
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

# ---- the spike rule: 10-second buckets, 2.5x the median of the 3 minutes before, and a minimum
calm = [4] * 18
check("chat connection is the anonymous one asked for", (live.CHAT_HOST, live.CHAT_PORT, live.CHAT_NICK),
      ("irc.chat.twitch.tv", 6667, "justinfan12345"))
check("3 minutes of 10-second buckets is the history", live.CHAT_HISTORY * live.CHAT_BUCKET, 180)
check("2.5x the median is a spike", live.chat_spike(calm, 10, 5), 2.5)
check("just under 2.5x is not", live.chat_spike(calm, 9, 5), None)
check("a spike under the minimum is not", live.chat_spike([1] * 18, 4, 5), None)
check("the minimum alone counts in a silent chat", live.chat_spike([0] * 18, 5, 5), 5.0)
check("a higher minimum is respected", live.chat_spike([0] * 18, 7, 8), None)
check("less than 3 minutes of history is not judged yet", live.chat_spike([4] * 17, 50, 5), None)
check("the median, not the average: one earlier burst doesn't raise the bar",
      live.chat_spike([2] * 17 + [200], 6, 5), 3.0)
check("only the last 3 minutes are compared", live.chat_spike([100] * 30 + [2] * 18, 6, 5), 3.0)

# ---- a session: chat counted on the stream clock becomes clips from 20 s before to 10 s after a spike
def session(chat_clips=True, minimum=5):
    s = live.Session.__new__(live.Session)
    import threading, collections
    s.lock = threading.RLock()
    s.settings = dict(creators.DEFAULTS, chat_clips=chat_clips, chat_min=minimum)
    s.job = {"creator": None, "clips": []}
    s.chat, s.manual = collections.Counter(), []
    s.chat_since, s.chat_next, s.last_chat_clip, s.chat_spikes = 0, None, -999.0, 0
    s.samples = 0
    return s

def feed(s, per_bucket: dict, until: int):
    """per_bucket: bucket index -> messages (others get 3). Runs the watcher as the stream reaches `until` s."""
    for b in range(until // 10):
        n = per_bucket.get(b, 3)
        for i in range(n):
            s.chat[b * 10 + i % 10] += 1
    s.samples = until * live.SR
    with patch.object(live.creators, "log"):
        s._watch_chat_clips()

s = session()
feed(s, {30: 20}, 320)
check("one spike gives one clip", len(s.manual), 1)
check("the clip runs from 20 s before to 10 s after the spike",
      (s.manual[0]["start"], s.manual[0]["end"]) if s.manual else None, (280.0, 310.0))
check("the reason says it was chat", s.manual[0]["why"] if s.manual else "", lambda v: v.startswith("Chat spike: 20 messages"))

s = session()
feed(s, {30: 20, 31: 25, 32: 22}, 340)
check("a chat that stays wild for 30 s gives one clip, not three", len(s.manual), 1)

s = session()
feed(s, {30: 20, 40: 30}, 420)
check("two separate spikes give two clips", len(s.manual), 2)

s = session()
feed(s, {30: 20}, 320)
feed(s, {}, 330)          # the watcher runs again: buckets already looked at are not looked at again
check("a bucket is only judged once", len(s.manual), 1)

s = session()
s.job["clips"] = [{"start": 278.0, "end": 312.0}]
feed(s, {30: 20}, 320)
check("a spike on top of a clip already made is left out", len(s.manual), 0)

s = session(chat_clips=False)
feed(s, {30: 20}, 320)
check("nothing happens unless chat clipping is on", len(s.manual), 0)

s = session(minimum=50)
feed(s, {30: 20}, 320)
check("the creator's minimum is used", len(s.manual), 0)

s = session()
s.chat_since = 200        # chat reconnected at 200 s: the quiet while it was away is not history
feed(s, {25: 20}, 260)
check("after a reconnect, 3 minutes of fresh history are needed first", len(s.manual), 0)

s = session()
s.chat_since = None
feed(s, {30: 20}, 320)
check("no chat connection, no chat clips", len(s.manual), 0)

check("chat clipping is off for creators by default", (creators.DEFAULTS["chat_clips"], creators.DEFAULTS["chat_min"]), (False, 5))
check("the minimum is kept within limits", creators._clean({"chat_min": 0, "chat_clips": 1, "keep_recording": ""}),
      {"chat_min": 1, "chat_clips": True, "keep_recording": False})

# ---- .srt subtitles
check("SRT times", subtitles._ts(3725.5), "01:02:05,500")
srt = subtitles.to_srt([(0, 1.5, "hello there"), (1.5, 1.5, "empty"), (2, 3, "  "), (3, 4.25, "bye")])
check("SRT is numbered from 1 and skips empty cues", srt, "1\n00:00:00,000 --> 00:00:01,500\nhello there\n\n2\n00:00:03,000 --> 00:00:04,250\nbye\n")
words = [{"w": "Oh", "s": 100.0, "e": 100.3}, {"w": "my", "s": 100.3, "e": 100.5}, {"w": "god.", "s": 100.5, "e": 101.0},
         {"w": "Look", "s": 103.0, "e": 103.4}, {"w": "at", "s": 103.4, "e": 103.6}, {"w": "this", "s": 103.6, "e": 104.0},
         {"w": "late", "s": 131.0, "e": 131.5}]
cues = subtitles.cues_from_words(words, 99.0, 130.0)
check("own-transcript cues are on the clip's clock", [round(c[0], 1) for c in cues], [1.0, 4.0])
check("own-transcript cues break at sentence ends and pauses", [c[2] for c in cues], ["Oh my god.", "Look at this"])
check("words after the clip are left out", all("late" not in c[2] for c in cues))
long = [{"w": "word", "s": 10 + i * 0.3, "e": 10.25 + i * 0.3} for i in range(30)]
check("no subtitle line is longer than 42 characters", subtitles.cues_from_words(long, 10, 30), lambda cs: cs and all(len(c[2]) <= 42 for c in cs))

tmp = Path(tempfile.mkdtemp())
clip = tmp / "clip01.mp4"
clip.write_bytes(b"")
with patch.object(subtitles, "whisper_installed", return_value=True), \
     patch.object(subtitles, "_whisper_cues", return_value=[(0.0, 2.0, " Whisper heard this")]):
    got = subtitles.write(clip, words, 99.0, 130.0)
check("Whisper writes the .srt when it is there", (got, (tmp / "clip01.srt").read_text(encoding="utf-8")),
      ({"file": "clip01.srt", "from": "whisper"}, "1\n00:00:00,000 --> 00:00:02,000\nWhisper heard this\n"))
with patch.object(subtitles, "whisper_installed", return_value=True), \
     patch.object(subtitles, "_whisper_cues", side_effect=RuntimeError("out of memory")):
    got = subtitles.write(clip, words, 99.0, 130.0)
check("if Whisper fails, the own transcript is used", got, {"file": "clip01.srt", "from": "own"})
check("...and the file has its words", "Oh my god." in (tmp / "clip01.srt").read_text(encoding="utf-8"))
with patch.object(subtitles, "whisper_installed", return_value=False):
    check("without Whisper, the own transcript is used", subtitles.write(clip, words, 99.0, 130.0)["from"], "own")
with patch.object(subtitles, "ENGINE", "off"):
    check("subtitles can be switched off", subtitles.write(clip, words, 99.0, 130.0), {})

# ---- the watch command
check("a channel name is read", watch.channel_of("Shroud"), "shroud")
check("a twitch.tv link is read", watch.channel_of("https://www.twitch.tv/Some_One/"), "some_one")
for bad in ("", "a", "not a name", "https://youtube.com/@x", "x" * 30):
    try:
        watch.channel_of(bad)
        check(f"'{bad[:20]}' is refused", False, True)
    except watch.Fail:
        check(f"'{bad[:20]}' is refused", True, True)
try:
    watch.main(["shroud", "--min", "0"])
    check("--min 0 is refused", False, True)
except SystemExit as e:
    check("--min 0 is refused", e.code, 2)

class FakeStudio(watch.Studio):
    def __init__(self, rows):
        self.rows, self.sent = rows, []
    def call(self, method, path, **kw):
        self.sent.append((method, path, kw.get("json")))
        return self.rows if method == "GET" else {"id": "new", "name": "x", **(kw.get("json") or {})}
st = FakeStudio([{"id": "c1", "channel": "https://www.twitch.tv/shroud", "channels": {}}])
st.creator_for("shroud", 8, True)
check("an existing creator for the channel is updated, not duplicated", st.sent[-1],
      ("PUT", "/api/creators/c1", {"chat_clips": True, "chat_min": 8, "keep_recording": True}))
st = FakeStudio([{"id": "c1", "channel": "https://www.twitch.tv/shroudfan", "channels": {}}])
st.creator_for("shroud", 5, False)
check("a different channel that starts the same is not taken for it", st.sent[-1][0:2], ("POST", "/api/creators"))
check("a new creator has chat clipping on", st.sent[-1][2], lambda b: b["chat_clips"] is True and b["chat_min"] == 5
      and b["channel"] == "https://www.twitch.tv/shroud" and b["keep_recording"] is False)

# ---- Stop pressed in PeakCut is a decision: the watcher leaves instead of starting a new recording
calls = []
with patch.object(watch, "check_tools", return_value=[]), patch.object(watch.Studio, "ensure_running"), \
     patch.object(watch.Studio, "shutdown"), patch.object(watch, "say"), \
     patch.object(watch.Studio, "creator_for", return_value={"id": "c1", "name": "x"}), \
     patch.object(watch.Studio, "recording_of", return_value={"id": "j1"}), \
     patch.object(watch.Studio, "call", side_effect=lambda *a, **k: calls.append(a)), \
     patch.object(watch, "is_live", return_value=True), \
     patch.object(watch, "follow", return_value={"stage": "Stopped by you", "clips": [{"status": "done"}]}):
    code = watch.main(["shroud"])
check("a recording stopped from PeakCut makes the watcher leave", code, 0)
check("...without starting a new recording", [c for c in calls if c[0] == "POST"], [])

ends = iter([{"stage": "Stream ended", "clips": []}, {"stage": "Stopped by you", "clips": []}])
with patch.object(watch, "check_tools", return_value=[]), patch.object(watch.Studio, "ensure_running"), \
     patch.object(watch.Studio, "shutdown"), patch.object(watch, "say"), \
     patch.object(watch.Studio, "creator_for", return_value={"id": "c1", "name": "x"}), \
     patch.object(watch.Studio, "recording_of", return_value={"id": "j1"}), \
     patch.object(watch, "follow", side_effect=lambda *a: next(ends)) as fol:
    watch.main(["shroud"])
check("a stream that ends (or drops) is waited for and recorded again", fol.call_count, 2)

with patch.object(watch.config if hasattr(watch, "config") else __import__("studio.config", fromlist=["x"]), "USERS_DIR",
                  Path(tempfile.mkdtemp())) as users:
    (users / "u1" / "creators" / "c1" / "live" / "jlive").mkdir(parents=True)
    (users / "u1" / "jobs" / "jvid").mkdir(parents=True)
    check("a recording's clips folder is found", watch.clips_folder("jlive"), users / "u1" / "creators" / "c1" / "live" / "jlive" / "clips")
    check("a video's clips folder is found", watch.clips_folder("jvid"), users / "u1" / "jobs" / "jvid" / "clips")

# ---- following a recording again after Ctrl+C prints only what is new
said = []
done_job = {"id": "j9", "status": "done", "live": {"chat_connected": True, "chat_spikes": 1},
            "clips": [{"id": "clip001", "status": "done", "file": "clip001.mp4", "start": 0, "end": 30}]}
mem = {}
with patch.object(watch, "say", side_effect=said.append), patch.object(watch, "clips_folder", return_value=Path("c")):
    st = FakeStudio([])
    st.call = lambda *a, **k: done_job
    watch.follow(st, {"id": "j9"}, mem)
    first = len(said)
    watch.follow(st, {"id": "j9"}, mem)
check("the first follow prints the clip, chat and spike", first, 3)
check("following again after Ctrl+C repeats nothing", len(said), first)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
