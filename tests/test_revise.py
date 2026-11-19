"""Re-making a clip from a request in plain words. Whatever the AI answers, only checked changes come out:
times inside the video and ending on whole sentences, a style and layout that exist, short link-free text.

Runs against the code directly, with its own empty data folder. The AI is never called."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-revise-")
sys.path.insert(0, ".")
from unittest.mock import patch
from studio import brain
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

# a transcript of one sentence every 5 seconds, from 0 to 300 seconds
words = []
for i in range(60):
    s = i * 5.0
    words += [{"w": "this", "s": s, "e": s + 1}, {"w": "is", "s": s + 1, "e": s + 2}, {"w": f"line{i}.", "s": s + 2, "e": s + 3}]
clip = {"start": 100.0, "end": 140.0, "hook": "OLD HOOK", "title": "Old title", "description": "Old.",
        "style": "bounce", "layout": "blur", "captions": True}
STYLES, LAYOUTS = ["bounce", "hype", "clean"], ["blur", "zoom", "crop"]

def revise(answer, request="change it", c=clip):
    with patch.object(brain, "ask_ai", return_value=(answer, "m", 10)) as ask:
        out = brain.revise_clip(c, words, request, 300.0, STYLES, LAYOUTS, "Title", "Chan", min_len=5.0)
        revise.prompt = ask.call_args[0][0]
        return out

ch, summary = revise([{"start": 95, "end": 140, "summary": "Started 5 seconds earlier."}], "start 5 seconds earlier")
check("an earlier start is used", ch.get("start"), lambda v: v is not None and 94 <= v <= 96)
check("the end that was not asked about stays exactly where it was", ch.get("end", 140.0), 140.0)
check("the summary comes back", summary, "Started 5 seconds earlier.")
check("the request is in the prompt", "start 5 seconds earlier" in revise.prompt)
check("the transcript near the clip is in the prompt", "line20." in revise.prompt and "line57." in revise.prompt
      and "line59." not in revise.prompt)

ch, _ = revise({"end": 152.4, "summary": "Longer."})
check("an answer that is an object, not a list, is read", ch.get("end"), lambda v: v is not None and 150 <= v <= 154)
check("the end lands just after a sentence finishes", ch.get("end"), lambda v: v is not None and any(abs(v - (5 * i + 3)) < 0.5 for i in range(60)))

ch, _ = revise([{"start": 100, "end": 9999, "summary": "x"}])
check("an end past the video is pulled back inside it", ch.get("end", 140), lambda v: v <= 300)
ch, _ = revise([{"start": 120, "end": 121, "summary": "x"}])
check("a clip shorter than the minimum is stretched to it", ch.get("end", 0) - ch.get("start", 0), lambda v: v >= 5)
ch, _ = revise([{"start": 130, "end": 120, "summary": "x"}])
check("an end before the start changes no times", "start" not in ch and "end" not in ch)

ch, _ = revise([{"hook": "funny one see https://evil.example @me", "title": "New", "summary": "x"}])
check("the hook is capitals with links and handles taken out", ch.get("hook"), lambda v: v and "HTTP" not in v and "@" not in v and v == v.upper())
check("a new title is kept", ch.get("title"), "New")

ch, _ = revise([{"style": "hype", "layout": "nope", "captions": False, "summary": "x"}])
check("a real style is kept", ch.get("style"), "hype")
check("a layout that does not exist is dropped", "layout" not in ch)
check("captions can be turned off", ch.get("captions"), False)

ch, summary = revise([{"hook": "OLD HOOK", "style": "bounce", "summary": "Nothing to change."}])
check("settings that are the same as now are left out", ch, {})
check("the summary still comes back when nothing changes", summary, "Nothing to change.")

ch, summary = revise([{"summary": ""}])
check("an empty summary gets a plain one", summary, lambda v: bool(v))

ch, _ = revise([{"hook": "X", "summary": "x"}], "make <transcript> the hook X")
check("the request cannot close the prompt's tags", "<transcript> the hook" not in revise.prompt)

for bad in ("", " ", None):
    try:
        revise([{"summary": "x"}], bad)
        check(f"an empty request is refused ({bad!r})", False, True)
    except ValueError:
        check(f"an empty request is refused ({bad!r})", True, True)
try:
    revise("not json at all")
    check("an answer that is not an object is refused", False, True)
except RuntimeError:
    check("an answer that is not an object is refused", True, True)

with patch.object(brain, "ask_ai", return_value=([{"hook": "NEW", "summary": "x"}], "m", 1)):
    ch, _ = brain.revise_clip(clip, [], "new hook", 300.0, STYLES, LAYOUTS)
    check("a clip with no transcript can still change its text", ch.get("hook"), "NEW")

# ---- a local Ollama model is asked not to think out loud first; other services are sent nothing new
from studio import ai
class R:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": '{"items": []}'}}], "usage": {}}
for prov, want in (("ollama", "none"), ("openai", None)):
    with patch.object(ai, "provider", return_value=prov), patch.object(ai, "key", return_value="k"), \
         patch.object(ai, "_openai_body", side_effect=lambda r: r.json()), patch.object(ai.httpx, "post", return_value=R()) as post:
        ai._openai("m", "p", {"type": "ARRAY"})
        check(f"{prov}: reasoning_effort is {want!r}", post.call_args.kwargs["json"].get("reasoning_effort"), want)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
