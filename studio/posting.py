"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import re
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")
YT_FORMATS = ("short", "video")
IG_FORMATS = ("reel", "story", "both")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error", "yt_format", "ig_format", "format")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False, yt_format=None, ig_format="reel") -> list:
    """`yt_format` is how a clip goes to YouTube: "short" (vertical, a Short) or "video" (a normal 16:9 video).
    `ig_format` is how a clip goes to Instagram: "reel", "story", or "both"."""
    c = creators.get(job.get("creator")) or creators.DEFAULTS
    yt_format = yt_format if yt_format in YT_FORMATS else (c.get("yt_format") if c.get("yt_format") in YT_FORMATS else "short")
    ig_format = ig_format if ig_format in IG_FORMATS else "reel"
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if acc["platform"] == "youtube":
                formats = [yt_format]
            elif acc["platform"] == "instagram":
                formats = ["reel", "story"] if ig_format == "both" else [ig_format]
            else:
                formats = [None]
            for fmt in formats:
                if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                       and (p.get("format") or p.get("yt_format") or ("short" if p.get("platform") == "youtube" else "reel")) == fmt
                       and p["status"] in OPEN + ("posted",) for p in _mine()):
                    continue  # never post the same clip to the same account and format twice
                due = time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
                acc_label = f"{acc['name']} (Story)" if fmt == "story" else (f"{acc['name']} (Short)" if fmt == "short" and acc["platform"] == "youtube" else acc["name"])
                p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job.get("creator"),
                     "account": aid, "account_name": acc_label, "platform": acc["platform"], "status": "scheduled",
                     "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                     "score": clip.get("score", 0), "tries": 0,
                     "yt_format": fmt if acc["platform"] == "youtube" else None,
                     "ig_format": fmt if acc["platform"] == "instagram" else None,
                     "format": fmt, "url": "", "remote_id": "", "error": None, "views": None}
                _posts.append(p)
                made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def enqueue_all(job, clip, now=True) -> list:
    """Master Key: Queues the clip everywhere at once: YouTube Short + Instagram Reel + Instagram Story + TikTok."""
    all_accs = accounts.list_accounts()
    made = []
    # 1. YouTube Short
    yt_accs = [a["id"] for a in all_accs if a["platform"] == "youtube"]
    if yt_accs:
        made += enqueue(job, clip, yt_accs, now=now, yt_format="short")
    # 2. Instagram Reel + Story
    ig_accs = [a["id"] for a in all_accs if a["platform"] == "instagram"]
    if ig_accs:
        made += enqueue(job, clip, ig_accs, now=now, ig_format="both")
    # 3. TikTok
    tt_accs = [a["id"] for a in all_accs if a["platform"] == "tiktok"]
    if tt_accs:
        made += enqueue(job, clip, tt_accs, now=now)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    # Strict guard: NEVER auto-upload clips generated without AI, clips with score <= 0, or clips missing a title/hook
    if clip.get("score", 0) <= 0 or not (clip.get("title") or clip.get("hook")):
        return

    u = job.get("user") or "u_local"
    c = creators.get(job.get("creator"))

    with ctx.as_user(u):
        st = accounts.settings()
        if st.get("paused"):
            return
        auto_master = st.get("auto_master_post", True)
        if auto_master:
            made = enqueue_all(job, clip, now=True)
            if made:
                tag = (c and c.get("id")) or "system"
                creators.log(tag, f"⚡ Auto Master-Posted ({len(made)} destinations): {clip.get('title') or clip.get('hook')}")
                return
        min_score = c.get("post_min_score", 50) if c else 50
        if clip.get("score", 0) < min_score:
            return  # includes hand-made clips (score 0): those wait for you to press Post
        if c and c.get("autopost") and c.get("accounts"):
            made = enqueue(job, clip, c["accounts"])
            if made:
                creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


CREATOR_HANDLES = {
    "ishowspeed": "@ishowspeed",
    "speed": "@ishowspeed",
    "carryminati": "@carryminati",
    "carryislive": "@carryminati",
    "carry": "@carryminati",
    "mrbeast": "@mrbeast",
    "liveinsaan": "@triggeredinsaan",
    "triggeredinsaan": "@triggeredinsaan",
    "lirik": "@lirik",
    "jusjacklive": "@jusjacklive",
}


def creator_handle(creator: dict | None, blob: str = "") -> str:
    blob_lower = f"{blob} {(creator or {}).get('name', '')} {(creator or {}).get('id', '')}".lower()
    for key, handle in CREATOR_HANDLES.items():
        if key in blob_lower:
            return handle
    if creator:
        for field in ("handle", "instagram", "social_handle", "user_tag"):
            val = str(creator.get(field) or "").strip()
            if val:
                return val if val.startswith("@") else f"@{val}"
        raw_name = str(creator.get("name") or "").strip()
        clean = re.sub(r"\W", "", raw_name)
        if clean and clean.lower() not in ("clips", "clip", "user", "local"):
            return f"@{clean}"
    return ""


