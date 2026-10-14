# Issue41: CPU protocol preparation and staged GPU handoff

This preparation adds `detector_input_v1` on top of the fixed comparison implementation from PR #22. That implementation is not merged into main. The new PR targets `issue20/cpu-comparison-preparation`; neither PR is silently merged. Main's existing `docs/research/` diary remains the research history. This document describes the executable protocol and CPU verification, not new GPU results.

## Fixed protocol

- Decode exact source bytes and retain original image ID, filename, SHA256, H×W and original COCO annotations.
- Before C3 optimization, use Detectron2 `ResizeShortestEdge([800,800],1333)` on RGB uint8. Preserve official pixel values, then convert HWC to float32 using division by255. No codec padding is introduced for C3.
- Preserve C3's rounded/clipped uint8 PNG output, now at detector-resized dimensions. Decode that PNG directly for evaluation; do not resize again. Shared detector retains original H×W to map boxes back to original coordinates.
- Validate mapped uint8 SHA256, mapped dimensions, protocol-tagged experiment signature and the completed-attempt ledger before loading reconstructions. Original-input artifacts cannot be passed as detector-input runs, or vice versa.
- Primary rate: mean over images of total estimated bits divided by resized nonpadding H×W. Include latent, quantized synthesis and entropy network bits. Existing architecture/config and other shared conventions remain assumed known; per-image headers/container overhead and actual bitstream are not measured. This preparation does not claim an actual transmission-size accounting beyond the existing bit components.
- Retain original-pixel pooled rate under `original_pixel_pooled_bpp_auxiliary`. Changing the reporting axis does not convert old C3 results into new-input results.
- Keep image MSE weight1, feature L1 weight0.1 for combined, FPN0–3 each0.25, seed20261004 and existing image-ID-folded optimization RNG. Both objectives use500 noise + maximum50 STE, unchanged optimizer/schedule/model/quantization-candidate selection. No AP-driven seed/checkpoint selection.
- Original `original_rgb` inputs and summaries remain the default. Legacy rate summary content is unchanged. New-protocol collection refuses mixed primary-rate definitions and unequal full detector metadata; use `detector_input_protocol.collect_runs` for C3-only ablations.

## CPU verification completed

[Validation JSON](issue41/cpu_protocol_validation.json) records source/annotation hash checks and bitwise uint8 mapper parity with the official BGR loader on all20 actual images. Original pixel total5,659,540; resized pixel total17,724,800.

151 CPU tests pass with CUDA hidden and JAX restricted to CPU. They cover real mapper parity, asymmetric fixtures, precise tensor values, legacy loader behavior, PNG precision/no second resize, original-coordinate forwarding, missing/failed rows, arithmetic mean versus pooled/padded denominators, wrong protocol and mapped-hash rejection, stale summaries, detector binary mismatch, and previous comparison/checkpoint/config behavior. Synthetic evaluator fixtures are plumbing checks, not AP measurements. No new codec optimization or learned detector inference ran.

The first float conversion fixture found up to one ULP difference from torchvision reciprocal scaling. The opt-in loader now uses the official division-by255 path; the legacy path remains unchanged.

## Existing results recovered and reaggregated without GPU

