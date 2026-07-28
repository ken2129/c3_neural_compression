# C3 (neural compression)

This repository contains code for reproducing results in the paper
*C3: High-performance and low-complexity neural compression from a single image or video*
(abstract and arxiv link below).

C3 paper link: https://arxiv.org/abs/2312.02753

Project page: https://c3-neural-compression.github.io/

*Abstract: Most neural compression models are trained on large datasets of images or videos
in order to generalize to unseen data. Such generalization typically requires
large and expressive architectures with a high decoding complexity. Here we
introduce C3, a neural compression method with strong rate-distortion (RD)
performance that instead overfits a small model to each image or video
separately. The resulting decoding complexity of C3 can be an order of magnitude
lower than neural baselines with similar RD performance. C3 builds on COOL-CHIC
(Ladune et al.) and makes several simple and effective improvements for images.
We further develop new methodology to apply C3 to videos. On the CLIC2020 image
benchmark, we match the RD performance of VTM, the reference implementation of
the H.266 codec, with less than 3k MACs/pixel for decoding. On the UVG video
benchmark, we match the RD performance of the Video Compression Transformer
(Mentzer et al.), a well-established neural video codec, with less than 5k
MACs/pixel for decoding.*

This code can be used to train and evaluate the C3 model in the paper, that can
be used to reproduce the empirical results of the paper, including the
psnr/per-frame-mse values
(logged as `psnr_quantized` / `per_frame_distortion_quantized`) and the
corresponding bpp values (logged as `bpp_total`) for each image / video patch.
We have tested this code on a single NVIDIA P100 and V100 GPU, with Python 3.10.
Around 20M / 300M / 25G of hard disk space is required to download the
Kodak / CLIC2020 / UVG datasets respectively.

