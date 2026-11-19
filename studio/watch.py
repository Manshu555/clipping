"""Watch a Twitch channel from the command line: record it, clip every chat spike, and keep watching.

    python -m studio.watch <twitch_channel> [--min 5] [--keep-recording | --no-keep-recording]

(or .\\watch.ps1 <twitch_channel> --min 5, which checks everything is installed first.)

This is not a second clipper. It drives the PeakCut that is running on this computer, through the same steps as the
dashboard: it makes (or updates) a creator for the channel, with chat-spike clipping on, starts the recording, and
prints what happens. The clips are made by PeakCut, kept in its data folder and shown in the dashboard like any
other. If PeakCut is not running, it is started, and stopped again at the end.

While the channel is offline it waits and looks again every minute. When the stream drops, it starts recording
again once the stream is back. Ctrl+C stops the recording; the clips already found are finished first (Ctrl+C
again to leave at once).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
LIVE_EVERY = 60          # seconds between "are they live?" looks while offline
POLL = 3                 # seconds between status updates while recording


def say(msg: str):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- what has to be on this computer
def check_tools() -> list:
    """The programs and Python packages PeakCut needs, with what is missing."""
    from . import tools
    tools.setup()                       # looks for ffmpeg and ffprobe, in PATH and the usual install folders
    problems = [f"{name} is not installed (or not found). {tools.HELP}" for name in tools.missing()]
    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("numpy", "numpy"), ("sherpa_onnx", "sherpa-onnx"),
                     ("yt_dlp", "yt-dlp"), ("soundfile", "soundfile"), ("PIL", "pillow")):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            problems.append(f"The Python package {pkg} is missing: pip install -r requirements.txt")
    return problems


def channel_of(arg: str) -> str:
    m = re.match(r"^(?:https?://)?(?:www\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$", arg.strip()) or \
        re.match(r"^([A-Za-z0-9_]{3,25})$", arg.strip())
    if not m:
        raise Fail(f"'{arg}' is not a Twitch channel name. Use the name from the link, e.g. twitch.tv/NAME.")
    return m.group(1).lower()


def is_live(channel: str) -> bool:
    """Ask Twitch whether the channel is live, without starting a recording (which would leave a failed card in
    the dashboard every minute while waiting). Streamlink when it is installed, else yt-dlp."""
    url = f"https://www.twitch.tv/{channel}"
    try:
        import streamlink
        return bool(streamlink.Streamlink().streams(url))
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - offline, or Twitch did not answer: look again later
        return False
    from . import live
    try:
        live.resolve(url)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- talking to PeakCut
class Studio:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        # PeakCut refuses changes sent by other websites; this is PeakCut's own address, as the dashboard sends
        self.http = httpx.Client(base_url=self.base, timeout=30, headers={"Origin": self.base})
        self.server = None

    def up(self) -> bool:
        try:
            return self.http.get("/healthz", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def ensure_running(self):
        if self.up():
            say(f"PeakCut is running at {self.base}")
            return
        say("Starting PeakCut...")
        log = open(ROOT / "watch-server.log", "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0   # Ctrl+C here must not kill it mid-clip
        self.server = subprocess.Popen([sys.executable, "-m", "studio.server"], cwd=ROOT, stdout=log, stderr=log,
                                       creationflags=flags, start_new_session=os.name != "nt")
        for _ in range(120):
            if self.up():
                say(f"PeakCut started at {self.base} (its output is in watch-server.log)")
                return
            if self.server.poll() is not None:
                raise Fail("PeakCut could not start. See watch-server.log in the PeakCut folder.")
            time.sleep(1)
        raise Fail("PeakCut did not start within two minutes. See watch-server.log in the PeakCut folder.")

    def shutdown(self):
        if self.server and self.server.poll() is None:
            say("Stopping the PeakCut this started...")
            self.server.terminate()
            try:
                self.server.wait(20)
            except subprocess.TimeoutExpired:
                self.server.kill()

    def call(self, method, path, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code == 401 or (r.status_code in (302, 303) and "login" in r.headers.get("location", "")):
            raise Fail("This PeakCut asks people to sign in (PEAKCUT_ACCOUNTS=1). watch.ps1 works with the "
                       "sign-in-free PeakCut on your own computer.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise Fail(str(detail or f"PeakCut answered {r.status_code}"))
        return r.json()

    def creator_for(self, channel: str, minimum: int, keep: bool | None) -> dict:
        """The creator for this channel, made if there is none, with chat-spike clipping on."""
        url = f"https://www.twitch.tv/{channel}"
        want = {"chat_clips": True, "chat_min": minimum}
        if keep is not None:
            want["keep_recording"] = keep
        for c in self.call("GET", "/api/creators"):
            links = [str(c.get("channel") or "")] + [str(u) for u in (c.get("channels") or {}).values()]
            if any(re.search(rf"twitch\.tv/{channel}/?$", u, re.I) for u in links):
                return self.call("PUT", f"/api/creators/{c['id']}", json=want)
        return self.call("POST", "/api/creators", json={"name": channel, "channel": url, "channels": {"twitch": url},
                                                         "keep_recording": bool(keep), **want})

    def recording_of(self, cid: str):
        return next((j for j in self.call("GET", "/api/jobs") if j.get("creator") == cid and j.get("kind") == "live"
                     and j.get("status") == "live"), None)


def clips_folder(jid: str) -> Path:
    """Where PeakCut keeps this recording's clips (the API leaves server paths out on purpose)."""
    from . import config
    # videos live in users/<user>/jobs/<id>, recordings in users/<user>/creators/<creator>/live/<id>
    found = next((p for pat in (f"*/jobs/{jid}", f"*/creators/*/live/{jid}") for p in config.USERS_DIR.glob(pat)), None)
    return (found or config.USERS_DIR / "<user>" / "jobs" / jid) / "clips"


