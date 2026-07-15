# Barnabas ffmpeg worker

This directory defines the small purpose-built container that runs the heavy Disc Steward encode stage on Barnabas.

## Why this image

- It is intentionally small.
- It only needs `ffmpeg` and the standard Debian runtime libraries.
- It keeps the controller on Gospel and the encode work inside Barnabas's Dockerized environment.
- It stores its local compose/build artifacts under `/mnt/data1/docker/disc-steward-ffmpeg` on Barnabas, which matches the requested Docker data layout.

## Build on Barnabas

Copy this directory to Barnabas under `/mnt/data1/docker/disc-steward-ffmpeg`, then run:

```bash
cd /mnt/data1/docker/disc-steward-ffmpeg
docker compose build
```

If you prefer a direct image build:

```bash
docker build -t disc-steward-ffmpeg:bookworm /mnt/data1/docker/disc-steward-ffmpeg
```

## Smoke test

```bash
docker run --rm \
  -v /mnt/data2/media-pipeline:/mnt/data2/media-pipeline \
  -v /mnt/data1/docker/disc-steward-ffmpeg:/mnt/data1/docker/disc-steward-ffmpeg \
  disc-steward-ffmpeg:bookworm \
  ffmpeg -version
```

## How Disc Steward uses it

Disc Steward's remote runner now SSHes to Barnabas as `flan` and executes `docker run` there with the Barnabas-native pipeline paths mounted into the container. The controller still builds the job and work-order JSON, but the encode happens inside this worker image.
