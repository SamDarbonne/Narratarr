# syntax=docker/dockerfile:1
#
# Narratarr's image. Read APP-CONTRACT.md section 11 before you change this
# file. Two stages: the frontend build, then the Python runtime.
#
# Build for linux/amd64. The reference machine is a 4-core Intel i5-6500T.
# Pass the platform on the build command, once, rather than on each FROM
# line below, so the whole multi-stage build targets the one platform:
#
#   docker build --platform linux/amd64 -t narratarr .
#
# The image holds no TTS weight and no whisper weight. Refer to the
# comment above the model-fetch section, near the end of this file.

# ---------------------------------------------------------------------------
# Stage 1: the frontend build
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend

WORKDIR /web

# Copy the lockfile first, so a change to application code does not bust
# the dependency-install layer.
COPY web/package.json web/package-lock.json* ./
RUN npm ci

COPY web/ ./
RUN npm run build
# The build writes web/dist. Refer to web/vite.config.ts.

# ---------------------------------------------------------------------------
# Stage 2: the Python runtime
# ---------------------------------------------------------------------------
# pyproject.toml pins `requires-python = ">=3.12,<3.13"`. Match it here.
FROM python:3.12-slim-bookworm AS runtime

# Required, not only a default. Refer to APP-CONTRACT.md section 11.1: the
# HuggingFace xet transport fails on at least one machine in this project,
# and the failure is confusing. A person must never have to discover this
# for themselves.
ENV HF_HUB_DISABLE_XET=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg:      abpipe's assemble and bind stages call it.
# espeak-ng:   the system voice the espeak fallback speaks through.
# libsndfile1: the audio-file library kokoro's dependency chain reads and
#              writes through.
#
# An exact Debian package version pins to one mirror snapshot, which goes
# stale and then fails the build outright. `python:3.11-slim-bookworm`
# already pins the base image; that is the reproducibility boundary this
# project chooses.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       espeak-ng \
       libsndfile1 \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY narratarr/ ./narratarr/
COPY vendor/abpipe/ ./vendor/abpipe/
COPY scripts/ ./scripts/

# --- torch: CPU wheels only. NEVER the CUDA wheels. -------------------
#
# The default PyPI index serves a CUDA-enabled build of torch on Linux.
# That build drags in several gigabytes of CUDA and cuDNN libraries that
# this image never uses, because the target machine has no GPU. Refer
# to APP-CONTRACT.md section 11.1.
#
# Installing the CPU wheel FIRST, from PyTorch's own CPU index, and
# before any package that depends on torch, is what keeps a later
# `pip install` from resolving a different, CUDA-enabled torch as a
# transitive dependency of kokoro or faster-whisper.
RUN pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu

# --- the rest of the Python dependencies -------------------------------
#
# misaki is pinned to an exact version, matching kokoro's exact version.
# Read this before you change either pin.
#
# The claim that the torch kokoro engine (kokoro_cpu) and the
# Apple-silicon engine (kokoro_mlx) produce the same phonemes for the
# same text rests on a measurement P0 made inside ONE shared install of
# misaki. Two independently resolved misaki versions could drift apart
# and silently change how a book is pronounced on one engine but not the
# other. The pin is cheap insurance against that drift. Refer to
# vendor/abpipe/CONTRACT.md section 18 for why a pronunciation change is
# invisible to QC.
# The "pipeline" extra of pyproject.toml holds the rest: faster-whisper,
# ebooklib, and the other packages vendor/abpipe needs. It declares torch,
# kokoro, and misaki too, with loose lower bounds; installing the two
# exact pins above FIRST is what keeps this step from resolving a
# different, unpinned version of either.
RUN pip install \
       "kokoro==0.9.4" \
       "misaki[en]==0.9.4" \
    && pip install ".[pipeline]" \
    && python -m spacy download en_core_web_sm \
    # en_core_web_trf is tier 1 of the homograph audit (pipeline contract
    # 18.3). misaki tags with en_core_web_sm; the audit cross-checks with the
    # transformer, and an occurrence passes only when both taggers agree.
    # Without this model the audit raises and the job fails at the
    # homographs stage, which is exactly what happened on the first real run.
    #
    # The cost is bounded. Measured on a real book: 400 of 2,042 chunks hold
    # a word from the 701-entry heteronym inventory, so the transformer runs
    # on about a fifth of the book, not all of it.
    && python -m spacy download en_core_web_trf

