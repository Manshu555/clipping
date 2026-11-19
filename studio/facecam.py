"""Where the facecam is.

A gaming stream is a game with a small webcam picture in one corner of it. The
short that comes out of it is better when the game fills the screen and the
webcam sits in the corner of it, instead of the whole 16:9 frame squeezed into
a band with blurred fill above and below.

The webcam is found by the one thing a webcam always has and a game almost never
has: a straight border that stays in the same place in every frame. A wall in
the game moves, a HUD bar changes, but the edge of the webcam box does not move
at all. A second thing tells the webcam apart from the game's own panels, which
have straight borders too: the picture inside a webcam is almost still, while
the game behind a panel keeps moving.

Metric: for every column and every row, how much the picture changes at that
line, counted over frames spread through the video. A border of the webcam box
stands out from the rest.

One frame can also be shown to the AI (`detect(path, ask=...)`), which is better
than any measurement at telling a person from a game character. Its answer is
measured again on the picture before it is used, so being wrong costs a search,
not a clip.

Only numpy and ffmpeg, which are already here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

W, H = 320, 180            # the picture is looked at small: only the lines matter
MAX_FRAMES = 24            # frames spread through the video
MIN_SIZE = 0.12            # the smallest a webcam may be, as a part of the width or height
MAX_SIZE = 0.60            # and the largest
ASPECT_MIN = 0.55          # the narrowest: a phone held upright is about 0.56
ASPECT_MAX = 3.20          # the widest: a banner or a ticker is wider than this
STILL_FRAC = 0.35          # how much of the game's own movement a webcam picture may have
STILL_MIN = 6.0            # and the floor, for a game that itself is almost still
STILL_SHARE = 0.30         # at most this much of a webcam picture may be moving, or the game is showing through
EDGE_FLUSH = 0.02          # a side this close to the frame's own edge is the edge
MATCH = 0.35               # how much a box the AI points at must cover a measured box to be that box
EDGE = 2.2                 # how much brighter than the average line a border must be


def _frames(path: str | Path, count: int = MAX_FRAMES) -> np.ndarray:
    """The video as a few grey frames, spread through it."""
    try:
        dur = _duration(path)
        rate = max(count / dur, 0.02) if dur > 0 else 0.2
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"fps={rate:.4f},scale={W}:{H},format=gray",
             "-frames:v", str(count), "-f", "rawvideo", "-"],
            capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001 - no ffmpeg or an unreadable file: no facecam, not a failure
        return np.zeros((0, H, W), dtype=np.uint8)
    raw = proc.stdout or b""
    size = W * H
    n = len(raw) // size
    if n < 4:
        return np.zeros((0, H, W), dtype=np.uint8)
    return np.frombuffer(raw[:n * size], dtype=np.uint8).reshape(n, H, W)


NEAR_SPAN = 60.0           # seconds around the frame the model was shown, for checking the box it proposed
NEAR_SLIP = 0.25           # how far (as a part of the box) a snapped side may move from where the model put it


def _frames_near(path: str | Path, count: int = MAX_FRAMES, span: float = NEAR_SPAN, at: float = 0.5) -> np.ndarray:
    """Grey frames from about a minute around `at` (a part of the video), where the model's frame came from.

    Over a whole long video a webcam picture changes nearly as much as the game: the light changes, the
    person moves about, sits down, leans in. Over a minute it hardly moves while the game races on, which
    is the difference that tells them apart. A short file (a live clip's piece) is used whole."""
    dur = _duration(path)
    if dur <= span * 1.5:
        return _frames(path, count)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{min(max(0.0, dur * at - span / 2.0), max(0.0, dur - span)):.1f}", "-t", f"{span:.1f}",
             "-i", str(path), "-vf", f"fps={count / span:.4f},scale={W}:{H},format=gray",
             "-frames:v", str(count), "-f", "rawvideo", "-"],
            capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001
        return np.zeros((0, H, W), dtype=np.uint8)
    raw = proc.stdout or b""
    n = len(raw) // (W * H)
    if n < 4:
        return np.zeros((0, H, W), dtype=np.uint8)
    return np.frombuffer(raw[:n * W * H], dtype=np.uint8).reshape(n, H, W)


