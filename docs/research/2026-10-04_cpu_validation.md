# 比較基盤のCPU検証記録（2026-10-04）

この文書は当時の検証記録です。現在の起動手順は[比較手順](../compression_comparison.md)、現在の運用は[研究ポリシー](../experiment_policy.md)を参照してください。以降の環境・未実施事項は2026-10-04時点の記録です。

These checks ran on dot's CPU cloud workspace. No user server, Docker daemon,
GPU, private SSH key, or full COCO image download was used.

## Verified execution

- Python 3.12; PyTorch2.5.1+cpu, torchvision0.20.1+cpu, NumPy2.3.5,
  Pillow12.3.0, pycocotools2.0.11, pytest8.3.5
- TransTIC pinned source1132f62cbddc0122aa19f620453c3b0c4de49b2c;
  timm0.6.13, pybind11 2.13.6, pytorch-msssim1.0.0; native extension built
  with GCC/G++. Only generated compressai/version.py differs from the source pin
- Detectron2 source1e3e13bbf607b54f62205c4c33922521822fb298; native CPU
  extension built with GCC/G++, MAX_JOBS=2. setuptools80.9.0 is required
  by this source's pkg_resources model-zoo import. An initial source fetch403
  was recovered by an approved retry of the same official clone

### Official assets

| Asset | Bytes | SHA256 | Check |
|---|---:|---|---|
| [TransTIC det1](https://github.com/NYCU-MAPL/TransTIC/releases/download/v1.0/det_1.pth.tar) | 52018885 | 022e8c330234d2ec5502722a0409da78b79722e95159734f1fc0b010a24543d5 | weights_only read, 675 state keys, strict model load, actual CPU forward |
| [Common D2 candidate](https://dl.fbaipublicfiles.com/detectron2/COCO-Detection/faster_rcnn_R_50_FPN_3x/137849458/model_final_280758.pkl) | 167266879 | 3c25caca37baabbff3e22cc9eb0923db165a0c18b867871a3bf3570bac9b7ef0 | strict compatibility after D2 legacy conversion, actual CPU inference |

Hashes identify downloaded bytes; no publisher-provided TransTIC digest was
available. Detector identity with the original TransTIC paper is still unverified.
Do not interpret successful loading as proof of paper-checkpoint identity.

### Actual CPU smoke scope

1. Official det1: frozen9,130,691-parameter model loaded safely and strictly.
   Constant float RGB256×256 input produced finite x_hat and y/z likelihoods
2. Faithful full mapper+codec: a **synthetic**, RGB100×1000 gradient PNG mapped
   through actual Detectron2 to133×1333, bilinear-resized to256×1536 for codec,
   then resized back and clamped to float32[3,133,1333]. All values finite
   An independent parity check using the actual upstream DatasetMapper +
   Alignment + official model produced a bitwise-identical reconstruction
   (maximum pixel difference0.0) and identical y/z bits to the adapter
3. Common detector: actual official checkpoint loaded, synthetic120×160 RGB
   image mapped to800×1067 and yielded two original-coordinate predictions
4. Full `comparison_runner` CLI: actual det1 + shared detector + official
   pycocotools ran on a clearly labeled one-image synthetic/no-ground-truth
   fixture and wrote complete status, per-image sizes/bits, predictions,
   rate summary, config/manifest and model/environment provenance

The synthetic CLI generated no detections. Official COCOeval correctly reports
AP=-1 (undefined) because the synthetic annotation has no ground truth. This is
**not a COCO AP measurement or a compression quality result**. Its purpose is
to validate execution, dimensions, rate plumbing and empty-result handling.
For that synthetic fixture, total7800.327392578125 estimated bits over100000
original pixels yields0.07800327392578126 common bpp; the native detector-size
auxiliary denominator yields0.043997808056778054. These numbers demonstrate
why denominator correction matters; they are not a research rate point.

## Tests

Selected evaluator/config/adapter/report suite: **115 passed**, consisting of
28 unchanged evaluation tests plus87 new tests. Includes real CPU tensor
preprocessing, strict/negative cases, mock orchestration, real pycocotools with
synthetic boxes, and expected empty/no-GT behavior. The exact command is in
`compression_comparison.md`. Python compile checks and `git diff --check` passed.

The full repository suite was attempted. It did **not** run to completion:
collection stopped in test_machine_checkpoint.py, test_machine_loss_config.py,
and test_torch_bridge.py because JAX/chex are not installed in this isolated
codec/evaluator environment. This selected-suite pass is not a full C3 pass.
The PR14 seed change is inspected/copied; C3 training/gradient/resume paths have
not been revalidated here. Use the separate C3 GPU environment for those gates.

## Still unverified

- C3 image/combined optimization on actual COCO images; gradient and resume
  checks in a compatible C3/JAX environment
- det2–4 asset acquisition/load/forward (adapter quality selection is unit tested)
- CUDA compilation/execution, GPU peak memory, runtime budget and convergence
- Real shared COCO subset AP, rate overlap, three-condition comparison, research
  interpretation, and any actual bitstream measurement
- Past seminar experiment/config/checkpoints; this remains a new fixed baseline

Deprecation warnings from the upstream PyTorch autocast/meshgrid and D2
pkg_resources imports remain; warnings did not invalidate the actual CPU checks.
No remote code push or PR was made by this preparation task.

## Independent-review provenance corrections

The first preparation commit was reviewed before handoff. Two blocking gaps
were found and corrected: original C3 inputs had only ID/name/dimension matching,
and a reused C3 output root could mix partial attempts. The COCO loader now hashes
the exact bytes it decodes; discovery requires that hash to match the manifest.
Comparison-only attempts now claim an empty root exclusively, record status/UUID,
and bind metrics/reconstruction/config files by hash. Only verified completed
attempts can be discovered. General non-comparison resume remains unchanged.
Start-time config/seed snapshots survive failures before the first output.
The actual step wrapper and artifact writer were exercised by source extraction
with CPU NumPy/Pillow and mocked JAX boundaries, not full JAX optimization.
New regressions cover same-ID/same-size changed source bytes, missing source
hashes, source replacement after read, stale metrics, failed/interrupted attempts,
and rerun isolation. C3 training itself remains a separate GPU/JAX validation gate.
