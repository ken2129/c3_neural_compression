# Issue49 execution helpers

These helpers use the unchanged C3 implementation at commit
`0c3a60c703bd41956b6558d527470350bcc7dd5d`. That implementation is in
unmerged PRs #22/#42; main's result documents alone are insufficient to run it.
Use a separate clean checkout of the pinned commit as the Python package.
Do not edit a previous experiment checkout or reuse its output directories.

`prepare_heldout.py` selects images using explicit prior-use manifests and a
fixed SHA256 ordering. Private input manifests must include all inspected
experiment histories. Annotation IDs and dimensions alone are not use history.
`fetch_selected_images.py` obtains only the selected COCO-distributed JPEGs,
checks their dimensions, and records their hashes. No category or AP filtering
is performed. Neither helper proves that unavailable histories contain no use.

`supervise.py PRIVATE_JOB_JSON` validates a scope approval bound to the exact
private plan, a derived job registry, clean source, selected GPU's real process
state, disk reserve, and cumulative GPU time before invoking `execute_job.py`.
Run supervisors inside tmux. Approval records are receipts of actual user/review
authorization; creating these records does not itself grant permission. Derived
jobs must follow the frozen rate-only selection rule within the approved caps.
Failed/stopped jobs retain their outputs and cannot automatically retry.

`storage_runner.py` uses the original jaxline entrypoint, seed/RNG handling,
500 noise steps and maximum50 STE steps. `storage_config.py` changes only output
and checkpoint directories; tuple-typed scientific settings are preserved.
Every50 noise steps, it verifies the new resumable checkpoint and records its
hash/RNG before removing only its own superseded checkpoint. Final trained and
quantized parameters are retained separately from the strict comparison root.
Only after checking final reconstruction and metadata against retained params
is the last rolling checkpoint removed. Failed jobs keep their latest state.
Recovery is a separately recorded action: the original fresh-attempt guard does
not silently resume a failed comparison or select among retries.

Evaluation uses the pinned common evaluator, detector-size input protocol and
native PNG path. The baseline uses the same evaluator on COCO-distributed JPEGs.
`uncertainty.py` recomputes official pooled COCO AP for identical delete-one
subsets across all conditions and baseline. It reports descriptive sensitivity
ranges and category support, not confidence intervals or statistical superiority.
AP is never interpolated and BD-rate is not calculated.

Large artifacts stay private. Transfer only approved Issue49 artifact batches to
the explicitly approved Box destination. Verify a complete remote copy and its
hashes before pruning Issue49-owned local copies; preserve every old artifact.
The supervisor stops on storage/time limits rather than deleting old results.

CPU checks: `python experiments/issue49/test_preflight.py`. Full numerical,
checkpoint-storage, source/prediction and reproducibility checks remain required
on the approved GPU pilot before any calibration result is adopted.

`rate_selection.py` implements the frozen rate-only interpolation/extrapolation,
factor-of-two correction cap and separate10% tolerance checks. Its observer
validates complete fixed20 source attempts; historical observations remain
reference-only until numerical/provenance compatibility is established.

`report.py` renders a verified private bundle through a public field whitelist.
It requires all eight condition rows per phase, retaining explicit failures and
undefined metrics, and refuses overlapping calibration/heldout sets. It emits
separate calibration/heldout panels as PNG/PDF plus CSV/JSON. This renderer does
not replace the source-provenance and frozen-condition completion audit.
CPU checks: `python experiments/issue49/test_rate_selection.py` and
`python experiments/issue49/test_report.py`. Renderer integration fixtures are
synthetic tests only and must never be published as research measurements.

`final_report.py PRIVATE_RESULT_DIRECTORY OUTPUT_DIRECTORY` creates the final
comparison, frozen conditions, paired sensitivity tables, and PNG/PDF figures
from an already audited private bundle. It is the path-parameterized version
of the presentation script used for the final record. It does not perform a
provenance audit or launch GPU work. Original executed controllers, immutable
plans/configs, source snapshots, and complete per-job evidence remain in the
verified private retention archive.

`provenance_audit.py PRIVATE_RESULT_DIRECTORY` is the path-parameterized
version of the final CPU audit. Run with the pinned runtime on PYTHONPATH and
CUDA hidden; it requires the original private path layout referenced by the
frozen plan and all assets. It verifies source/asset hashes, receipts and caps,
freeze timing, config/input/parameter/PNG/prediction correspondence, mean rates,
18 full-set official metrics and all 360 paired delete-one metrics. It writes
private evidence containing identities and paths; do not publish those outputs.
