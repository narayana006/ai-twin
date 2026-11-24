"""Twin on Modal: pay-per-second GPUs that scale to zero.

    modal deploy modal_app.py        # → https://<workspace>--twin-web.modal.run

Pieces:
  web      cheap CPU container: the FastAPI site, accounts, SQLite + media on a Modal Volume
  Brain    L4 GPU: chat model (Ollama gemma3:12b) + voice clone (Chatterbox) + speech-to-text (Whisper)
  LipSync  L4 GPU: SadTalker talking-head video
"""
import os
import subprocess
import time

import modal

app = modal.App("twin")
models_vol = modal.Volume.from_name("twin-models", create_if_missing=True)  # model weights, survive restarts
data_vol = modal.Volume.from_name("twin-data", create_if_missing=True)      # db, photos, voices, replies

# GPU fallback lists: Modal takes the first type that's free. L4s are cheapest but sometimes all taken,
# which left requests queued for 10+ minutes; A10G / L40S are a bit pricier and faster.
GPU = ["L4", "A10G", "L40S"]
BIG_GPU = ["H100", "A100-80GB"]
IDLE_SECONDS = 180  # how long a GPU stays warm after the last request (you pay for this idle time)
LLM_MODEL = "gemma3:12b"

# ---------------------------------------------------------------- Brain (GPU)
brain_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "curl", "zstd", "ca-certificates")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    .pip_install("chatterbox-tts", "faster-whisper", "setuptools<81", "httpx")
    .env({"HF_HOME": "/models/hf", "OLLAMA_MODELS": "/models/ollama", "TQDM_DISABLE": "1"})
)


@app.cls(gpu=GPU, image=brain_image, volumes={"/models": models_vol}, scaledown_window=IDLE_SECONDS,
         timeout=900, max_containers=3)
@modal.concurrent(max_inputs=4)
class Brain:
    @modal.enter()
    def load(self):
        import threading
        import httpx
        import torch
        self.lock = threading.Lock()
        t0 = time.time()
        lap = lambda what: print(f"[brain boot] {what}: {time.time() - t0:.1f}s", flush=True)
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                httpx.get("http://127.0.0.1:11434/api/tags", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        lap("ollama up")
        tags = [m["name"] for m in httpx.get("http://127.0.0.1:11434/api/tags").json()["models"]]
        print("[brain boot] models on volume:", tags, flush=True)
        if LLM_MODEL not in tags:  # first boot only: weights then live on the volume
            subprocess.run(["ollama", "pull", LLM_MODEL], check=True)
            models_vol.commit()
        # keep the chat model resident in GPU memory
        httpx.post("http://127.0.0.1:11434/api/generate", json={"model": LLM_MODEL, "keep_alive": -1}, timeout=600)
        lap("chat model in GPU")

        from chatterbox.tts import ChatterboxTTS
        self.tts = ChatterboxTTS.from_pretrained(device="cuda")
        lap("voice model loaded")
        from faster_whisper import WhisperModel
        try:
            self.stt = WhisperModel("small", device="cuda", compute_type="float16")
        except Exception:
            self.stt = WhisperModel("small", device="cpu", compute_type="int8")
        lap("whisper loaded")
        models_vol.commit()
        lap("volume committed")
        print("Brain ready, cuda:", torch.cuda.get_device_name(0))

    @modal.method()
    def ping(self) -> bool:
        return True

    @modal.method()
    def chat(self, messages: list[dict], num_predict: int, fmt: str | None = None) -> str:
        import httpx
        body = {"model": LLM_MODEL, "messages": messages, "stream": False, "think": False, "keep_alive": -1,
                "options": {"temperature": 0.8, "num_predict": num_predict, "num_ctx": 8192}}
        if fmt:
            body["format"] = fmt
        r = httpx.post("http://127.0.0.1:11434/api/chat", timeout=300, json=body)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    @modal.method()
    def speak(self, text: str, voice_ref_wav: bytes) -> bytes:
        import io
        import tempfile
        import torchaudio
        with tempfile.NamedTemporaryFile(suffix=".wav") as ref, self.lock:
            ref.write(voice_ref_wav)
            ref.flush()
            wav = self.tts.generate(text, audio_prompt_path=ref.name, exaggeration=0.5, cfg_weight=0.5)
        buf = io.BytesIO()
        torchaudio.save(buf, wav.cpu(), self.tts.sr, format="wav")
        return buf.getvalue()

    @modal.method()
    def transcribe(self, audio: bytes) -> str:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".audio") as f:
            f.write(audio)
            f.flush()
            segments, _ = self.stt.transcribe(f.name, vad_filter=True, beam_size=1)
            return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------- LipSync (GPU)
