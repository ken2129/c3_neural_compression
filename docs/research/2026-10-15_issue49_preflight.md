# Issue49: four-rate calibration and independent-subset preparation

The new comparison is still in preparation. No new compression measurements or
performance improvement are claimed in this record.

Main and the documentation checkout match the current merged Issue41 results.
PR #48 remains open; its finite-optimization, representation and detector-output
controls motivate a fresh heldout comparison, without requiring another cause
investigation or a merge. No merge or issue closure has been performed.

The implementation is pinned to `0c3a60c703bd41956b6558d527470350bcc7dd5d`
(PRs #22/#42, unmerged). A separate clean checkout preserves the old code and
results. Both deterministic backend settings are proposed because the prior
early-trajectory reproducibility control matched; complete fresh500+max50-step
repeats still need verification. This setting is not selected by AP. Prior
seven-point fixed20 rates propose candidate lambdas only and will not be mixed
with fresh deterministic points without compatible provenance.

Proposed targets are0.10,0.15,0.225 and0.30 estimated bpp per method. Calibration
uses only fixed20 mean rate, with at most two candidates per target per method.
Target tolerance and between-method tolerance are both10%, with normal C3 as
the latter denominator. All misses and failures remain in the record. The
heldout lambdas will not change in response to heldout rate or AP.

Inspection found an older100-image study and500-image heldout study in addition
to the recent calibration/smoke history.624 unique prior-use images were excluded
from the5,000-image COCO val2017 population. A fixed SHA256 ordering selected20
of the remaining4,376 images without reading their AP or selecting categories.
Their JPEG bytes and dimensions were checked. The defensible unused claim is
relative to inspected records; unavailable/unrecorded history is not certified.
The subset manifest and image identities remain private.

Historical full20 jobs cost approximately156 seconds/image for normal C3 and
215 seconds/image for C3′. The20-image evaluation requires160 encodes, estimated
at8.23 GPU hours before a50% allowance, or12.35 GPU hours with allowance.
These are historical estimates, not fresh pilot measurements. A bounded pilot
must verify current costs before adopting the calibration execution forecast.
GPU-hour costs are reported separately from wall time; a currency price has
not been established.

The proposed storage wrapper retains trained/quantized params, PNG, metrics and
provenance while keeping only one rolling checkpoint per active image. Old
20-image jobs saved about2.65 GiB of checkpoints per condition. CPU checks show
that the new configuration changes only output/checkpoint locations and
preserves the scientific values and tuple types. Four preflight tests passed,
and all20 selected JPEG hashes/dimensions and the nonoverlap check passed.
Full GPU reproducibility and storage behavior are not yet verified.

Proposed uncertainty analysis uses paired delete-one official pooled COCO AP
recomputation, including changes in category support. Ranges describe
sensitivity; they are not confidence intervals. All measured points and both
baselines will be plotted with calibration and heldout data separated. No AP
interpolation, BD-rate, statistical superiority or full5,000-image comparison is
planned for this pilot.

Execution/storage approval is pending. Scientific execution and publication
status remain separate. The result PR, numerical tables and graphs are still
outstanding; this preparation record does not complete Issue49.

CPU follow-up: the frozen rate-only rule now has executable interpolation,
extrapolation, a factor-of-two update cap, stable attempt-order tie handling,
and separate target/pair tolerance checks. Six additional tests passed,
including AP-invariance and rejection of single-image calibration proxies.
The new observer verified completed attempts, input and native reconstruction
provenance on two prior fixed20 conditions and reproduced their published mean
rates: normal C3 0.1770819703 bpp and C3′ 0.1446657308 bpp. These remain historical
reference points, not newly adopted deterministic calibration measurements.
The requested execution/storage scope is unchanged and still awaits approval.

Further CPU preparation: five report tests passed, covering separate rate
criteria, retention of failed/undefined points, full-set pooled-COCO certification,
subset nonoverlap, and public-field privacy. A temporary synthetic fixture also
verified PNG/PDF/CSV/JSON generation; its artifacts were deleted and are not
research results. No new calibration or heldout measurements have been made.