# =========================================================================
# THE BUILD-TIME ESPEAK WARMUP.
#
# WARNING: THIS STEP PREVENTS A SILENT DATA-LOSS FAULT. IT IS NOT AN
# OPTIONAL SLOW STEP. DO NOT DELETE IT TO SPEED UP THE BUILD.
#
# The short version: when the espeak fallback fails to construct, kokoro's
# G2P layer is built with unk="", and every word that is missing from the
# misaki lexicon is silently deleted from the rendered audio. There is no
# second warning, and QC cannot see the loss, because the transcript and
# the source both lose the same word. On a book with hundreds of foreign
# terms, this is the difference between an accented reading and missing
# words. Refer to vendor/abpipe/CONTRACT.md section 17.1 for the full
# measured history of this fault, including the traced cause: a full disk
# breaks the fallback's one-time unpack of libespeak-ng.
#
# This step buys two things a run-time check cannot buy on its own:
#
#   1. It fails the BUILD, not a customer's overnight render, when the
#      fallback cannot construct on this image. A build machine has
#      disk headroom that a five-year-old NAS at 95 percent full does
#      not.
#   2. It forces libespeak-ng's unpack to happen now, while the build
#      machine has room, so the unpacked library ships inside the image
#      itself. A container started from this image never repeats that
#      unpack, so it can never repeat the fault that a full disk causes.
#
# scripts/build_warmup_espeak.py holds the full explanation of WHY this
# reads `pipeline.g2p.fallback` directly instead of grepping a log: the
# torch kokoro package silences its own logger on import, so the log
# line this fault was first diagnosed from (APP-CONTRACT.md 11.2, point
# 2) never gets written on this engine. Reading the object is the real
# detection surface. scripts/espeak_guard.py is the log-based check kept
# for other engines; it is a secondary defence, never the only one.
#
# scripts/build_warmup_espeak.py calls
# `abpipe.engines.kokoro_cpu.KokoroCPUEngine.preflight()`, so the build
# check and the runtime check share one implementation. That method
# needs a re-vendored copy of vendor/abpipe that P0 has not landed yet
# as this Dockerfile is written; the script fails loudly and by name when
# the method is missing, rather than silently skip the check.
#
# This step downloads the real Kokoro model weights to run its probe,
# because a silent audio drop can only be detected in real audio. The
# download and its cleanup happen in this ONE RUN instruction, so the
# weights never reach a committed image layer: they exist only inside
# this instruction's temporary container filesystem, and `rm -rf` erases
# them before the layer commits. This is why the image still meets the
# "no TTS weights" rule of APP-CONTRACT.md section 11.1 despite this
# step downloading them once, mid-build.
#
# The `rm -rf` below deletes ONLY the HuggingFace hub cache. It leaves
# libespeak-ng's unpacked library exactly where espeakng_loader placed
# it, under the system temp directory, because leaving it there is the
# entire point of this step.
# =========================================================================
RUN python scripts/build_warmup_espeak.py \
    && rm -rf /root/.cache/huggingface

COPY --from=frontend /web/dist ./web/dist

# --- the non-root user --------------------------------------------------
#
# /config, /output, and /watch are mount points (refer to APP-CONTRACT.md
# section 2.1). Chowning them here covers a fresh, Docker-managed named
# volume, which inherits this directory's ownership on its first use. A
# host bind mount keeps the host directory's own ownership; give that
# host directory UID 1000 (or run the container with a matching `user:`
# override) so this non-root user can write to it. Refer to
# docker-compose.yml for both cases.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin narratarr \
    && mkdir -p /config /output /watch \
    && chown -R narratarr:narratarr /app /config /output /watch

# The numeric form, not the name, so the ID resolves the same way even
# on a host whose /etc/passwd this image never sees.
USER 1000:1000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# `--factory` matches narratarr/config.py's documented seam: `create_app()`
# takes no argument and returns the FastAPI application. Refer to
# APP-CONTRACT.md section 14.1.
CMD ["sh", "-c", "uvicorn narratarr.api:create_app --factory --host 0.0.0.0 --port ${NARRATARR_PORT:-8000}"]