# ---------------------------------------------------------------- one recording, from start to end
def follow(st: Studio, job: dict, stopping: dict) -> dict:
    """Print what happens on this recording until it ends. Returns the last state of the recording.

    What has been printed is kept in `stopping` per recording, so following it again after Ctrl+C (to wait for
    the clips in progress) prints only what is new."""
    mem = stopping.setdefault("seen", {}).setdefault(job["id"], {"clips": {}, "spikes": 0, "chat": None})
    seen, spikes, chat, last = mem["clips"], mem["spikes"], mem["chat"], job
    while True:
        try:
            j = st.call("GET", f"/api/jobs/{job['id']}")
        except (httpx.HTTPError, Fail) as e:
            if not st.up():
                raise Fail("PeakCut stopped answering.") from e
            time.sleep(POLL)
            continue
        last = j
        lv = j.get("live") or {}
        now_on = lv.get("chat_connected")
        if now_on is not None and now_on != chat:
            if now_on:
                say("Chat connected: counting messages in 10-second buckets (spikes count after 3 minutes of history)")
            elif chat:                    # only a connection that was there can drop; at the start it is still coming
                say("Chat disconnected, reconnecting...")
            chat = mem["chat"] = now_on
        if (lv.get("chat_spikes") or 0) > spikes:
            spikes = mem["spikes"] = lv["chat_spikes"]
            say(f"Chat spike #{spikes}: clipping 20 s before to 10 s after")
        for c in j.get("clips", []):
            state = c.get("status")
            if seen.get(c["id"]) == state:
                continue
            seen[c["id"]] = state
            name = c.get("hook") or c.get("title") or c["id"]
            if state == "rendering":
                say(f"Making {c['id']} ({c['end'] - c['start']:.0f} s): {name}")
            elif state == "done":
                extra = f" + {c['srt']}" if c.get("srt") else ""
                say(f"Clip ready: {clips_folder(j['id']) / c['file']}{extra}  [{c.get('reason') or ''}]")
            elif state == "error":
                say(f"Clip {c['id']} failed: {c.get('error')}")
        if j.get("status") != "live":
            busy = [c for c in j.get("clips", []) if c.get("status") in ("queued", "rendering")]
            if not busy or stopping.get("now"):
                return j
        time.sleep(POLL)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watch", description="Record a Twitch channel with PeakCut and clip every chat spike.")
    ap.add_argument("channel", help="the Twitch channel name, or its twitch.tv link")
    ap.add_argument("--min", type=int, default=5, dest="minimum",
                    help="fewest chat messages in 10 seconds that can count as a spike (default 5)")
    keep = ap.add_mutually_exclusive_group()
    keep.add_argument("--keep-recording", dest="keep", action="store_true", default=None,
                      help="also keep the whole stream in rec/ in PeakCut's data folder (the default for watch)")
    keep.add_argument("--no-keep-recording", dest="keep", action="store_false", help="keep only the rolling buffer")
    ap.add_argument("--url", default=os.environ.get("PEAKCUT_WATCH_URL") or
                    f"http://127.0.0.1:{os.environ.get('PEAKCUT_PORT') or 8765}", help="where PeakCut is running")
    a = ap.parse_args(argv)
    if a.minimum < 1:
        ap.error("--min must be 1 or more")
    keep_rec = True if a.keep is None else a.keep
    st = Studio(a.url)
    stopping = {"now": False}
    job = None
    try:
        channel = channel_of(a.channel)
        problems = check_tools()
        if problems:
            raise Fail("\n  ".join(["Something PeakCut needs is missing:"] + problems))
        try:
            import streamlink  # noqa: F401
            say("Streamlink found: Twitch is recorded through it")
        except ImportError:
            say("Streamlink is not installed: recording through yt-dlp instead (pip install -r requirements-twitch.txt)")
        st.ensure_running()
        c = st.creator_for(channel, a.minimum, keep_rec)
        say(f"Creator '{c['name']}': clipping chat spikes of {a.minimum}+ messages per 10 s, at 2.5x the 3-minute median"
            + (", keeping the whole recording in rec/" if keep_rec else ""))
        waiting = False
        while True:
            job = st.recording_of(c["id"])
            if not job:
                if not is_live(channel):
                    if not waiting:
                        say(f"{channel} is offline. Looking again every minute (Ctrl+C to stop).")
                        waiting = True
                    time.sleep(LIVE_EVERY)
                    continue
                waiting = False
                try:
                    job = st.call("POST", f"/api/creators/{c['id']}/live", json={"channel": f"https://www.twitch.tv/{channel}"})
                except Fail as e:
                    say(f"Could not start recording: {e}. Trying again in a minute.")
                    time.sleep(LIVE_EVERY)
                    continue
            say(f"Recording {channel} (dashboard card: {job.get('title') or job['id']})")
            end = follow(st, job, stopping)
            clips = [k for k in end.get("clips", []) if k.get("status") == "done"]
            stage = end.get("stage") or end.get("status") or ""
            job = None
            if stage.startswith("Stopped by you"):
                # Stop was pressed (in the dashboard, or by another watch): that is a decision, not a dropped
                # stream, so this does not start recording again behind your back.
                say(f"Recording stopped from PeakCut: {len(clips)} clip(s). Leaving.")
                st.shutdown()
                return 0
            say(f"Recording ended ({stage}): {len(clips)} clip(s). Waiting for {channel} to come back.")
    except KeyboardInterrupt:
        say("Stopping...")
        try:
            if job:
                try:
                    st.call("POST", f"/api/live/{job['id']}/stop")
                except Fail:
                    pass                      # it had already ended
                say("Finishing the clips already found (Ctrl+C again to leave now)...")
                try:
                    end = follow(st, job, stopping)
                    clips = [k for k in end.get("clips", []) if k.get("status") == "done"]
                    say(f"Done: {len(clips)} clip(s) from this recording. They are in the PeakCut dashboard.")
                except KeyboardInterrupt:
                    say("Left without waiting. Clips still being made will finish when PeakCut next runs.")
        finally:
            st.shutdown()
        return 0
    except Fail as e:
        say(str(e))
        st.shutdown()
        return 1
    except Exception:
        st.shutdown()
        raise


if __name__ == "__main__":
    sys.exit(main())
