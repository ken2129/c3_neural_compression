# Issue24: single-image optimization-budget cost pilot

This directory records the manual GPU pilot authorized in chat on 2026-11-10.
The experiment uses the first image in the original Issue49 calibration order,
the fixed codec source `0c3a60c703bd41956b6558d527470350bcc7dd5d`, and the
original detector-input protocol. It does not enable the resident controller.

| Condition | Objective | Noise steps | Maximum STE | Lambda |
| --- | --- | ---: | ---: | ---: |
| A | C3 image MSE + rate | 500 | 50 | 0.002173243273017414 |
| B | C3 image MSE + rate | 1000 | 50 | 0.002173243273017414 |
| C | C3 prime, image MSE + 0.1 feature L1 + rate | 500 | 50 | 0.043644024521953165 |
| D | C3 prime, image MSE + 0.1 feature L1 + rate | 1000 | 50 | 0.043644024521953165 |

The LR, soft-rounding and Kumaraswamy schedules follow the normalized progress
of each noise budget. Each condition starts independently with seed 20261004
and the original image fold-in. FPN layers 0–3 each have weight 0.25. The teacher
is the original frozen torchvision Faster R-CNN ResNet-50 FPN COCO_V1.

The fixed order was two connection attempts, then A/C/B/D. The first image-only
connection attempt failed before its first optimizer step because the launcher
enabled only the CUDA backend while the fixed codec explicitly uses a CPU
backend for optimizer bookkeeping. The launcher was corrected to `cuda,cpu`;
the runner still rejects a non-GPU default backend. The failed attempt was
retained and was not repeated. The remaining five jobs stayed inside the
original six-attempt limit and the original two-hour wall window.

## Scope and artifacts

Execution stays inside the existing work container, with no new host file,
service, mount or container. Only the selected idle GPU is used. CPU affinity
covers two cores. Process-group RSS is sampled once per second and stopped
above 8 GiB; this is not a cgroup hard memory limit. Other checks cover 7200
seconds, 6 GiB of additional GPU-stage outputs/cache/temp, a 10 GiB free-disk
reserve, input/config/teacher changes and unexpected competing compute
processes. Dependency setup is separate from GPU-stage artifact growth.

The private root retains the original image/annotation/manifest, pinned source
checkout, isolated venv, teacher weights, exact plans and approvals, job hashes,
logs, initial RNGs, trained and quantized parameters, native PNGs and receipts.
These are not committed. No new Box destination is used.

The six measured-execution scripts in `runtime/` preserve the
code used for the repaired execution. The measured continuation script was
copied to the private root as `pilot/sequence.py`; it is published here as
`runtime/resume_sequence.py`. `storage_runner.py` is the unchanged Issue49
retention wrapper. The fresh-run sequence is supplied separately so a later
reproduction need not manufacture a failed connection attempt.

To reproduce, provide a separately approved private root with `assets/`,
`code/c3_neural_compression/`, `venv/`, `pilot/`, `control/`, `runs/`, `results/`,
`evidence/` and `cache/`. Copy the runtime scripts to `pilot/` and regenerate the
private plan, asset/wrapper/job hashes and approval for that exact root. Each
private job supplies the mode, variant, noise/STE budgets, manifest/subset,
private output/evidence directories and resource limits required by
`supervise.py`. An approval from this run or Issue49 must not be reused.
Run the fixed sequence under the external 7200-second `timeout` command saved
with the private plan. There is no enabled switch or background service here.

## Audit and figures

```sh
python experiments/issue24/collect.py PRIVATE_ROOT OUTPUT_DIRECTORY
python experiments/issue24/plot.py OUTPUT_DIRECTORY
python -m unittest discover -s experiments/issue24 -p test_supervision.py -v
```

The collector uses an explicit export list. It checks asset and wrapper
identity, the original input/order, signatures, independent RNG correspondence,
bit components/denominator, retained parameter/PNG/metrics hashes and frozen
teacher checksums. An independent Pillow resize must match the previously
verified D2 byte hash before computing saved-PNG PSNR. It never invokes a GPU.

`psnr_quantized` in the codec's JSON already evaluates rounded RGB pixels from
quantized network parameters. It is exported as `psnr_reported_quantized_db`.
`psnr_saved_png_db` is computed independently from the PNG and must agree
within 0.0001 dB; their small differences reflect floating-point arithmetic.
The primary cost is worker wall time, which includes import/load, compilation,
optimization, quantization, saving and process exit. The codec's own `total`
is evaluated before final saving and is named accordingly in the export.

Fresh processes share a persistent JAX compilation cache, so time measurements
include order effects. Teacher/load/JIT/noise/STE are not timed independently.
Full-run training logs retain the original ten-step interval rather than every
step. Memory peaks are sampled. The logged intermediate bpp is latent-only;
the final bpp includes latent, synthesis-network and entropy-network bits.
The pilot has one previously used calibration image, fixed lambda per method,
and no AP/BD-rate result or matched-rate superiority claim.

Public numbers and figures are under
[`docs/research/assets/issue24_budget_pilot/`](../../docs/research/assets/issue24_budget_pilot/).
The full Issue24 study remains open. Refs #24, #35, #49.
