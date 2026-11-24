"""Local AI engines. Every engine is open-source and runs on this machine:
  - STT:      faster-whisper (MIT)
  - Brain:    Ollama (any local chat model; default gemma3:12b)
  - Voice:    Chatterbox TTS zero-shot voice cloning (MIT, Resemble AI)
  - Lip-sync: SadTalker (Apache-2.0) talking-head video from headshot + audio
"""
import glob
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid

import httpx

from . import config

log = logging.getLogger("twin.engines")


# ---------- ffmpeg helpers ----------
def ffmpeg(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[:300]}")


def audio_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def to_wav(src: str, dst: str, rate: int = 24000) -> None:
    ffmpeg("-i", src, "-ac", "1", "-ar", str(rate), dst)


def make_voice_reference(full_wav: str, ref_wav: str) -> None:
    """Chatterbox conditions on ~10s of audio. Strip silences, then take a clean 12s window
    from the middle of the recording (skips the nervous start)."""
    tmp = ref_wav + ".nosil.wav"
    ffmpeg("-i", full_wav, "-af", "silenceremove=start_periods=1:stop_periods=-1:stop_duration=0.4:stop_threshold=-40dB",
           "-ac", "1", "-ar", "24000", tmp)
    dur = audio_duration(tmp)
    start = max(0.0, min(dur / 2 - 6, dur - 12)) if dur > 14 else 0.0
    ffmpeg("-ss", f"{start:.2f}", "-t", "12", "-i", tmp, "-af", "loudnorm", "-ac", "1", "-ar", "24000", ref_wav)
    os.remove(tmp)


def normalize_headshot(src: str, dst: str) -> None:
    # Re-encode (also strips EXIF/GPS metadata) and cap the size.
    ffmpeg("-i", src, "-vf", "scale='min(768,iw)':-2", "-frames:v", "1", dst)


# ---------- Cloud GPU backend (Modal) ----------
CLOUD = config.BACKEND == "modal"
_cls_cache = {}


def _remote(name: str):
    """Handle to a GPU class deployed by modal_app.py (Brain / LipSync)."""
    if name not in _cls_cache:
        import modal
        _cls_cache[name] = modal.Cls.from_name(config.MODAL_APP, name)()
    return _cls_cache[name]


def warm_gpu() -> None:
    """Start a GPU container in the background so the first reply isn't a cold start."""
    global _last_warm
    if CLOUD and time.time() - _last_warm > 120:  # throttled so the endpoint can't be used to burn GPU money
        _last_warm = time.time()
        _remote("Brain").ping.spawn()


_last_warm = 0.0


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


# ---------- Speech to text ----------
_stt = None
_stt_lock = threading.Lock()


def transcribe(path: str) -> str:
    global _stt
    if CLOUD:
        return _remote("Brain").transcribe.remote(_read(path))
    with _stt_lock:
        if _stt is None:
            from faster_whisper import WhisperModel
            _stt = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _ = _stt.transcribe(path, vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()


# ---------- Brain (Ollama) ----------
def chat(system: str, history: list[dict], user_text: str, voice_mode: bool) -> str:
    msgs = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_text}]
    if CLOUD:
        text = _remote("Brain").chat.remote(msgs, 90 if voice_mode else 500)
        return text.replace("*", "").replace("#", "").strip() if voice_mode else text
    r = httpx.post(f"{config.OLLAMA_URL}/api/chat", timeout=180, json={
        "model": config.LLM_MODEL, "messages": msgs, "stream": False, "think": False,
        "options": {"temperature": 0.8, "num_predict": 90 if voice_mode else 500, "num_ctx": 8192},
    })
    r.raise_for_status()
    text = r.json()["message"]["content"].strip()
    if voice_mode:  # spoken replies: strip markdown-ish leftovers
        text = text.replace("*", "").replace("#", "").strip()
    return text


# ---------- Voice clone (Chatterbox) ----------
_tts = None
_tts_lock = threading.Lock()


def _load_tts():
    global _tts
    if _tts is None:
        import torch
        from chatterbox.tts import ChatterboxTTS
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        try:
            _tts = ChatterboxTTS.from_pretrained(device=device)
        except Exception as e:  # some torch/MPS combos fail; CPU always works
            log.warning("Chatterbox on %s failed (%s); falling back to CPU", device, e)
            _tts = ChatterboxTTS.from_pretrained(device="cpu")
    return _tts


