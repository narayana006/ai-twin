import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TWIN_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "twin.db")
MEDIA_DIR = os.path.join(DATA_DIR, "media")      # served publicly under unguessable names
PRIVATE_DIR = os.path.join(DATA_DIR, "private")  # voice samples & consent: never served

BACKEND = os.environ.get("TWIN_BACKEND", "local")   # local | modal
MODAL_APP = os.environ.get("TWIN_MODAL_APP", "twin")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("TWIN_LLM", "gemma3:12b")
WHISPER_MODEL = os.environ.get("TWIN_WHISPER", "small")

SADTALKER_DIR = os.path.expanduser(os.environ.get("SADTALKER_DIR", "~/SadTalker"))
SADTALKER_PY = os.path.expanduser(os.environ.get("SADTALKER_PY", os.path.join(SADTALKER_DIR, ".venv/bin/python")))
SADTALKER_DEVICE = os.environ.get("SADTALKER_DEVICE", "mps")          # mps | cuda | cpu
SADTALKER_PREPROCESS = os.environ.get("SADTALKER_PREPROCESS", "crop")  # crop (fast) | full (whole photo)
SADTALKER_ENHANCE =os.environ.get("SADTALKER_ENHANCE", "0") == "1"

MAX_UPLOAD_MB = 40
MIN_VOICE_SECONDS = 8

for d in (DATA_DIR, MEDIA_DIR, PRIVATE_DIR, os.path.join(DATA_DIR, "tmp")):
    os.makedirs(d, exist_ok=True)
