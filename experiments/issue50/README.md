# Issue50: frozen held-out external comparison

This experiment adds the four public TransTIC detection checkpoints and four
public Adapt-ICMH levels to the fixed C3/C3′ held-out experiment in Issue49 / PR51.
No training, lambda selection, image selection, quality selection or retry sweep
belongs to this experiment.

The execution runtime is the fixed repository source commit
`0c3a60c703bd41956b6558d527470350bcc7dd5d`, which provides the evaluation package.
The base main branch does not by itself contain that runtime. Supply the original
private execution layout to `--root`; manifests, checkpoint locations/digests,
images, predictions, and environment metadata stay private. Published research
assets contain aggregates only. Do not publish the private root.

- `preflight.py`: verifies frozen manifest, disjoint calibration IDs, eight C3
  completion attempts and original rate fields, all mapped input hashes, eight
  external checkpoint digests, source commits, detector identity and JPEG baseline.
- `codec.py`: retains each float32 reconstruction and its ties-to-even rounded PNG;
  pilot is the first fixed image and is reused in the 80-image method sequence.
- `transtic_official.py`: uses official TransTIC model and official
  `Alignment(divisor=256, mode='resize')`. Independent bilinear alignment/resume
  checks use `align_corners=False`. This user-approved codec resampling is a
  method difference; the common detector performs no additional resizing.
- `adapt_icmh_adapter.py`: fixed adapter from the previous calibration experiment,
  with official zero pad/crop alignment and no post-codec resize.
- `supervise.py`: accounts subprocess startup, load, inference and exit, refuses
  active GPU jobs, and enforces cumulative GPU budget. The initial Adapt codec
  supervisor receipt was retained from the first execution before this generic
  supervisor was introduced; do not relaunch that completed job.
- `evaluate.py`: exact Issue49 detector/environment metadata required; evaluates
  both saved precisions on all manifest IDs, with original-coordinate boxes.
- `audit.py` / `audit_c3.py`: independent reconstruction/hash/round/rate and full
  pooled COCO AP checks. `uncertainty.py` recomputes paired delete-one pooled AP
  on identical 19-image sets and records category support changes.
- `report.py`: emits all measured points and JPEG baseline as PNG/PDF/CSV/JSON
  only after both external audits, complete uncertainty and four GPU receipts.

Use the common fixed evaluation Python for detection and CPU audits; Adapt uses
its existing isolated codec environment. The supervisor selects the matching
interpreter and exposes one physical GPU. Commands, from the execution root:

```bash
python experiments/issue50/preflight.py --root "$EXPERIMENT_ROOT"
python experiments/issue50/supervise.py --root "$EXPERIMENT_ROOT" --stage codec --method transtic
python experiments/issue50/supervise.py --root "$EXPERIMENT_ROOT" --stage evaluate --method transtic
python experiments/issue50/audit.py --root "$EXPERIMENT_ROOT" --method transtic
python experiments/issue50/report.py --private "$PRIVATE_RESULTS" --output docs/research/assets
```

These illustrate the sequence; archived run paths differ because the experiment
used its dedicated worktree. Existing output directories/receipts deliberately
prevent implicit reruns. All assertions must remain enabled. Limits are 160 total
codec attempts, 7,200 total GPU seconds, 4 GiB new artifacts, and 3 GiB free reserve.
Likelihood bits use the arithmetic mean of bits divided by the detector-sized
nonpadding pixel count **before** codec alignment. Shared pretrained weights are
not charged per image; C3 includes its per-image network bits. No actual bitstream,
AP interpolation, confidence interval, BD-rate or general superiority is claimed.