def speak(text: str, voice_ref: str, out_wav: str) -> None:
    if CLOUD:
        return _write(out_wav, _remote("Brain").speak.remote(text, _read(voice_ref)))
    import torchaudio
    with _tts_lock:
        model = _load_tts()
        wav = model.generate(text, audio_prompt_path=voice_ref, exaggeration=0.5, cfg_weight=0.5)
        torchaudio.save(out_wav, wav.cpu(), model.sr)


# ---------- Face → stylized avatar (Qwen-Image-Edit, cloud GPU only) ----------
def avatars_available() -> bool:
    return CLOUD  # a 20B image model; too heavy to run usefully on a laptop


def make_avatars(photo_png: str, prompts: dict, seed: int = 7) -> dict:
    """prompts: {key: prompt} → {key: png bytes}"""
    if not CLOUD:
        raise RuntimeError("Avatar generation needs the cloud GPU backend")
    return _remote("AvatarMaker").edit.remote(_read(photo_png), prompts, seed)


# ---------- Dream Life reels (cloud only) ----------
def dreams_available() -> bool:
    return CLOUD


def start_dream(photo: str, voice_ref: str, name: str, dream: str, style: str, style_hint: str, seed: int) -> str:
    """Kicks off the reel on Modal and returns a job id; the work keeps running even if this server restarts."""
    import modal
    fn = modal.Function.from_name(config.MODAL_APP, "make_dream")
    return fn.spawn(_read(photo), _read(voice_ref), name, dream, style, style_hint, seed).object_id


def poll_dream(call_id: str):
    """None while still rendering; the result dict when done; raises if it failed."""
    import modal
    try:
        return modal.FunctionCall.from_id(call_id).get(timeout=0)
    except (TimeoutError, modal.exception.TimeoutError):
        return None


# ---------- Lip-sync video (SadTalker, background queue) ----------
_jobs: "queue.Queue[tuple]" = queue.Queue()


def sadtalker_available() -> bool:
    if CLOUD:
        return True
    return os.path.exists(os.path.join(config.SADTALKER_DIR, "inference.py")) and os.path.exists(config.SADTALKER_PY)


def render_video(headshot: str, audio_wav: str, out_mp4: str) -> None:
    if CLOUD:
        return _write(out_mp4, _remote("LipSync").render.remote(_read(headshot), _read(audio_wav),
                                                                 config.SADTALKER_PREPROCESS))
    work = os.path.join(config.DATA_DIR, "tmp", uuid.uuid4().hex)
    os.makedirs(work, exist_ok=True)
    try:
        # sadtalker_run.py lets SadTalker use the Apple GPU (~10x faster than its CPU path).
        wrapper = os.path.join(os.path.dirname(__file__), "sadtalker_run.py")
        cmd = [config.SADTALKER_PY, wrapper, config.SADTALKER_DEVICE, "--driven_audio", os.path.abspath(audio_wav),
               "--source_image", os.path.abspath(headshot), "--result_dir", work,
               "--still", "--preprocess", config.SADTALKER_PREPROCESS, "--size", "256"]
        if config.SADTALKER_ENHANCE:
            cmd += ["--enhancer", "gfpgan"]
        env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
        r = subprocess.run(cmd, cwd=config.SADTALKER_DIR, capture_output=True, text=True, timeout=1800, env=env)
        mp4s = sorted(glob.glob(os.path.join(work, "*.mp4")))
        if r.returncode != 0 or not mp4s:
            raise RuntimeError((r.stderr or r.stdout)[-500:])
        # re-encode to browser-friendly H.264/AAC
        ffmpeg("-i", mp4s[-1], "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out_mp4)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def enqueue_video(job_id, headshot, audio_wav, out_mp4, on_done) -> None:
    _jobs.put((job_id, headshot, audio_wav, out_mp4, on_done))


def _worker():
    while True:
        job_id, headshot, audio, out, on_done = _jobs.get()
        try:
            render_video(headshot, audio, out)
            on_done(job_id, "ready", None)
        except Exception as e:
            log.exception("lip-sync job %s failed", job_id)
            on_done(job_id, "failed", str(e)[:300])


threading.Thread(target=_worker, daemon=True, name="lipsync-worker").start()