def _duration(path: str | Path) -> float:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(path)],
                              capture_output=True, text=True, timeout=60)
        return float((proc.stdout or "").strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _profiles(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(vertical lines, horizontal lines): how much changes at each column and each row.

    The median over the frames is used, so one busy moment cannot invent a line."""
    if len(frames) < 4:
        return np.zeros(W), np.zeros(H)
    f = frames.astype(np.int16)
    gx = np.median(np.abs(np.diff(f, axis=2)).sum(axis=1), axis=0)   # change across each column boundary
    gy = np.median(np.abs(np.diff(f, axis=1)).sum(axis=2), axis=0)   # change across each row boundary
    return gx, gy


def _peaks(profile: np.ndarray, limit: int) -> list[tuple[int, float]]:
    """The lines that stand out: (where, how much)."""
    if len(profile) < 3:
        return []
    med = float(np.median(profile)) or 1.0
    out = []
    for i in range(1, len(profile) - 1):
        if profile[i] >= profile[i - 1] and profile[i] > profile[i + 1] and profile[i] > EDGE * med:
            out.append((i, float(profile[i] / med)))
    return sorted(out, key=lambda p: -p[1])[:limit]


def _size(path: str | Path) -> tuple[int, int]:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height", "-of", "default=nw=1", str(path)],
                          capture_output=True, text=True, timeout=60)
    w = h = 0
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            if k.strip() == "width":
                w = int(v.strip())
            elif k.strip() == "height":
                h = int(v.strip())
        except ValueError:
            continue
    return w, h


def _stillness(frames: np.ndarray) -> np.ndarray:
    """How much each pixel changes over the frames.

    A webcam picture barely changes: a person sits in a room and shifts a little. The game changes every
    frame. This is the one thing that tells a webcam apart from the game's own panels, which have straight
    borders too and are drawn over the game with the game showing through them."""
    return frames.astype(np.float32).std(axis=0)


def _plausible(x0, y0, x1, y1, still, light, game, share=STILL_SHARE):
    """Could this rectangle be a webcam picture? Judged on its shape, its stillness and its light.

    Returns how much it moves, or None. Nothing here says which corner it is in: a webcam may sit in a
    corner, along one edge, or with a margin all round."""
    bw, bh = (x1 - x0) / W, (y1 - y0) / H
    if not (MIN_SIZE <= bw <= MAX_SIZE and MIN_SIZE <= bh <= MAX_SIZE):
        return None
    # measured in pixels: a 4:3 picture is 1.33, a phone held upright is 0.56
    aspect = (bw * W) / max(1e-6, bh * H)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return None
    a, b, c, d = int(x0), int(y0), int(x1), int(y1)
    inside = still[b:d, a:c]
    if not inside.size:
        return None
    cut = max(STILL_MIN, STILL_FRAC * game)
    moving = float(np.median(inside))
    if moving > cut:
        return None                    # it changes with the game: it is the game
    if float((inside > cut).mean()) > share:
        return None                    # a good part of it moves: the game is showing through
    if float(light[b:d, a:c].mean()) < 0.30 * float(light.mean()):
        return None                    # a black hole, not a picture
    return moving


def _local(vpeaks, hpeaks, still, light):
    """Every rectangle the strong straight lines can make, that still looks like a camera.

    Returns them best first: the one whose weakest border is the strongest, and of those the bigger."""
    game = float(np.median(still))
    xs = sorted({0.0, float(W)} | {float(x) for x, _ in vpeaks[:10]})
    ys = sorted({0.0, float(H)} | {float(y) for y, _ in hpeaks[:10]})
    vs = {float(x): s for x, s in vpeaks}
    hs = {float(y): s for y, s in hpeaks}
    out = []
    for i, x0 in enumerate(xs):
        wx = [vs[v] for v in (x0,) if v in vs]
        for x1 in xs[i + 1:]:
            wscore = wx + [vs[v] for v in (x1,) if v in vs]
            for j, y0 in enumerate(ys):
                hy0 = [hs[v] for v in (y0,) if v in hs]
                for y1 in ys[j + 1:]:
                    score = wscore + hy0 + [hs[v] for v in (y1,) if v in hs]
                    if len(score) < 2:
                        continue       # a box needs two borders of its own, or it is just the frame
                    moving = _plausible(x0, y0, x1, y1, still, light, game)
                    if moving is None:
                        continue
                    key = (min(score), (x1 - x0) * (y1 - y0))
                    out.append({"key": key, "x": x0, "y": y0, "x1": x1, "y1": y1,
                                "score": round(min(score), 2), "moving": moving})
    return sorted(out, key=lambda c: c["key"], reverse=True)


def _overlap(a, box) -> float:
    """How much two boxes cover the same ground: 1 when they are the same, 0 when they do not touch."""
    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x1"], a["y1"]
    bx, by, bw, bh = box
    bx0, by0, bx1, by1 = bx * W, by * H, (bx + bw) * W, (by + bh) * H
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _full_frame(path: str | Path, at: float = 0.5) -> np.ndarray | None:
    """One grey frame at full size, from `at` (the middle unless said): where the borders are measured."""
    w, h = _size(path)
    if not w or not h:
        return None
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, _duration(path) * at):.1f}", "-i", str(path),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, timeout=120).stdout or b""
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < w * h:
        return None
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w).astype(np.float32)


def _best(profile: np.ndarray, approx: float, span: float) -> float:
    """The strongest change near a guessed line."""
    lo, hi = max(0, int(approx - span)), min(len(profile), int(approx + span) + 1)
    seg = profile[lo:hi]
    return float(lo + seg.argmax()) if len(seg) else float(approx)


def _snap(path: str | Path, box, w: int, h: int, span: float = 0.0, at: float = 0.5):
    """A box on the small grid, put onto the borders that are really in the picture.

    A border the small grid found is a pixel or two out; a box a model proposed can be a good part of the
    frame out, which is what `span` is for. A side within a hair of the frame's own edge is the edge."""
    frame = _full_frame(path, at)
    if frame is None:
        return None
    fh, fw = frame.shape
    col = np.abs(np.diff(frame, axis=1)).sum(axis=0)
    row = np.abs(np.diff(frame, axis=0)).sum(axis=1)
    # The small grid is a quarter of the picture, so a border found on it is a few pixels out; one the
    # model proposed can be a good part of the frame out, which is what `span` is for.
    spread = max(4.0, span * max(fw, fh), 1.5 * max(fw / W, fh / H))

    def line(profile, approx, edge, extent):
        if abs(approx - edge) <= EDGE_FLUSH * extent:
            return float(edge)
        return _best(profile, approx, spread)

    x0, y0, x1, y1 = box
    return (line(col, x0 * fw / W, 0, fw), line(row, y0 * fh / H, 0, fh),
            line(col, x1 * fw / W, fw, fw), line(row, y1 * fh / H, fh, fh))


def _corner(x0, y0, x1, y1, w, h) -> str:
    """What part of the frame the box is on: its corner, its edge, or the middle."""
    near = EDGE_FLUSH
    left, right = x0 <= w * near, x1 >= w * (1 - near)
    top, bottom = y0 <= h * near, y1 >= h * (1 - near)
    if left and top:
        return "top-left"
    if right and top:
        return "top-right"
    if left and bottom:
        return "bottom-left"
    if right and bottom:
        return "bottom-right"
    if left or right:
        return "right" if right else "left"
    if top or bottom:
        return "bottom" if bottom else "top"
    return "middle"


def _finish(path: str | Path, found: dict, w: int, h: int, still: np.ndarray, at: float = 0.5,
            seen: bool = False) -> dict | None:
    """The box in source pixels, with how sure it is that this is really a camera.

    `seen`: the model said a person is in this box, and the box has already passed the check that it is not the
    game. That is as sure as a still picture: a streamer who never stops moving (bouncing, shouting at the screen)
    is still a webcam, though the picture is never calm enough for the measurement alone."""
    snapped = _snap(path, (found["x"], found["y"], found["x1"], found["y1"]), w, h, at=at)
    if not snapped:
        return None
    x0, y0, x1, y1 = snapped
    if x1 - x0 < w * MIN_SIZE or y1 - y0 < h * MIN_SIZE:
        return None
    inside = still[int(found["y"]):int(found["y1"]), int(found["x"]):int(found["x1"])]
    game = float(np.median(still))
    moving = float(np.median(inside)) if inside.size else 99.0
    # sure of it when the picture inside hardly moves while the game around it is racing
    conf = "high" if seen or moving <= max(3.0, 0.25 * game) else "low"
    return {"x": int(round(x0)), "y": int(round(y0)), "w": int(round(x1 - x0)), "h": int(round(y1 - y0)),
            "corner": _corner(x0, y0, x1, y1, w, h), "score": found.get("score", 0.0), "conf": conf}


def _frame_jpeg(path: str | Path, width: int = 768, at: float = 0.5) -> bytes:
    """One frame of the stream, small, as a JPEG: what a model is shown."""
    try:
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, _duration(path) * at):.1f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-pix_fmt", "yuvj420p", "-q:v", "4",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=120).stdout or b""
    except Exception:  # noqa: BLE001
        return b""


ASK_AT = (0.5, 0.3, 0.7)   # the moments shown to the model, in turn, until it sees a webcam


def _asked(path: str | Path, ask):
    """Frames to the model, one moment at a time: (did it look, its box as parts of the frame or None, the moment).

    One look is not enough: a model sometimes misses a webcam that is plainly there, and a stream may show a
    full-screen camera or a "be right back" slate at that moment. So up to three moments are shown, and the
    first that has a webcam is used. "No webcam" means it looked at all of them and found none."""
    looked = False
    for at in ASK_AT:
        jpeg = _frame_jpeg(path, at=at)
        if not jpeg:
            continue
        try:
            answer = ask(jpeg) or {}
        except Exception:  # noqa: BLE001 - a model that cannot look is not a failure
            continue
        looked = looked or bool(answer.get("looked"))
        if answer.get("looked") and answer.get("box"):
            return True, answer["box"], at
    return looked, None, 0.5


def _seen(path: str | Path, box, w: int, h: int, still: np.ndarray, light: np.ndarray, at: float = 0.5) -> dict | None:
    """A box the model proposed, put onto the picture's real borders and checked like any other.

    A model reads a picture well but places a box roughly, and it can be wrong about the whole thing. So
    its box is only believed where the borders are really there and the picture inside really is still."""
    fx, fy, fw, fh = box
    # Judged over the minute the model looked at, not the whole video (see _frames_near).
    near = _frames_near(path, at=at)
    if len(near) >= 4:
        still, light = _stillness(near), near.astype(np.float32).mean(axis=0)
    game = float(np.median(still))
    asked = (fx * w, fy * h, (fx + fw) * w, (fy + fh) * h)
    shapes = []
    # 1. the model's box put onto the real borders. A side the snapping moved far from where the model put
    #    it found some other line (a road, a HUD bar): there the model's own side is kept.
    snapped = _snap(path, (fx * W, fy * H, (fx + fw) * W, (fy + fh) * H), w, h, span=0.12, at=at)
    if snapped:
        shapes.append(tuple(snapped))
        shapes.append(tuple(s if abs(s - a) <= NEAR_SLIP * (fw * w if i % 2 == 0 else fh * h) else a
                            for i, (s, a) in enumerate(zip(snapped, asked))))
    # 2. the still area around it: a webcam with no border line of its own is still the part that hardly moves
    region = _still_region(still, box, game)
    if region:
        shapes.append(tuple(v * (w / W if i % 2 == 0 else h / H) for i, v in enumerate(region)))
    # 3. the model's own box, as it said
    shapes.append(asked)
    good = []
    for x0, y0, x1, y1 in shapes:
        x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(w), x1), min(float(h), y1)
        if x1 - x0 < w * MIN_SIZE or y1 - y0 < h * MIN_SIZE:
            continue
        small = (x0 * W / w, y0 * H / h, x1 * W / w, y1 * H / h)
        # The model has already said a person is here, so the picture inside is allowed to move more than a
        # measured box would be; what it may not be is the game itself.
        moving = _plausible(*small, still, light, game, share=1.0)
        if moving is None:
            continue
        good.append(((small[2] - small[0]) * (small[3] - small[1]), moving, small))
    if not good:
        return None
    # Of the boxes big enough to be the whole webcam (at least half the biggest), the calmest: a box that runs
    # over the webcam's edge takes in some of the game, and the game is what moves.
    biggest = max(a for a, _, _ in good)
    small = min((g for g in good if g[0] >= 0.5 * biggest), key=lambda g: g[1])[2]
    found = {"x": small[0], "y": small[1], "x1": small[2], "y1": small[3], "score": 0.0}
    return _finish(path, found, w, h, still, at=at, seen=True)