C3 builds on top of [COOL-CHIC](https://arxiv.org/abs/2212.05458) with official
[PyTorch implementation](https://github.com/Orange-OpenSource/Cool-Chic).

## Rate Distortion values and MACs per pixel

The rate-distortion values and MACs per pixel for C3 and other compression baselines can be found in the files under the `baselines` directory.

## Setup

We recommend installing this package into a Python virtual environment.
To set up a Python virtual environment with the required dependencies, run:

```shell
# create virtual environment
python3 -m venv /tmp/c3_venv
source /tmp/c3_venv/bin/activate
# update pip, setuptools and wheel
pip3 install --upgrade pip setuptools wheel
# clone repository
git clone https://github.com/google-deepmind/c3_neural_compression.git
# Navigate to root directory
cd c3_neural_compression
# install all required packages
pip3 install -r requirements.txt
# Include this directory in PYTHONPATH so we can import modules.
export PYTHONPATH=${PWD}:$PYTHONPATH
```

Once done with virtual environment, deactivate with command:

```shell
deactivate
```

then delete venv with command:

```shell
rm -r /tmp/c3_venv
```

## Setup UVG dataset (optional)
The Kodak and CLIC2020 image datasets are automatically downloaded via the data loader in `utils/data_loading.py`. However the UVG(UVG-1k) dataset requires some manual preparation.
Here are some instructions for Debian linux.

To set up the UVG dataset, first install `7z` (to unzip .7z files into .yuv files) and `ffmpeg` (to convert .yuv files to .png frames) via commands:

```shell
sudo apt-get install p7zip-full
sudo apt install ffmpeg
```

Then run `bash download_uvg.sh` after modifying the `ROOT` variable in `download_uvg.sh` to be the desired directory for storing the data. Note that this can take around 20 minutes.

## Run experiments
Set the hyperparameters in `image.py` or `video.py` as desired by modifying
the config values. Then inside the virtual environment,
make sure `pwd` is the parent directory of `c3_neural_compression` and run the
[JAXline](https://github.com/deepmind/jaxline) experiment via command:

```shell
python3 -m c3_neural_compression.experiments.image --config=c3_neural_compression/configs/kodak.py
```

or

```shell
python3 -m c3_neural_compression.experiments.image --config=c3_neural_compression/configs/clic2020.py
```

or

```shell
python3 -m c3_neural_compression.experiments.video --config=c3_neural_compression/configs/uvg.py
```

Note that for the UVG experiment, the value of `exp.dataset.root_dir` must match the value of the `ROOT` variable used for `download_uvg.sh`.


## COCO object detection evaluation

For a GPU environment isolated from the C3/JAX dependencies, either build
`Dockerfile.detector` or create a dedicated virtual environment:

```shell
python -m venv /workspace/.venv-detector
/workspace/.venv-detector/bin/python -m pip install \
  --index-url https://download.pytorch.org/whl/cu121 \
  torch==2.2.0 torchvision==0.17.0
/workspace/.venv-detector/bin/python -m pip install \
  -r c3_neural_compression/requirements-detector.txt
```

The commands below should be run with `/workspace/.venv-detector/bin/python`
when the host C3 environment contains CPU-only PyTorch.

The independent evaluator in `evaluation/coco_detection.py` accepts original
COCO validation images or reconstructed images with the same COCO `file_name`
layout. It rejects missing files, duplicate image IDs/file mappings, and size
mismatches before loading detector weights.

```shell

# Validation only: does not load weights or run inference.
python -m c3_neural_compression.evaluation.coco_detection \
  --image-root=/workspace/datasets/coco/val2017 \
  --annotation-file=/workspace/datasets/coco/annotations/instances_val2017.json \
  --output-dir=/workspace/outputs/detector_eval/original \
  --validate-only

# Actual evaluation (do not run until data, weights, pycocotools, and CUDA are ready).
python -m c3_neural_compression.evaluation.coco_detection \
  --image-root=/workspace/datasets/coco/val2017 \
  --annotation-file=/workspace/datasets/coco/annotations/instances_val2017.json \
  --output-dir=/workspace/outputs/detector_eval/original \
  --device=cuda --batch-size=1 \
  --visualization-limit=10 --visualization-score-threshold=0.5
```

For reconstructions, change only `--image-root` and `--output-dir`. A fixed
subset can be supplied as a JSON list or newline-separated IDs with
`--subset-ids`. Outputs are `predictions.json`, `metrics.json`, `config.json`,
and optional overlays. The visualization threshold affects only overlays; COCO
evaluation receives all candidates returned by the detector.

## Phase 3 JAX/PyTorch gradient bridge

Phase 3 keeps the validated JAX environment and installs Blackwell-capable
PyTorch in an isolated environment. Build `Dockerfile.phase3`, or create the
equivalent environment inside the existing container:

```shell
python -m venv --system-site-packages /workspace/.venv-c3-phase3
/workspace/.venv-c3-phase3/bin/python -m pip install --upgrade \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch==2.7.1 torchvision==0.22.1
```

Run the non-JIT Gate A bridge from `/workspace`. Detector weights persist
under `/workspace/datasets/torch`, and metrics are written outside the image:

```shell
XLA_PYTHON_CLIENT_PREALLOCATE=false \
TORCH_HOME=/workspace/datasets/torch \
CUDA_VISIBLE_DEVICES=0 \
/workspace/.venv-c3-phase3/bin/python \
  -m c3_neural_compression.experiments.phase3_bridge \
  --image=/workspace/datasets/kodak/kodim01.png \
  --feature-layer=0 \
  --output-dir=/workspace/outputs/c3_phase3_bridge
```

Gate A shares concrete tensors with DLPack and returns the PyTorch-computed
image gradient through `jax.custom_vjp`. It intentionally rejects `jax.jit`;
the JIT/external-call boundary is a separate Gate B decision.

The C3 optimization loop keeps the original JIT path whenever
`loss.machine_weight` is zero. Two deterministic, two-step GPU integration
configs exercise the non-JIT path:

```shell
# image distortion + machine feature distortion + rate
CUDA_VISIBLE_DEVICES=0 /workspace/.venv-c3-phase3/bin/python \
  -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/kodak_machine_smoke.py

# machine feature distortion + rate (image distortion weight is zero)
CUDA_VISIBLE_DEVICES=0 /workspace/.venv-c3-phase3/bin/python \
  -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/kodak_machine_only_smoke.py
```

The configs write reconstructions and JSON metrics under
`/workspace/outputs/c3_machine_smoke` and
`/workspace/outputs/c3_machine_only_smoke`. The JSON records the final feature
distortion, detector freeze/checksum checks, and objective weights. At this
Gate A stage, network quantization candidates are still selected with the
original image rate-distortion criterion; the JSON marks this explicitly.

### RTX 50-series / Blackwell smoke test

The original JAX 0.4.24 environment is retained in `requirements.txt`. The
Docker image installs the separately pinned `requirements-blackwell.txt` by
default; build with `--build-arg C3_JAX_PROFILE=official` to retain only the
original environment.

From `/workspace`, run the short, non-benchmark Kodak pipeline check with:

```shell
CUDA_VISIBLE_DEVICES=0 python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/kodak_smoke.py
```

It uses one image and writes its reconstruction, config, and metrics under
`/workspace/outputs/c3_baseline_smoke`. The reported rates are entropy
estimates for latents and quantized network parameters. This repository does
not generate an arithmetic/range-coded bitstream, so these values are not
actual file-size bpp measurements.

After the smoke test succeeds, run the same image with the official optimization
step counts using:

```shell
CUDA_VISIBLE_DEVICES=0 python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/kodak_baseline.py
```

This writes to `/workspace/outputs/c3_baseline_official` and can take several
hours on a single GPU.

The official config logs live metrics to the `c3-neural-compression` W&B
project and saves a resumable noise-optimization checkpoint every 5,000 steps.
Authenticate without placing the API key in source control:

```shell
wandb login
```

Re-running the same command automatically resumes from the latest checkpoint
under `/workspace/outputs/c3_baseline_official/checkpoints`.

## Citing this work

If you use this code in your work, we ask you to please cite our work:

```latex
@article{c3_neural_compression,
  title={C3: High-performance and low-complexity neural compression from a single image or video},
  author={Kim, Hyunjik and Bauer, Matthias and Theis, Lucas and Schwarz, Jonathan Richard and Dupont, Emilien},
  journal={arXiv preprint arXiv:2312.02753},
  year={2023}
}
```

## License and disclaimer

Copyright 2024 DeepMind Technologies Limited

All software is licensed under the Apache License, Version 2.0 (Apache 2.0);
you may not use this file except in compliance with the Apache 2.0 license.
You may obtain a copy of the Apache 2.0 license at:
https://www.apache.org/licenses/LICENSE-2.0

All other materials are licensed under the Creative Commons Attribution 4.0
International License (CC-BY). You may obtain a copy of the CC-BY license at:
https://creativecommons.org/licenses/by/4.0/legalcode

Unless required by applicable law or agreed to in writing, all software and
materials distributed here under the Apache 2.0 or CC-BY licenses are
distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the licenses for the specific language governing
permissions and limitations under those licenses.

This is not an official Google product.