SADTALKER_REF = "cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8"
REL = "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc"
lipsync_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "ffmpeg", "wget", "libgl1", "libglib2.0-0")
    .pip_install("torch==2.5.1", "torchvision==0.20.1", "torchaudio==2.5.1")
    .pip_install("numpy==1.23.5", "face-alignment==1.3.5", "imageio==2.19.3", "imageio-ffmpeg==0.4.7",
                 "librosa==0.9.2", "numba==0.58.1", "resampy==0.3.1", "pydub==0.25.1", "scipy==1.10.1",
                 "kornia==0.6.8", "tqdm", "yacs==0.1.8", "pyyaml", "joblib==1.1.0", "scikit-image==0.20.0",
                 "facexlib==0.3.0", "av", "safetensors", "opencv-python-headless==4.10.0.84",
                 "setuptools<81", "wheel")
    # basicsr/gfpgan build from source against the installed torch
    .pip_install("basicsr==1.4.2", "gfpgan==1.3.8", extra_options="--no-build-isolation")
    # basicsr imports a torchvision module that was renamed
    .run_commands("sed -i 's/functional_tensor import/functional import/' "
                  "$(python -c 'import importlib.util,os;print(os.path.dirname(importlib.util.find_spec(\"basicsr\").origin))')/data/degradations.py")
    .run_commands(
        f"git clone https://github.com/OpenTalker/SadTalker.git /sadtalker && cd /sadtalker && git checkout {SADTALKER_REF}",
        "mkdir -p /sadtalker/checkpoints /sadtalker/gfpgan/weights",
        *[f"wget -q {REL}/{f} -O /sadtalker/checkpoints/{f}" for f in
          ("mapping_00109-model.pth.tar", "mapping_00229-model.pth.tar", "SadTalker_V0.0.2_256.safetensors",
           "SadTalker_V0.0.2_512.safetensors")],
        "wget -q https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth -O /sadtalker/gfpgan/weights/alignment_WFLW_4HG.pth",
        "wget -q https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth -O /sadtalker/gfpgan/weights/detection_Resnet50_Final.pth",
        "wget -q https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth -O /sadtalker/gfpgan/weights/GFPGANv1.4.pth",
        "wget -q https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth -O /sadtalker/gfpgan/weights/parsing_parsenet.pth",
    )
)


@app.cls(gpu=GPU, image=lipsync_image, scaledown_window=IDLE_SECONDS, timeout=900, max_containers=2)
class LipSync:
    @modal.method()
    def render(self, headshot_png: bytes, audio_wav: bytes, preprocess: str = "crop") -> bytes:
        import glob
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            img, wav, out = f"{d}/face.png", f"{d}/speech.wav", f"{d}/out"
            open(img, "wb").write(headshot_png)
            open(wav, "wb").write(audio_wav)
            r = subprocess.run(["python", "inference.py", "--driven_audio", wav, "--source_image", img,
                                "--result_dir", out, "--still", "--preprocess", preprocess, "--size", "256"],
                               cwd="/sadtalker", capture_output=True, text=True)
            mp4s = sorted(glob.glob(f"{out}/*.mp4"))
            if r.returncode != 0 or not mp4s:
                raise RuntimeError((r.stderr or r.stdout)[-800:])
            final = f"{d}/final.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4s[-1], "-c:v", "libx264", "-pix_fmt",
                            "yuv420p", "-c:a", "aac", "-movflags", "+faststart", final], check=True)
            return open(final, "rb").read()


