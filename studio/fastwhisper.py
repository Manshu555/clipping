"""Speech that is not English, written down by faster-whisper on this computer.

PeakCut's own speech model (Parakeet) knows only English: Hindi comes out of it as made-up English words, and the AI
then finds nothing worth clipping. For a video in another language, with no transcript from YouTube to borrow, the
whole video is written down here instead, by Whisper (large-v3-turbo: good at Hindi, and fast on a graphics card).

The graphics card is shared with a local AI model (Ollama). That model is asked to step aside first, so Whisper
has the card to itself; it loads again by itself the next time the AI is asked something. Without a card, or if it
is full, the processor does it (much slower).

    PEAKCUT_WHISPER_VIDEO_MODEL   large-v3-turbo (default) | medium | small ...
    PEAKCUT_VIDEO_WHISPER         1 (default) | 0 to always use PeakCut's own model, as before
"""
import os
import threading

import numpy as np

from . import config

MODEL = (config.env("PEAKCUT_WHISPER_VIDEO_MODEL", "large-v3-turbo") or "large-v3-turbo").strip()
ON = config.env("PEAKCUT_VIDEO_WHISPER", "1") != "0"
BATCH = int(config.env("PEAKCUT_WHISPER_BATCH", "8") or 8)    # pieces of speech at once on a graphics card
SR = 16000
_lock = threading.Lock()
_cache: dict = {}


def available() -> bool:
    if not ON:
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _gpu_libraries():
    """faster-whisper's engine finds the CUDA libraries that came with PyTorch, when PyTorch is installed."""
    try:
        import torch
        lib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(lib) and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(lib)
    except Exception:  # noqa: BLE001
        pass


def _model(device: str):
    from faster_whisper import WhisperModel
    if device not in _cache:
        if device == "cuda":
            _gpu_libraries()
        _cache[device] = WhisperModel(MODEL, device=device, compute_type="int8_float16" if device == "cuda" else "int8",
                                      download_root=str(config.MODELS_DIR / "faster-whisper"))
    return _cache[device]


def _devices() -> list:
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return ["cuda", "cpu"]
    except Exception:  # noqa: BLE001
        pass
    return ["cpu"]


def free_the_card():
    """Ask a local Ollama model to unload, so Whisper has the graphics card. Harmless when it is not loaded."""
    try:
        from . import ai
        if ai.provider() != "ollama":
            return
        import httpx
        base = ai.base_url().rstrip("/").removesuffix("/v1")
        for m in ai.models():
            httpx.post(f"{base}/api/generate", json={"model": m, "keep_alive": 0}, timeout=30)
    except Exception:  # noqa: BLE001 - if it does not answer, Whisper still runs, perhaps on the processor
        pass


def _run(fn):
    last = None
    with _lock:
        for device in _devices():
            try:
                return fn(_model(device))
            except Exception as e:  # noqa: BLE001 - the card is full or missing a library: the processor
                last = e
                _cache.pop(device, None)
    raise RuntimeError(f"Whisper could not run: {last}")


def language(audio: np.ndarray, samples: int = 3, seconds: int = 30) -> str:
    """The language spoken, from a few half-minutes spread through the audio (16 kHz int16). Several are heard,
    since a game's characters can speak English in a stream that is not."""
    n = len(audio)
    votes: dict = {}
    for k in range(samples):
        mid = int(n * (k + 1) / (samples + 1))
        piece = audio[max(0, mid - seconds * SR // 2): mid + seconds * SR // 2].astype(np.float32) / 32768.0
        if len(piece) < SR * 5 or float(np.sqrt((piece ** 2).mean())) < 0.003:
            continue                                      # silence says nothing about the language
        _lang, _p, probs = _run(lambda m: m.detect_language(piece))
        for code, p in probs[:5]:
            votes[code] = votes.get(code, 0.0) + float(p)
    return max(votes, key=votes.get) if votes else ""


def transcribe(audio: np.ndarray, lang: str, on_progress=None) -> list:
    """[{'w', 's', 'e'}] for the whole audio (16 kHz int16), each word with its own timing."""
    x = audio.astype(np.float32) / 32768.0
    total = max(1.0, len(x) / SR)

    def fn(model):
        if model.model.device == "cuda":
            # On a graphics card, many pieces of speech at once: several times faster than one after another, which
            # leaves the card mostly waiting. (On the processor batching gains nothing.)
            from faster_whisper import BatchedInferencePipeline
            segs, _info = BatchedInferencePipeline(model=model).transcribe(
                x, language=lang or None, word_timestamps=True, vad_filter=True, beam_size=1, batch_size=BATCH)
        else:
            segs, _info = model.transcribe(x, language=lang or None, word_timestamps=True, vad_filter=True,
                                           beam_size=1, condition_on_previous_text=False)
        out = []
        for seg in segs:                                  # a generator: the work happens while this is read
            for w in seg.words or []:
                word = str(w.word).strip()
                if word:
                    out.append({"w": word, "s": round(float(w.start), 2), "e": round(float(w.end), 2)})
            if on_progress:
                on_progress(min(1.0, float(seg.end) / total))
        return out
    return _run(fn)
