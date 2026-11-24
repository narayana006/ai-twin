#!/bin/zsh
# Start the Twin server. Requires: ollama running, ffmpeg, ~/SadTalker (optional, for lip-sync video).
cd "$(dirname "$0")"
export PYTORCH_ENABLE_MPS_FALLBACK=1 TQDM_DISABLE=1
exec .venv/bin/uvicorn app.main:app --host ${HOST:-127.0.0.1} --port ${PORT:-8700}