# ---------------------------------------------------------------- AvatarMaker (big GPU, runs once per twin)
AVATAR_MODEL = "Qwen/Qwen-Image-Edit-2509"  # Apache-2.0: commercial use OK
avatar_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("torch==2.6.0", "torchvision==0.21.0", "diffusers>=0.36", "transformers>=4.51", "accelerate",
                 "sentencepiece", "huggingface_hub", "pillow")
    .env({"HF_HOME": "/models/hf"})
)


@app.function(image=avatar_image, volumes={"/models": models_vol}, timeout=3600)
def download_avatar_model(repo: str = AVATAR_MODEL):
    """One-time: fetch big model weights on a cheap CPU box instead of paying H100 time for it.
        modal run modal_app.py::download_avatar_model --repo Wan-AI/Wan2.2-TI2V-5B-Diffusers"""
    from huggingface_hub import snapshot_download
    snapshot_download(repo)
    models_vol.commit()


@app.cls(gpu=BIG_GPU, image=avatar_image, volumes={"/models": models_vol}, scaledown_window=60, timeout=1800,
         max_containers=1)
class AvatarMaker:
    @modal.enter()
    def load(self):
        import torch
        from diffusers import QwenImageEditPlusPipeline
        self.pipe = QwenImageEditPlusPipeline.from_pretrained(AVATAR_MODEL, torch_dtype=torch.bfloat16).to("cuda")

    @modal.method()
    def edit(self, photo_png: bytes, prompts: dict, seed: int = 7, aspect: str = "square") -> dict:
        """prompts: {key: prompt}. Each result is generated from the ORIGINAL photo to keep identity.
        aspect: "square" (avatars, 768x768) or "portrait" (9:16 dream-reel scenes, 704x1280)."""
        import io
        import torch
        from PIL import Image
        src = Image.open(io.BytesIO(photo_png)).convert("RGB")
        tw, th = (1, 1) if aspect == "square" else (9, 16)
        w = min(src.width, src.height * tw // th)
        h = w * th // tw
        left, top = (src.width - w) // 2, max(0, (src.height - h) // 3)
        src = src.crop((left, top, left + w, top + h)).resize((1024, 1024) if aspect == "square" else (720, 1280))
        out_size = (768, 768) if aspect == "square" else (704, 1280)
        out = {}
        for key, prompt in list(prompts.items())[:6]:
            img = self.pipe(image=[src], prompt=prompt, negative_prompt="blurry, deformed, extra fingers, text, "
                            "watermark, nudity, open mouth", true_cfg_scale=4.0, num_inference_steps=30,
                            generator=torch.manual_seed(seed)).images[0]
            buf = io.BytesIO()
            img.resize(out_size).save(buf, format="PNG")
            out[key] = buf.getvalue()
        return out


# ---------------------------------------------------------------- Dream Life reels
VIDEO_MODEL = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"  # Apache-2.0
video_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("torch==2.6.0", "torchvision==0.21.0", "diffusers>=0.36", "transformers>=4.51", "accelerate",
                 "sentencepiece", "ftfy",
                 "imageio", "imageio-ffmpeg", "pillow", "huggingface_hub")
    .env({"HF_HOME": "/models/hf"})
)


@app.cls(gpu=BIG_GPU, image=video_image, volumes={"/models": models_vol}, scaledown_window=60, timeout=1800,
         max_containers=3)
