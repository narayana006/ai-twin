# Twin: your AI self

An open-source app where anyone can create an **AI twin of themselves**. The twin looks like
them (a stylized avatar from one photo), sounds like them (a voice clone from a 15-second
read-aloud), thinks and talks like them (from a questionnaire and writing samples), and can
**live their dream life** in short vertical video reels narrated in their own voice.

Every model is open-source and self-hosted. GPUs are rented per second on
[Modal](https://modal.com), so there's no per-user API bill and the app costs nothing when idle.

## Features

- **Try without signing up.** Guest twins are private, have daily limits, and are deleted after
  7 days unless the person saves them with an email.
- **Coached selfie.** The webcam checks the shot live in the browser (face size, centered, head
  level, eye-level camera, light, glare) before it's used. Uploaded photos get the same check.
- **Avatar designer.** The photo becomes stylized avatars (3D, illustrated, anime, studio,
  watercolor, comic), with outfits, accessories and backgrounds to choose from. The original photo
  stays private.
- **Voice in one take.** The person reads a short script containing 3 random code words (this
  proves the voice is theirs), and their twin greets them back in their cloned voice.
- **Talk to the twin.** Text chat, push-to-talk, or hands-free conversation. It speaks in the
  first person, in the person's style, and learns from corrections and new facts.
- **Dream Life reels.** "I'm in New York: coffee, a jog by the Hudson, then work downtown in a
  suit" becomes a 15-second vertical reel of the person living it (about 5–6 min, about $0.80).
- **Share.** Saved twins can be made public at `/t/<slug>`.

## How it works

| Piece | Model | License | Where it runs |
|---|---|---|---|
| Brain | [gemma3:12b](https://ollama.com/library/gemma3) via Ollama | Gemma terms | L4 / A10G / L40S |
| Voice clone | [Chatterbox](https://github.com/resemble-ai/chatterbox) | MIT | same GPU |
| Hearing | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) small | MIT | same GPU |
| Avatars and scene stills | [Qwen-Image-Edit-2509](https://huggingface.co/Qwen/Qwen-Image-Edit-2509) | Apache-2.0 | H100 / A100-80GB |
| Reel video | [Wan2.2-TI2V-5B](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B-Diffusers) | Apache-2.0 | H100 / A100-80GB, 3 in parallel |
| Talking-head lip-sync | [SadTalker](https://github.com/OpenTalker/SadTalker) | Apache-2.0 | L4 / A10G / L40S |
| Photo coaching | [MediaPipe face detector](https://ai.google.dev/edge/mediapipe) | Apache-2.0 | the user's browser |

```
browser ── Vercel (static UI) ──/api, /media──▶ Modal "web" (FastAPI, SQLite on a Volume, CPU)
                                                   ├─▶ Brain      (chat + voice + hearing)
                                                   ├─▶ AvatarMaker (photo → avatars / scenes)
                                                   ├─▶ VideoMaker  (scene → 5 s clip)
                                                   ├─▶ LipSync     (talking-head video)
                                                   └─▶ make_dream  (orchestrates a reel)
```

Code: `app/main.py` (API), `app/engines.py` (local or cloud engines), `app/persona.py`
(questionnaire and persona prompt), `app/avatar_prompts.py`, `app/static/` (UI, no build step),
`modal_app.py` (cloud GPUs).

## Deploy your own (Modal + Vercel)

Requirements: Python 3.11, [uv](https://docs.astral.sh/uv/), a Modal account (set a spending limit
under Billing), and optionally the Vercel CLI.

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python modal fastapi uvicorn python-multipart httpx
.venv/bin/modal setup                                   # log in to Modal
.venv/bin/python build_images.py                         # build GPU images one at a time
.venv/bin/modal run modal_app.py::download_avatar_model  # one-time: avatar model weights (~58 GB, on CPU)
.venv/bin/modal run modal_app.py::download_avatar_model --repo Wan-AI/Wan2.2-TI2V-5B-Diffusers
.venv/bin/modal deploy modal_app.py                      # prints https://<workspace>--twin-web.modal.run
```

The site already works at the Modal URL. To serve the UI from Vercel's CDN on your own domain:

```bash
./deploy_vercel.sh https://<workspace>--twin-web.modal.run
```

The chat model downloads itself on the brain GPU's first boot, so the very first reply takes a few
minutes. After that, weights load from a Modal Volume.

## Run fully local (Mac or Linux, no cloud)

Local mode covers chat, voice cloning, speech-to-text and SadTalker lip-sync. Avatars and Dream
Life reels need the cloud GPUs.

```bash
ollama pull gemma3:12b
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python chatterbox-tts faster-whisper "setuptools<81" \
    fastapi "uvicorn[standard]" python-multipart httpx
git clone https://github.com/OpenTalker/SadTalker ~/SadTalker   # optional: lip-sync (see its README for weights)
./run.sh                                                      # http://127.0.0.1:8700
```

Settings (environment variables): `TWIN_LLM`, `TWIN_WHISPER`, `SADTALKER_DIR`, `SADTALKER_DEVICE`
(`mps`/`cuda`/`cpu`), `SADTALKER_PREPROCESS`, `PORT`, `HOST`.

## Costs (Modal list prices, approximate)

| Action | GPU time | Cost |
|---|---|---|
| Chat reply with voice (warm) | ~4 s | < $0.01 |
| 4 starter avatars | ~2 min H100 | ~$0.15 |
| Dream Life reel | ~6 min across 4–5 GPUs | ~$0.80 |
| Idle | none (scales to zero) | $0 |

A GPU stays warm for 1–3 minutes after use (`IDLE_SECONDS` in `modal_app.py`).

## Safety built in

- **Voice ownership:** every voice recording must contain fresh, single-use code words.
- **Honesty:** the twin speaks as the person, but if someone sincerely asks whether it's an AI, it
  says it's their AI twin. It never makes commitments (money, contracts, meetings) on their behalf.
- **No real people in reels:** apart from the user, real people (celebrities, politicians) are
  never depicted or named; they become generic characters. Every reel carries an "AI-generated"
  label (see EU AI Act Art. 50 for disclosure rules).
- **Content filters** on avatar details and dream prompts; guests can't share publicly; rate limits
  on guests and generations.
- **Privacy:** original photos, voice samples and consent recordings are never served publicly.
  "Delete my twin" removes everything. Photo coaching runs entirely in the browser.

Laws on voice cloning, likeness and synthetic media vary by country. If you run this for other
people, it's your responsibility to comply with them.

## Before a big public launch

SQLite on a Modal Volume with one web container is fine for an MVP. For scale: move to Postgres,
use object storage for media, add email verification, HTTPS on your domain, abuse monitoring, and
speaker verification between the consent clip and the voice sample.

## License

MIT (see `LICENSE`). Models downloaded at runtime keep their own licenses (table above); the
self-hosted MediaPipe files in `app/static/vendor/mediapipe` are Apache-2.0.
