# Issue 20: CPU preparation and GPU handoff

This is a **new fixed implementation baseline**, not a reproduction of the
summer-seminar checkpoints/results. Those original artifacts remain unverified.
No AP or rate–AP result is supplied by CPU unit tests.

## Frozen source and conditions

- C3 base: `fcc3cbebdf5556141b59d624b163295b704edab8`
- PR14 inspected: `40b043f30ab3fb9d179bd474d77795e856874229`; only its
  image-ID-folded optimization RNG change is included. Initialization remains
  C3's shared initialization RNG; noise optimization RNG is image-ID-folded.
- TransTIC: `1132f62cbddc0122aa19f620453c3b0c4de49b2c`
- Common evaluation detector: Detectron2 `COCO-Detection/faster_rcnn_R_50_FPN_3x.yaml`,
  candidate `model_final_280758.pkl`, local SHA256 mandatory. **Identity with
  the detector used in the TransTIC paper is unverified.** This is a selected
  common-detector implementation comparison, not a paper reproduction.
- The same manifest, detector checkpoint, detector config, box postprocessing,
  COCO IDs and bbox evaluator are used for every condition. Never filter the
  image set by detected objects or successful runs.

| Property | C3 image / combined | TransTIC det1–4 |
|---|---|---|
| Codec input | Original RGB image, original H×W | D2 mapper short edge 800/max1333, then bilinear resize up independently to multiples of 256 (not padding) |
| Codec output | Native rounded/clipped uint8 PNG at original H×W | Float RGB, bilinear resized back to mapper H×W, then clamped [0,1] |
| Evaluation | Native PNG mapped to short800/max1333 | Already at mapper H×W |
| Shared detector tensor | RGB→BGR×255; D2 pixel mean/std; right/bottom replicate pad to32; unpadded H×W retained; boxes scaled to original H×W | Same |
| Feature-loss detector | torchvision Faster R-CNN R50-FPN COCO_V1; frozen | TransTIC training architecture is D2 R50-FPN; exact original weights unknown |
| Feature loss | Combined L1, layers0–3, weights .25 each; coefficient .1 | Published model; MSE P2–P6 during original training |
| Image loss | MSE coefficient1 | Published model training differs |
| Estimated transmitted bits | Latents + quantized synthesis network + entropy network | y+z likelihood bits |
| Additional shared information | Architectures/config/entropy conventions assumed known; container/header overhead not measured | Pretrained decoder/prompt weights shared; weights not charged per image, no container overhead |

C3 PNG precision versus TransTIC float output is an explicit method difference.
Neither codec's training loss or output precision is silently changed to claim
matched conditions. C3 feature-loss detector differs from the common evaluation
detector. A matched-feature-detector experiment would be a separately named variant.

Common dataset rate is `sum(estimated_bits) / sum(original_height*original_width)`.
The row schema retains original, detector-resized and codec-resized dimensions.
Native TransTIC mean(bits/detector_pixels) is auxiliary only, never the comparison
rate. These are entropy estimates, **not actual bitstream file sizes**.

## CPU validation

From the directory containing `c3_neural_compression`, with Pillow, NumPy,
pytest, ml-collections, absl-py and jaxline installed:

```sh
python -m pytest -q c3_neural_compression/tests/test_comparison.py \
  c3_neural_compression/tests/test_comparison_config.py \
  c3_neural_compression/tests/test_comparison_report.py \
  c3_neural_compression/tests/test_comparison_attempt.py \
  c3_neural_compression/tests/test_comparison_source_hash.py \
  c3_neural_compression/tests/test_transtic_adapter.py \
  c3_neural_compression/tests/test_shared_detector.py \
  c3_neural_compression/tests/test_phase4.py \
  c3_neural_compression/tests/test_coco_detection.py \
  c3_neural_compression/tests/test_coco_manifests.py
```

Torch-dependent tests require CPU PyTorch. Synthetic/mock outputs never count as
an actual codec checkpoint, detector inference, or COCO comparison result.
See `issue20_validation.md` for the actually executed checks and dependency versions.

## GPU stage: assets, then smoke, then pilot

Use separate C3 and TransTIC/Detectron2 environments. Original C3 requirements
and current accelerator profiles are different: the CPU helper-test environment
is **not** a validated GPU training lockfile. Pin and record the actual CUDA,
PyTorch, JAX, Detectron2 commit and compiled extension versions before comparison.
Do not install the legacy C3 requirements wholesale into the TransTIC environment.

1. Obtain a small explicitly chosen COCO val2017 subset and its original
   `instances_val2017.json`. No full 5,000-image download is required. Start with
   the checked-in `manifests/coco2017/smoke.json` (2 images). Freeze the image IDs
   before measuring AP; do not select favorable images or seeds.
2. Acquire common detector and det1–4 weights from official sources. Record
   downloaded SHA256; file existence, successful loading, and successful inference
   are separate gates. TransTIC's published URLs do not publish trusted digests.
   Review licensing before reuse: root Apache-2.0 plus retained bundled CompressAI
   notices; no blanket assumption of identical terms for every file/weight.
3. Clone TransTIC and checkout the exact commit above. Install its native
   CompressAI extension and dependencies in its own environment. Install an
   explicitly pinned, compatible Detectron2 source build. `examples/detection.py`
   itself is not used: its test mode still loads training/Kodak paths.
4. Build/validate the frozen manifest (all commands below from repository parent):