class VideoMaker:
    @modal.enter()
    def load(self):
        import torch
        from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
        t = time.time()
        vae = AutoencoderKLWan.from_pretrained(VIDEO_MODEL, subfolder="vae", torch_dtype=torch.float32)
        self.pipe = WanImageToVideoPipeline.from_pretrained(VIDEO_MODEL, vae=vae, torch_dtype=torch.bfloat16).to("cuda")
        print(f"[video boot] model loaded in {time.time() - t:.0f}s", flush=True)

    @modal.method()
    def animate(self, still_png: bytes, prompt: str, seed: int = 7) -> bytes:
        """~5 s, 24 fps, 704x1280 vertical clip that starts from the still."""
        import io
        import tempfile
        import torch
        from diffusers.utils import export_to_video
        from PIL import Image
        img = Image.open(io.BytesIO(still_png)).convert("RGB").resize((704, 1280))
        t = time.time()
        frames = self.pipe(image=img, prompt=prompt, negative_prompt=(
            "blurry, distorted face, deformed body, extra limbs, flicker, text, watermark, logo, static image"),
            height=1280, width=704, num_frames=121, guidance_scale=5.0, num_inference_steps=28,
            generator=torch.manual_seed(seed)).frames[0]
        print(f"[video] {time.time() - t:.0f}s to render a clip", flush=True)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            export_to_video(frames, f.name, fps=24)
            return open(f.name, "rb").read()


DIRECTOR = """You are the director of a 15-second vertical "dream life" reel starring {name}.
{name} describes a dream; you turn it into 3 short scenes and a voiceover.
Return ONLY JSON: {{"title": "...", "narration": "...", "scenes": [{{"still": "...", "motion": "..."}}, x3]}}
Rules:
- narration: first person, present tense, as if {name} is living it right now; 25-35 words; warm and natural,
  in {name}'s own way of talking ({style_hint}). Place names they mentioned are fine.
- still: one vivid sentence describing a photo of {name}: where they are, what they wear, what they are doing.
  Use generic visuals: never logos, brand names, readable signs or text.
- motion: one short sentence of natural motion for a 5-second clip (subject action + gentle camera move).
- NEVER depict or name any real person other than {name} (no celebrities, politicians, athletes). If the
  dream involves one, replace them with an unnamed generic character (e.g. "a world leader", "a famous coach")
  and do not name them in the narration either.
- Keep it wholesome and safe for work. If anything requested is explicit, violent or hateful, leave it out.
- Follow the dream literally: each scene shows one thing {name} asked for, in the same order, with the
  outfit that fits it (e.g. jogging -> running clothes, mid-jog; office -> the suit they described).
- Scenes should flow as a story (e.g. morning -> action -> big moment)."""

dream_image = modal.Image.debian_slim(python_version="3.11").apt_install("ffmpeg", "fonts-dejavu-core")


