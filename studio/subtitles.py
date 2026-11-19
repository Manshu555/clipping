"""An .srt subtitle file next to every clip, for uploading as subtitles or editing in another app.

The words come from OpenAI's Whisper, run on this computer (the graphics card when there is one), straight from the
clip's own sound. If Whisper is not installed, or fails, they come from PeakCut's own transcript instead, which the
burned-in captions already use. Nothing is sent anywhere.

    PEAKCUT_SRT_ENGINE     auto (Whisper if installed, else the own transcript) | whisper | own | off
    PEAKCUT_WHISPER_MODEL  tiny | base | small | medium | large-v3 ... (default base: quick, and good on clear speech)
"""
import threading
import time
from pathlib import Path

import numpy as np

from . import config, media

ENGINE = (config.env("PEAKCUT_SRT_ENGINE", "auto") or "auto").strip().lower()
WHISPER_MODEL = (config.env("PEAKCUT_WHISPER_MODEL", "base") or "base").strip()
MAX_CHARS, MAX_SECONDS, GAP = 42, 4.0, 0.7      # one subtitle: at most this long, split at pauses
_lock = threading.Lock()                        # one Whisper at a time: it is large, and the card is shared
_model = {"m": None, "device": None}


def whisper_installed() -> bool:
    try:
        import whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(cues: list) -> str:
    """[(start, end, text)] as an .srt file. Empty and zero-length cues are left out; numbering starts at 1."""
    out, n = [], 0
    for s, e, text in cues:
        text = " ".join(str(text).split())
        if not text or e - s < 0.05:
            continue
        n += 1
        out.append(f"{n}\n{_ts(s)} --> {_ts(e)}\n{text}\n")
    return "\n".join(out)


def cues_from_words(words: list, start: float, end: float) -> list:
    """PeakCut's own words ({w, s, e} on the source clock) as subtitles on the clip's clock: short lines that break at
    pauses and sentence ends, never past the clip's end."""
    dur = end - start
    cues, cur = [], []

    def flush():
        if cur:
            s, e = max(0.0, cur[0]["s"] - start), min(dur, cur[-1]["e"] - start)
            cues.append((s, max(e, s + 0.3) if s + 0.3 <= dur else dur, " ".join(w["w"] for w in cur)))
            cur.clear()

    for w in sorted((w for w in words if w.get("w") and start - 0.2 <= w["s"] < end), key=lambda w: w["s"]):
        if cur and (w["s"] - cur[-1]["e"] > GAP or w["e"] - cur[0]["s"] > MAX_SECONDS
                    or len(" ".join(x["w"] for x in cur)) + 1 + len(w["w"]) > MAX_CHARS or cur[-1]["w"][-1:] in ".?!"):
            flush()
        cur.append(w)
    flush()
    return cues


_models: dict = {}                              # (name, device) -> loaded Whisper model


def _load(device, name=None):
    import whisper
    key = (name or WHISPER_MODEL, device)
    if key not in _models:
        _models[key] = whisper.load_model(key[0], device=device, download_root=str(config.MODELS_DIR / "whisper"))
    _model["m"], _model["device"] = _models[key], device
    return _models[key]


def _drop(device, name=None):
    _models.pop((name or WHISPER_MODEL, device), None)
    _model["m"] = None


# ---------------------------------------------------------------- live speech that is not English
# PeakCut's own speech model (Parakeet) knows only English: Hindi comes out of it as made-up English words. A
# stream in another language is written down by Whisper instead, word by word, so captions, the AI and "clip it"
# all still work. The graphics card is shared with a local AI model; when it is full, the processor does it, and
# the card is not tried again for a few minutes.
LIVE_MODEL = (config.env("PEAKCUT_WHISPER_LIVE_MODEL", "small") or "small").strip()
_gpu_busy_until = [0.0]


def _devices() -> list:
    try:
        import torch
        if torch.cuda.is_available() and time.time() >= _gpu_busy_until[0]:
            return ["cuda", "cpu"]
    except Exception:  # noqa: BLE001
        pass
    return ["cpu"]


def _run(fn):
    """Run fn(model, device) on the card, or on the processor when the card cannot take it."""
    last = None
    with _lock:
        for device in _devices():
            try:
                return fn(_load(device, LIVE_MODEL), device)
            except Exception as e:  # noqa: BLE001
                last = e
                _drop(device, LIVE_MODEL)
                if device == "cuda":
                    _gpu_busy_until[0] = time.time() + 300
                    try:
                        import torch
                        torch.cuda.empty_cache()
                    except Exception:  # noqa: BLE001
                        pass
    raise RuntimeError(f"Whisper could not run: {last}")


def language_votes(audio_int16: np.ndarray) -> dict:
    """{language code: probability} for a piece of 16 kHz speech, from Whisper."""
    import whisper
    x = whisper.pad_or_trim(audio_int16.astype(np.float32) / 32768.0)

    def fn(model, device):
        mel = whisper.log_mel_spectrogram(x, n_mels=model.dims.n_mels).to(model.device)
        _, probs = model.detect_language(mel)
        return dict(probs)
    return _run(fn)


def whisper_words(audio_int16: np.ndarray, language: str) -> list:
    """[{'w', 's', 'e'}] for a piece of 16 kHz speech in `language`, with each word's own timing."""
    x = audio_int16.astype(np.float32) / 32768.0

    def fn(model, device):
        res = model.transcribe(x, language=language, fp16=device == "cuda", word_timestamps=True,
                               condition_on_previous_text=False, verbose=None)
        out = []
        for seg in res.get("segments", []):
            for w in seg.get("words") or []:
                word = str(w.get("word", "")).strip()
                if word:
                    out.append({"w": word, "s": round(float(w["start"]), 2), "e": round(float(w["end"]), 2)})
        return out
    return _run(fn)


def _whisper_cues(clip_file: Path) -> list:
    """Whisper on the clip's own sound. The graphics card is tried first; if it is busy (a local AI model can be
    holding its memory), the processor does it instead."""
    wav = clip_file.with_name(clip_file.stem + ".srt.wav")
    try:
        media.extract_audio(clip_file, wav)
        audio = media.load_audio(wav).astype(np.float32) / 32768.0
    finally:
        wav.unlink(missing_ok=True)
    last = None
    with _lock:
        for device in _devices():
            try:
                res = _load(device).transcribe(audio, fp16=device == "cuda", condition_on_previous_text=False,
                                               verbose=None)
                return [(float(s["start"]), float(s["end"]), s["text"]) for s in res.get("segments", [])]
            except Exception as e:  # noqa: BLE001
                last = e
                _drop(device)
                if device == "cuda":
                    _gpu_busy_until[0] = time.time() + 300
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError(f"Whisper could not transcribe this clip: {last}")


def write(clip_file: Path, words: list, start: float, end: float) -> dict:
    """Write <clip>.srt next to the clip. `words` and start/end are on the clip's source clock (as rendered).
    Returns {"file", "from"}, or {} when subtitles are switched off."""
    if ENGINE == "off":
        return {}
    cues, source = None, "own"
    if ENGINE in ("auto", "whisper") and whisper_installed():
        try:
            cues, source = _whisper_cues(clip_file), "whisper"
        except Exception:  # noqa: BLE001 - the own transcript is always there
            cues = None
    if cues is None:
        cues, source = cues_from_words(words, start, end), "own"
    out = clip_file.with_suffix(".srt")
    out.write_text(to_srt(cues), encoding="utf-8")
    return {"file": out.name, "from": source}