def _caption(clip, creator, is_short: bool = True):
    import re
    title = (clip.get("title") or clip.get("hook") or "Clip").strip()
    desc = (clip.get("description") or "").strip()
    footer = creator.get("post_footer", "").strip() if creator else ""

    # Build structured hashtags according to template
    blob = " ".join([
        str(clip.get("title") or ""),
        str(clip.get("description") or ""),
        str(clip.get("hook") or ""),
        str(clip.get("reason") or ""),
        str((creator or {}).get("name") or ""),
        str((creator or {}).get("id") or ""),
    ]).lower()

    # Determine creator handle (e.g. @ishowspeed)
    handle = creator_handle(creator, blob)
    creator_tag_line = f"Streamer: {handle}" if handle and handle.lower() not in desc.lower() and handle.lower() not in title.lower() else ""

    tags = []
    # 1. Characters: #lucia, #jason, #jasonduval (if present/mentioned in the video)
    if "lucia" in blob:
        tags.append("lucia")
    if any(k in blob for k in ("jason", "duval")):
        tags.extend(["jason", "jasonduval"])

    # 2. #gta6 and high-traffic GTA discovery & meme tags (from viral Instagram Reels)
    tags.extend(["gta6", "gtavi", "grandtheftauto6", "grandtheftautovi", "gtamemes", "gamingmemes"])

    # 3. Creator-specific tags (replace generic #streamershocked with #carryshocked, #speedshocked, etc.)
    c_name = str((creator or {}).get("name") or "").lower()
    cat = str(clip.get("category") or "shock").lower()
    act = "shocked" if cat in ("shock", "rage", "funny") else cat

    if any(k in c_name or k in blob for k in ("carry", "carryminati", "carryislive")):
        tags.extend(["carryshocked", "carryminati", "speed"])
    elif any(k in c_name or k in blob for k in ("speed", "ishowspeed")):
        tags.extend(["speedshocked", "speed", "ishowspeed", "carryminati"])
    elif any(k in c_name or k in blob for k in ("insaan", "liveinsaan", "triggeredinsaan")):
        tags.extend(["liveinsaan", "triggeredinsaan", "carryminati", "speed"])
    elif "mrbeast" in c_name or "mrbeast" in blob:
        tags.extend(["mrbeast", "mrbeastshocked", "speed"])
    else:
        clean = re.sub(r"\W", "", str((creator or {}).get("name") or ""))
        if clean:
            tags.extend([f"{clean.lower()}{act}", clean.lower()])

    # Top creator community tags requested
    for ct in ("carryminati", "speed", "mrbeast", "liveinsaan"):
        if ct not in tags:
            tags.append(ct)

    # 4. Existing clip tags (filtering out any generic 'streamer' tag)
    for h in clip.get("hashtags", []):
        clean_h = re.sub(r"\W", "", str(h))
        if not clean_h or "streamer" in clean_h.lower():
            continue  # remove generic #streamer / #streamershocked
        if clean_h.lower() not in [t.lower() for t in tags]:
            tags.append(clean_h)

    # 5. Core viral tags
    viral = ["Viral", "Trending", "FYP", "Gaming"]
    if is_short:
        viral.insert(0, "Shorts")
    for vt in viral:
        if vt.lower() not in [t.lower() for t in tags]:
            tags.append(vt)

    tag_str = " ".join("#" + t for t in tags[:16])

    parts = [title]
    if desc and desc.lower() != title.lower():
        parts.append(desc)
    if creator_tag_line:
        parts.append(creator_tag_line)
    if footer:
        parts.append(footer)
    parts.append(tag_str)
    return "\n\n".join(parts)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        is_short = not (acc["platform"] == "youtube" and p.get("yt_format") == "video")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator, is_short=is_short)
        if acc["platform"] == "youtube" and not is_short:
            # a normal video: YouTube makes anything vertical and under three minutes a Short, so a 16:9 copy goes up
            rid, url = accounts.youtube_upload(acc, jobs.wide_file(job, clip), title, cap, clip.get("hashtags", []),
                                               creator["privacy"], short=False)
        elif acc["platform"] == "youtube":
            rid, url = accounts.youtube_upload(acc, path, title, cap, clip.get("hashtags", []), creator["privacy"], short=True)
        elif acc["platform"] == "instagram" and p.get("format") == "story":
            rid, url = accounts.instagram_story_upload(acc, path, job["id"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, cap, job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, cap, creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