@app.function(image=dream_image, timeout=3600)
def make_dream(photo_png: bytes, voice_ref_wav: bytes, name: str, dream: str, style: str,
               style_hint: str, seed: int = 7) -> dict:
    """Script -> 3 scene stills of the person -> 3 animated clips (parallel GPUs) -> voiceover -> one reel."""
    import json
    import re
    import tempfile

    t0, timings = time.time(), {}
    lap = lambda k: timings.__setitem__(k, round(time.time() - t0)) or print(f"[dream] {k}: {timings[k]}s", flush=True)
    first = (name or "me").split()[0]
    raw = Brain().chat.remote([
        {"role": "system", "content": DIRECTOR.format(name=first, style_hint=style_hint or "casual")},
        {"role": "user", "content": f"My dream: {dream}"}], 700, "json")
    try:
        plan = json.loads(raw)
        scenes = [sc for sc in plan["scenes"] if sc.get("still")][:3]
        assert len(scenes) == 3 and plan.get("narration")
    except Exception:
        raise RuntimeError(f"Couldn't plan the reel, please rephrase your dream. ({raw[:200]})")
    lap("script")
    # Voiceover now, while the brain GPU is still warm (it idles out during the video render).
    vo_call = Brain().speak.spawn(plan["narration"], voice_ref_wav)

    looks = {"realistic": "Ultra-realistic photograph shot on a cinema camera, 35mm lens, natural light, shallow depth "
                          "of field, fine skin texture and fabric detail, cinematic colour grading, 4k",
             "game": "Next-generation AAA open-world video game cinematic, ultra-detailed environment, dramatic "
                     "ray-traced lighting, cinematic colour grading, hyper-detailed character model",
             "3d": "Polished 3D animated-film style render with soft lighting",
             "anime": "High-quality anime film style, clean line art, soft cel shading",
             "illustrated": "Clean modern digital illustration, warm painterly colours"}.get(style, "")
    keep = ("Keep the person's identity exactly: same face, skin tone, hair, facial hair and age. The person is "
            "the main subject and clearly visible. No logos, no brand names, no readable text, no other "
            "recognizable real people.")
    stills = AvatarMaker().edit.remote(
        photo_png, {f"s{i}": f"Place this exact person in a new vertical photo: {sc['still']} {looks}. {keep}"
                    for i, sc in enumerate(scenes)}, seed, "portrait")
    lap("scene stills")
    clips = list(VideoMaker().animate.starmap(
        [(stills[f"s{i}"], f"{sc.get('motion', '')} {sc['still']}", seed + i) for i, sc in enumerate(scenes)]))
    lap("video clips")
    voice = vo_call.get()
    lap("voiceover ready")

    with tempfile.TemporaryDirectory() as d:
        for i, c in enumerate(clips):
            open(f"{d}/c{i}.mp4", "wb").write(c)
        open(f"{d}/vo.wav", "wb").write(voice)
        dur = lambda p: float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                              "csv=p=0", p], capture_output=True, text=True).stdout or 0)
        L, X = dur(f"{d}/c0.mp4"), 0.5               # clip length, crossfade length
        video_len, vo_len = 3 * L - 2 * X, dur(f"{d}/vo.wav")
        hold = max(0.0, vo_len + 0.8 - video_len)    # freeze the last frame if the voiceover runs long
        tag = re.sub(r"[^A-Za-z0-9 ]", "", first)[:20] or "My"
        label = f"AI-generated · twin of {tag}"
        fc = (f"[0:v][1:v]xfade=transition=fade:duration={X}:offset={L - X}[a];"
              f"[a][2:v]xfade=transition=fade:duration={X}:offset={2 * L - 2 * X}[b];"
              f"[b]tpad=stop_mode=clone:stop_duration={hold:.2f},"
              f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{label}':"
              f"fontsize=26:fontcolor=white@0.85:box=1:boxcolor=black@0.35:boxborderw=8:x=w-tw-24:y=h-th-32[v];"
              f"[3:a]adelay=400|400,apad[aud]")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{d}/c0.mp4", "-i", f"{d}/c1.mp4",
                        "-i", f"{d}/c2.mp4", "-i", f"{d}/vo.wav", "-filter_complex", fc, "-map", "[v]",
                        "-map", "[aud]", "-t", f"{video_len + hold:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-movflags", "+faststart", f"{d}/reel.mp4"], check=True)
        lap("assembled")
        return {"mp4": open(f"{d}/reel.mp4", "rb").read(), "title": plan.get("title", "My dream"),
                "narration": plan["narration"], "scenes": scenes, "timings": timings}


# ---------------------------------------------------------------- Website (CPU)
web_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("fastapi", "uvicorn", "python-multipart", "httpx")
    .env({"TWIN_DATA_DIR": "/data", "TWIN_BACKEND": "modal", "TWIN_LLM": LLM_MODEL})
    .add_local_dir("app", "/root/app", ignore=["__pycache__", "static/.vercel", "static/vercel.json", "static/vercel.example.json", "static/.env*", "static/.gitignore", "static/.vercelignore"])
)


@app.function(image=web_image, volumes={"/data": data_vol}, max_containers=1, scaledown_window=600, timeout=900)
@modal.concurrent(max_inputs=40)
@modal.asgi_app()
def web():
    from app.main import app as fastapi_app
    return fastapi_app