All12 runs (external8 + #36 C34) certify the same20 IDs, exact manifest and full shared-detector metadata, including compiled extension hash. Saved summaries were independently recomputed from all per-image rows before reexpression. [CSV](issue41/existing_rate_audit.csv) and [source-hash report](issue41/existing_rate_audit.json) retain provenance.

| Method | Existing original-pixel pooled bpp | Resized nonpadding per-image mean bpp |
|---|---|---|
| TransTIC det1–4 | .350944, .512404, .673133, 1.031233 | .113240, .165240, .217195, .333036 |
| Adapt-ICMH level1–4 | .301469, .413969, .614881, .867356 | .097186, .133120, .197669, .278770 |

External models already encode detector-resized inputs. Their codec alignments remain different: TransTIC resizes to multiples256; Adapt pads/crops. Existing #36 C3 runs encoded original images, so their reexpressed rates are reporting-only auxiliary data, not a new-input ablation and not new paper-protocol results.

Actual per-image/summary/codec/detector/config/environment/metrics JSON and the previously untracked Adapt wrapper are preserved in the local `var/issue41_cpu_preparation_20261014/results/existing_metadata/` bundle. No archive download or external inference was needed. The wrapper is not thereby promoted to a tracked implementation or independently proven correct in all respects.

## GPU smoke proposal — not authorized or started

CPU preparation alone does not authorize GPU work. Proposed next scope: COCO ID474039, combined only, lambda=.01 (starting candidate, not calibrated),500 noise + max50 STE, GPU0 RTX3090 24GiB in the already authorized workspace; tmux,10-minute Slack progress and terminal notification.

Proposed limits: one codec attempt,60 GPU minutes,2GiB new results. This is a conservative hard cap, not a measured runtime estimate. Stop on time/storage cap, nonfinite loss/bits/reconstruction, incompatible source/input/checkpoint, coordinate/dimension/protocol failure. Preserve failures and do not retry, extend steps, change seed or launch lambda calibration automatically. Also evaluate the one original image through the shared evaluator; one-image AP is not a performance criterion.

After the new source commit is published, present that SHA and exact runtime/config/budget for user approval. Smoke counts as one of the maximum six combined lambda candidates if reused under identical conditions. Stage③ (remaining up to11 candidates across both methods) and stage④ (20 images ×2 methods ×2rates =80 encoded observations) need separate measured-cost proposals and approval. Old #36's6h/10GiB budget is not inherited.

The target mean bpp .15/.30 and between-method tolerance10% are proposed as specified in Issue41. Lambda selection uses rate only, with interpolation in log lambda only when measured neighbors bracket a target. No AP interpolation/extrapolation. Each20-image run must report both target proximity and between-method rate difference.

## Commands and prerequisites

The CPU environment used here is the existing evaluation environment with Torch2.7.1+cu128 (CPU operations only), torchvision0.22.1+cu128, JAX/jaxlib0.4.38, NumPy1.25.2, Detectron2 source1e3e13bbf607b54f62205c4c33922521822fb298 and its recorded compiled extension. Python3.10. Code must be imported from the new checkout's parent, not the old frozen runtime package.

```sh
# From the parent of the new checkout, using the verified evaluation interpreter.
CUDA_VISIBLE_DEVICES='' JAX_PLATFORMS=cpu python -m unittest \
  c3_neural_compression.tests.test_detector_input_protocol \
  c3_neural_compression.tests.test_rate_protocol_audit

# Existing JSON inspection only. A new empty output directory is required.
python -m c3_neural_compression.evaluation.rate_protocol_audit \
  --run-dir /path/to/completed/evaluation --output-dir /path/to/new/audit
```

For the separately approved GPU smoke, set explicit `C3_COCO_ROOT`, `C3_COCO_ANNOTATIONS`, `C3_SUBSET_IDS` (one ID only), `C3_OUTPUT_ROOT` (fresh root), `TORCH_HOME` and GPU visibility. Proposed experiment config:

```sh
python -m c3_neural_compression.experiments.image \
  --config=c3_neural_compression/configs/coco2017_detector_input.py:combined_lambda0.01
```

Use `comparison_runner` with original source manifest, `condition: c3_combined`, `input_protocol: detector_input_v1`, explicit checkpoint SHA256 and `c3_output_roots`. Outputs retain original coordinate metadata. GPU runtime loading, VRAM peak, optimization convergence and final AP remain untested for this new protocol.

Paper detector byte identity and Adapt paper AP/AP50 discrepancy remain unresolved. Existing20 images are calibration/exploration images, not held-out. Paper curves, all5,000 image evaluation, external anchor inference, longer steps and independent final evaluation are outside this CPU preparation.