def _still_region(still: np.ndarray, box, game: float):
    """The run of still rows and columns that holds the middle of the model's box (on the small grid), or None.

    Columns are judged on the box's rows and rows on its columns, out to its own size again on every side, so
    a rough box grows to the whole still picture around it and no further."""
    fx, fy, fw, fh = box
    cut = max(STILL_MIN, STILL_FRAC * game)
    mask = still < cut

    def run(frac, lo, centre):
        ok = frac > 0.6
        c = int(centre) - lo
        if not (0 <= c < len(ok)) or not ok[c]:
            return None
        a = b = c
        while a > 0 and ok[a - 1]:
            a -= 1
        while b < len(ok) - 1 and ok[b + 1]:
            b += 1
        return lo + a, lo + b + 1

    x0, x1 = max(0, int((fx - fw) * W)), min(W, int((fx + 2 * fw) * W))
    y0, y1 = max(0, int((fy - fh) * H)), min(H, int((fy + 2 * fh) * H))
    rx0, rx1 = max(0, int(fx * W)), min(W, max(int(fx * W) + 1, int((fx + fw) * W)))
    ry0, ry1 = max(0, int(fy * H)), min(H, max(int(fy * H) + 1, int((fy + fh) * H)))
    if x1 <= x0 or y1 <= y0:
        return None
    cols = run(mask[ry0:ry1, x0:x1].mean(axis=0), x0, (fx + fw / 2) * W)
    rows = run(mask[y0:y1, rx0:rx1].mean(axis=1), y0, (fy + fh / 2) * H)
    if not cols or not rows:
        return None
    return float(cols[0]), float(rows[0]), float(cols[1]), float(rows[1])


