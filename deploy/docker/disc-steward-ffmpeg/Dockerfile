FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-venv \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/disc-steward-ocr-venv \
    && /opt/disc-steward-ocr-venv/bin/pip install --no-cache-dir rapidocr-onnxruntime

COPY ocr_worker.py /opt/disc-steward-ocr-worker.py

WORKDIR /mnt/data2/media-pipeline

CMD ["ffmpeg"]