```sh
python -m c3_neural_compression.evaluation.comparison manifest \
  --annotation-file "$COCO_ANNOTATIONS" --subset-ids "$SUBSET_IDS" \
  --image-root "$COCO_ROOT" --output frozen_manifest.json
python -m c3_neural_compression.evaluation.comparison check \
  --manifest frozen_manifest.json
```

5. In the C3 GPU environment, run both **smoke-only** configurations first:

```sh
export C3_COCO_ROOT="$COCO_ROOT" C3_COCO_ANNOTATIONS="$COCO_ANNOTATIONS"
export C3_SUBSET_IDS="$SUBSET_IDS" C3_OUTPUT_ROOT=/absolute/path/attempt_001
export XLA_PYTHON_CLIENT_PREALLOCATE=false
python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/coco2017_comparison.py:smoke_image_rd1e3
python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/coco2017_comparison.py:smoke_combined_rd1e3
```

Smokes are 2 noise steps/0 STE, not research rate points. Pilot configuration is
500 noise/maximum50 STE, seed20261004, rd weights3e-4/1e-3/3e-3, combined weight.1.
This short budget is for feasibility/rate calibration, not claimed convergence.
Record wall time/memory for smoke before setting a total GPU budget and subset
size. Both C3 conditions must receive the same frozen budget. W&B is disabled.
The comparison configuration opts into a fresh-attempt guard. Before training,
it refuses a nonempty output root and exclusively creates a UUID ledger. A
failed/interrupted attempt cannot be resumed or evaluated; choose a new
C3_OUTPUT_ROOT. Ordinary non-comparison C3 resume behavior is unchanged.
Use `pilot_image_rd1e3`, `pilot_combined_rd1e3`, etc. only after smoke succeeds.

6. Copy `configs/comparison/*.example.json` outside the repo; fill paths and
   hashes. For combined change `condition` to `c3_combined` and use its output
   root. For each det1–4 use its checkpoint and quality1–4. Hash/check config:

```sh
sha256sum /path/to/model_final_280758.pkl /path/to/det_1.pth.tar
python -m c3_neural_compression.evaluation.comparison_runner \
  --config /path/to/condition.json --output-dir /path/to/run_001 --validate-only
python -m c3_neural_compression.evaluation.comparison_runner \
  --config /path/to/condition.json --output-dir /path/to/run_001
```

`--validate-only` checks assets, not model loading/inference. Evaluation requires
an empty output directory. It writes manifest/config/environment/detector
provenance, per-image bits/dimensions/time, predictions, rate summary and COCO
metrics. C3 optimization timings and signatures are retained. Failure writes a
ledger/status and stops; **no partial AP** is produced. Retry only for a logged
technical failure with the same config/seed in a new attempt directory. Do not
choose retries based on AP; retain failed attempts and report them. C3 output
roots must contain exactly the selected IDs and one signature, with no duplicate
attempts. The completed comparison_attempt.json ledger binds every metrics,
reconstruction and config file by SHA256 and binds metrics to its attempt UUID;
missing, running, failed, stale or mixed-attempt outputs are rejected. C3 records
the SHA256 of the exact encoded source bytes decoded during loading, and discovery
requires it to match the frozen manifest. Matching an ID/name/size alone is not
sufficient. Legacy outputs without this source/attempt provenance must be rerun;
provenance is never invented after the fact. Nothing resumes silently.

7. Collect complete runs only:

```sh
python -m c3_neural_compression.evaluation.comparison_report \
  --run-dir /path/to/c3_image_run --run-dir /path/to/c3_combined_run \
  --run-dir /path/to/transtic_det1_run --output comparison.json
```

Repeat `--run-dir` for other measured rate points. The collector requires the
same manifest and detector metadata and reaggregates rates from raw rows.
Compare measured overlapping bpp ranges, not equal lambda labels. If no overlap,
calibrate C3 rates using rate data, then freeze them before final AP evaluation.
No interpolation/extrapolation or BD-rate claim is automated.

## Interpretation gates

- No claim about relative performance until all three methods complete the same
  manifest and detector pipeline at overlapping measured rates
- Similar AP: next investigate decode cost; useful band only: narrow the scope;
  large gap: inspect conditions/implementation before adding complexity
- Do not infer an isolated effect of INR, feature loss, or general superiority
  from this whole-method comparison

## Docker/SSH GPU handoff (target not chosen yet)

Select the server/GPU first and confirm VRAM, driver, CUDA compatibility, COCO and
checkpoint paths. Existing authentication stays on the user's Mac; do not copy
SSH keys into this cloud workspace. The preparation here authorizes no remote
execution. No private server addresses or account names belong in this repo.

Use explicit context on **every** Docker command; never change the global context:

```sh
# Run on the user's Mac after the target is selected and access is authorized.
docker --context <chosen-context> info
docker --context <chosen-context> image inspect <validated-gpu-image>
docker --context <chosen-context> run --rm --gpus 'device=<chosen-gpu>' \
  --name <unique-user-issue20-smoke-name> \
  --mount type=bind,src=<server-coco-directory>,dst=/data/coco,readonly \
  --mount type=bind,src=<server-checkpoint-directory>,dst=/data/weights,readonly \
  --mount type=bind,src=<server-user-owned-output-directory>,dst=/outputs \
  <validated-gpu-image> <smoke-command>
```

Bind source paths are on the **server**, not the Mac. Use separate compatible
C3 and TransTIC/Detectron2 images/environments and user-unique output/container
names. Do not use privileged mode, change daemon settings, prune images, remove
other users' containers, or allocate another user's GPU. CUDA image pins and
commands remain templates until the actual server/GPU/software is known.
