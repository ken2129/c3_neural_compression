# syntax=docker/dockerfile:1

# The project was tested with Python 3.10 and its pinned JAX dependencies target
# CUDA 12. GPU access is provided at runtime by the NVIDIA Container Toolkit.
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PYTHONPATH=/workspace

# ffmpeg and 7z are needed by download_uvg.sh; wget downloads the UVG archives.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        p7zip-full \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Keep the repository directory name because imports use the
# c3_neural_compression package from its parent directory.
WORKDIR /workspace/c3_neural_compression

COPY requirements.txt ./
# requirements.txt currently combines torch 2.7 with torchvision 0.17.
# Install the matching CPU-only pair (torch is only a data loader here), then
# install the remaining project pins unchanged.
RUN python -m pip install --upgrade pip setuptools wheel \
    && sed '/^torch==/d; /^torchvision==/d' requirements.txt \
        > /tmp/requirements-without-torch.txt \
    && python -m pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.2.0 \
        torchvision==0.17.0 \
    && python -m pip install --requirement /tmp/requirements-without-torch.txt \
    && rm /tmp/requirements-without-torch.txt

COPY . ./

WORKDIR /workspace

# An interactive shell is a safe default; experiments require an explicit
# config and can be selected at docker run time.
CMD ["bash"]