def detect(path: str | Path, ask=None) -> dict | None:
    """The facecam's box in the source, in source pixels, or None.

    The box is given as x, y, width, height, with the confidence in "score" and
    how sure we are that it is a camera in "conf".

    `ask` is shown one frame of the stream and says where the webcam is (brain.look_for_facecam). Its
    answer is measured again on the picture before it is believed, so a model that is wrong costs the
    search an idea, not a broken clip. Without `ask` nothing outside the file is looked at, which is how
    the tests run and how a job with no AI set still works."""
    frames = _frames(path)
    if len(frames) < 4:
        return None
    w, h = _size(path)
    if not w or not h:
        return None
    still = _stillness(frames)
    light = frames.astype(np.float32).mean(axis=0)
    vx, hy = _profiles(frames)
    vpeaks, hpeaks = _peaks(vx, 40), _peaks(hy, 40)
    found = _local(vpeaks, hpeaks, still, light) if (vpeaks and hpeaks) else []
    if ask is not None:
        looked = pointed = False
        # One moment at a time (see ASK_AT). A box the model gives that does not hold up is not the end: the
        # next moment is asked too, since a model that placed the box badly once often places it well again.
        for at in ASK_AT:
            jpeg = _frame_jpeg(path, at=at)
            if not jpeg:
                continue
            try:
                answer = ask(jpeg) or {}
            except Exception:  # noqa: BLE001 - a model that cannot look is not a failure
                continue
            looked = looked or bool(answer.get("looked"))
            hinted = answer.get("box") if answer.get("looked") else None
            if not hinted:
                continue
            pointed = True
            # The model is better than any measurement at saying "that is a person", and worse at saying
            # exactly where the lines are. So it picks which of the measured boxes it is, and only if it
            # matches none of them is its own box measured and used.
            match = max(found, key=lambda c: _overlap(c, hinted)) if found else None
            if match and _overlap(match, hinted) >= MATCH:
                return _finish(path, match, w, h, still)
            for box in _box_variants(hinted):
                seen = _seen(path, box, w, h, still, light, at=at)
                if seen:
                    return seen
        if looked and not pointed:
            return None                    # it looked at every moment, and there is no webcam on this stream
    if not found:
        return None
    return _finish(path, found[0], w, h, still)


def _box_variants(box) -> list:
    """The model's box, and for a box that runs most of the frame's height (or width), its thirds along that side
    as well. A model sometimes marks the whole column a webcam sits in (top to bottom of the frame) when the
    webcam is only its top corner; the third that holds the webcam then grows to its real size in _seen."""
    fx, fy, fw, fh = box
    out = [box]
    if fh >= 0.6:
        out += [(fx, fy + k * fh / 3, fw, fh / 3) for k in range(3)]
    if fw >= 0.6:
        out += [(fx + k * fw / 3, fy, fw / 3, fh) for k in range(3)]
    return out
