# Issue50 public aggregate audit (Issue #87)

CPU-only reaggregation of the three approved public inputs. No new images, splits, calibration, training, codec inference or detector evaluation. This is an internal consistency audit, not independent scientific validation.

## Results

PASS: 24 compressed points, 25 CSV rows including JPEG, six method/precision groups of four points. No missing keys, duplicate keys or numeric mismatches found. Primary comparison: 16 points; external uint8 supplement: 8 points. The all-uint8 view reuses the eight C3/C3′ points; it adds no measurements.

| Group | Points | bpp endpoints | AP range (%) |
|---|---:|---|---|
| adapt_icmh/float32 | 4 | [0.09387180804749973, 0.27705573918739584] | [42.657516264253175, 48.03575454294868] |
| adapt_icmh/uint8 | 4 | [0.09387180804749973, 0.27705573918739584] | [43.472440225018005, 47.354949297733626] |
| c3/uint8 | 4 | [0.1051765044229496, 0.3169445500418172] | [29.733242364695784, 41.971638025658436] |
| c3_prime/uint8 | 4 | [0.09457139983328176, 0.2897194231980741] | [36.164844702269846, 42.390498997230246] |
| transtic/float32 | 4 | [0.11394990474969535, 0.33941894029419173] | [41.47825055150135, 46.12199844211216] |
| transtic/uint8 | 4 | [0.11394990474969535, 0.33941894029419173] | [41.23298067925865, 46.540253311135416] |

All six primary pairwise endpoint intersections:

- c3 / c3_prime: [0.1051765044229496, 0.2897194231980741]
- c3 / transtic: [0.11394990474969535, 0.3169445500418172]
- c3 / adapt_icmh: [0.1051765044229496, 0.27705573918739584]
- c3_prime / transtic: [0.11394990474969535, 0.2897194231980741]
- c3_prime / adapt_icmh: [0.09457139983328176, 0.27705573918739584]
- transtic / adapt_icmh: [0.11394990474969535, 0.27705573918739584]

Four-way endpoint intersection: [0.11394990474969535, 0.27705573918739584] bpp. These are endpoint ranges, not continuously measured rates or matched-rate points.

| Method | Quality | uint8 minus float32 AP (pp) |
|---|---:|---:|
| adapt_icmh | 1 | 0.8149239607648298 |
| adapt_icmh | 2 | -0.991613789806344 |
| adapt_icmh | 3 | -0.39411752574248027 |
| adapt_icmh | 4 | 0.9403641517606118 |
| transtic | 1 | -0.24526987224269448 |
| transtic | 2 | 0.4182548690232579 |
| transtic | 3 | -0.5061178874804781 |
| transtic | 4 | 0.7756727729157618 |

Precision differences have both signs. External pairs use the same reconstruction and estimated bits; uint8 uses ties-to-even rounding after clamping. C3 float reconstructions are unavailable; no C3 float AP is inferred.

JPEG AP remains 47.432959401770944%; bpp remains null (CSV blank), never zero. JPEG is the distributed lossy JPEG baseline, not a lossless original. Its CSV-only delete-one range is [44.11318318086129, 51.874101429124806]; JSON has no matching range, so no cross-check is claimed.

## Definitions and limits

bpp is the arithmetic mean of each image’s estimated bits divided by D2-resized, pre-alignment nonpadding pixels. External codecs include y+z likelihood bits; C3 includes latent plus per-image network bits. Shared pretrained external weights are not charged per image. These are estimates, not actual bitstreams or container sizes. Do not combine them with old Issue20 original-pixel/aggregate definitions.

AP is official COCO bbox .50:.95 dataset AP (%) on the fixed 20 images, not a mean of per-image AP. Aggregate inputs cannot reconstruct bits, evaluate AP again, or recompute delete-one/jackknife values. Shared JSON/CSV delete-one extrema were checked for consistency only. Paired difference ranges are retained as source metadata; subtracting marginal extrema cannot recover paired differences. Delete-one sensitivity is descriptive, not a confidence interval or significance test.

Small sample size and 26–30 supported categories limit interpretation. Four discrete rates per group do not establish same-rate superiority. No AP interpolation, extrapolation, BD-rate, generalization or independent confirmation claim is made. TransTIC uses official 256-multiple bilinear resize before/after codec (align_corners=False); Adapt-ICMH pads right/bottom then crops; C3 uses native detector-sized inputs. Alignment and primary external float32 versus C3 uint8 precision remain confounded.

The historical report records fixed evaluation images and nonoverlap with a calibration set of 20 images. No new split or image selection was performed here; aggregate data cannot independently verify nonoverlap, input hashes, model behavior or pretraining overlap. The historical 188.23444253765047 GPU seconds are not a cost of this run. This run used no GPU and installed no dependencies.

## Reproduction and provenance

Run from the repository root with Python 3 and only the standard library:

```sh
python -B docs/research/generated/issue50-goal-review/test_audit.py
python -B docs/research/generated/issue50-goal-review/audit.py
```

The CLI accepts --root and --output (output must stay inside the approved scope). JSON is sorted, timestamp-free, rejects NaN and is deterministic for identical inputs and test evidence. Source SHA below is the approved input snapshot, not the historical codec runtime SHA. The markdown input supplies historical context; rounded prose tables are not treated as full-precision independent measurements.

Approved input source SHA: `a3c285fb191f95bdf3ecea54408c53adb177b5c1`.

| Input | SHA256 |
|---|---|
| docs/research/assets/issue50_four_method.json | `90bc064100ddda3c39a5eff8b03912bfc47435ae3e183c6bcafcbd4caabacc8b` |
| docs/research/assets/issue50_four_method.csv | `96cbd38c693642149d8a7acec694e533e5e800015943db31129d33d3bf3078bf` |
| docs/research/2026-10-16_four_method_comparison.md | `5e8bfb54753743d3fcc634377377c80dd247541db64c6dba16cdadbceac916f0` |

## Validation, failures and negative results

unittest: 22 tests; 0 failures; 0 errors; 0 skipped. See test-results.json for exact names and outcomes. Negative fixtures deliberately exercise missing points/fields, duplicate keys, mismatched metrics, invalid rates/precision keys, nonfinite numbers, altered overlaps and invalid JPEG data. Expected rejections are successful tests, not research failures.

git and rg were unavailable in the execution environment; initial read-only discovery commands returned command-not-found. Python filesystem inspection replaced them. No dependencies were installed. No scientific evaluation failed because none was attempted. No publication retry or extra research run is part of this workflow.
