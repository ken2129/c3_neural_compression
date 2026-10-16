# Issue 49 delivery and completion audit

The approved scientific study is complete: 160 fixed 20 calibration encodes,
eight frozen conditions, 160 encodes on a nonoverlapping 20-image subset,
four fresh reproducibility encodes, both JPEG baselines, and predeclared
sensitivity analysis. No heldout AP or rate was used to change lambda.
This audit distinguishes scientific completion, delivery verification, and the
user's review/merge decision. It does not close the issue.

[Final results and interpretation](2026-10-16_1041_JST.md).
[Item-by-item requirement audit](assets/issue49_four_rate/requirement_audit.json)
contains every scientific checklist item and all five completion conditions.

| Requirement group | Evidence and outcome |
|---|---|
| 1: fixed protocol and reproducibility, all five items | Pinned PR22/42 implementation; both predeclared deterministic settings; original seed/RNG; full fresh repeats exact for parameters, PNG, bits and common predictions; measured pilot cost/storage. PR 48 remains open and unmerged; its unresolved causal questions are not claimed solved. |
| 2: calibration/freeze, all eight items | Fresh fixed 20 means for eight initial conditions, all target and pair rate criteria pass; rate-only historical proposals; no correction jobs; frozen conditions before heldout generation and baseline. Historical points were not mixed into final curves. |
| 3: subset and storage, all five items | Fixed SHA256 selection seed and N=20 after 624 inspected prior-use exclusions, nonoverlap and input hashes checked; no AP/category selection; scoped unused claim; approved destination; shared JPEG baseline. Pilot-based forecast constrained N before measurement. |
| 4: eight-condition evaluation, all eight items | All 160 heldout encodes complete; shared evaluator and six pooled COCO metrics; image-mean estimated total-bit rates; pinned resize/objective/teacher/steps; final provenance replay passed. Actual cost 16.2897 cumulative GPU hours versus 9.5598 elapsed hours, 324 total encodes, 42 completed jobs, no failures. |
| 5: interpretation/next step, all six items | Complete separate plots/tables, target and pair flags, paired delete-one pooled AP ranges and category support; mixed AP differences; no universal equal-rate superiority, BD-rate, interpolation, significance or full 5000-image claim. No external-codec or new causal experiments added. |

The method-rate tolerance is met at all calibration pairs. On heldout, individual
target tolerances pass, but the 0.10/0.15 method-pair tolerances fail. At 0.225,
the AP difference is negative. These outcomes satisfy the requirement to retain
unfavorable and unmatched points; they do not satisfy a strict equal-rate
improvement claim. Calibration C3 prime remains nonmonotonic.

## Validation and private evidence

- Four preflight, six rate-selection and five public-report tests passed.
- The final CPU audit checked 42 terminal receipts, the frozen scope/config/input
  identities, final parameters/PNG/predictions, and recomputed image-mean rates.
- All 18 full-set and 360 paired delete-one official metric dictionaries replayed
  exactly from saved predictions. The ranges are descriptive, not confidence
  intervals. Minimal repeat checks use one input per method and one seed.
- Public final CSV/JSON reproduce exactly through the path-parameterized final
  renderer; both PNG/PDF plots were inspected. Private original controllers,
  plans, job files, input/asset identities, source and audit records remain
  separate from the public numerical whitelist.
- All four older collections were SHA1-verified before authorized intermediate
  checkpoint cleanup. 1,848 deletions freed 18.586 GiB; 7,816 retained old files
  were rehashed. First/latest states and required small dependencies remain.

## Delivery and outstanding conditions

All scientific artifacts are retained in 20 codec/evaluation batches plus one
metadata/source/input/baseline supplement, **3,402,874,880 bytes
(3.1692 GiB)**, below the approved 8 GiB cap.
All 21 remote archive SHA1 values and sizes match the local archive receipts;
a separate Box connector readback also confirms the intended destination and
active files. All 2,966 job files are covered by immutable member SHA256 lists
and were rehashed after upload. Final public files and publication receipts are
retained in a small delivery supplement, with its final readback kept privately.
No source result files are deleted during delivery and sharing is unchanged.

A Slack start-notification action was rejected by automatic approval review:
it could not verify authorization for the saved destination and payload.
The action was not retried. Periodic/completion notification delivery is not
claimed; the rejection is an operational exception, not a scientific failure.

The first three Issue 49 completion conditions are met: traceable separation and
selection, all eight conditions with common provenance-checked evaluation, and
an interpretable account of rate/AP/baseline/uncertainty/cost/limits. This record, numerical assets and the results PR deliver the fourth condition;
PR head/tree readback is retained as private delivery evidence. The fifth requires
the user's review/merge and scope decision and remains pending. No PR is merged
and no issue is closed by this work. Refs #49.
