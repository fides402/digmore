# DIGMORE engine — Hugging Face Spaces (Docker SDK). Serves FastAPI on 7860.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch CPU wheels first (laion-clap would otherwise pull the large CUDA build).
# torchvision is NOT optional: laion_clap.clap_module.utils imports
# torchvision.ops.misc.FrozenBatchNorm2d at import time, so without it every
# CLAP call dies with ModuleNotFoundError — which stayed invisible until
# /api/embed became the first endpoint on the Space to actually load the
# model (profiles ship as pre-built .npy, so nothing else ever touched it).
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY engine/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY engine/ /app/
COPY web/    /web/

# Persistence: attach a HF persistent disk at /data to keep profiles/listened/seen.
ENV DIGMORE_DATA=/data \
    HF_HOME=/data/hf \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
