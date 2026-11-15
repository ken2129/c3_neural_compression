# Historical Issue95 preparation (2026-10-24, before live E2E)

This is the pre-execution proposal and its earlier test results, not the current
E2E status. The later real A/B runs and redelivery succeeded; see
[PR100](https://github.com/ken2129/c3_neural_compression/pull/100).
The follow-up implementation, source mapping and new verification are tracked in
[Issue101 reproduction](ISSUE101_REPRODUCTION.md). Earlier approvals below are
historical and must not be reused to dispatch another job.

## Fixed sources and reuse

- Input/main observed at a3c285fb191f95bdf3ecea54408c53adb177b5c1.
- PR93 client/contract: 9f99f59a5b08b4c7139582b774d17e8c3e2fc753.
- PR94 parent: f9e6b9a6beb95cbe34396b078a8cd5ba92ab6083.
- Earlier successful model-free Docker runtime:
  abee7bf5debde263cc83b041f62addb496948d79.
  Reuse its cancellation, ECHILD, cleanup, sequential-job and duplicate evidence
  in [DIAGNOSTIC_PROPOSAL.md](DIAGNOSTIC_PROPOSAL.md); its GitHub/Codex peers were
  fixtures. The old CPU E2E in Issue89/PR90 remains a different, disposable path.
- PR93 and PR94 are unmerged. A main checkout alone has no resident deployment.

## Defect and minimal runtime change

Previously manager() started ReceiptServer before querying uncertain CLAIMED
runs and before the inbox block/recovery transaction. A delivery arriving at
that query could receive a durable enqueued ACK and then remain behind a latch.

The manager now completes the prior-claim gate or delivery-lease recovery before
constructing/publishing the receipt listener. A prior claim is preserved and
new deliveries are rejected after the durable latch. Claim-query, gate-persist
and recovery failures publish no listener. Manager state is closed even if
startup aborts before a listener exists. The existing approval, claim, worker,
cleanup and production resource checks are retained.

## Regression evidence, 2026-10-24

Tests ran in the existing Linux bash work container with Python 3.10.22,
without credentials, a model, runner registration, GitHub delivery or Docker
lifecycle operations.

Five new tests call the actual manager() entrypoint with real SQLite/inbox/Unix
IPC and the formal approval/claim fixture. UID, boot/profile and state-factory
boundaries are fixtures; these tests do not prove deployed UID isolation.

1. During an uncertain-claim query there is no listener; after the latch, a new
   receipt is rejected, no event is added, and the claim witness is unchanged.
2. ACTIVE delivery recovery completes before IPC, FINISHED deliveries remain
   finished, and a later successful ACK corresponds to a committed PENDING row.
3. An existing inbox latch refuses recovery without publishing a listener.
4. A real SQLite authorization failure on the claim query closes state without
   publishing a listener.
5. A real SQLite trigger refusing gate persistence produces no success ACK.

The five tests fail against the untouched PR94 parent, including an observed
enqueued ACK during the query, and pass after the fix in normal and -O modes.

Full harness commands:

    python3 -B -m unittest discover -s tests -p 'test_harness_*.py'
    python3 -B -O -m unittest discover -s tests -p 'test_harness_*.py'

Both discover 1092 tests: 1091 pass, 1 explicitly skipped, no failures/errors.
Elapsed subprocess times were 56.934 and 57.295 seconds.
The skipped procfs/broker-anchor test requires a root-owned PID1; this work
container has a user-owned bash/init PID1. The untouched PR94 parent reproduces
that environment failure. Its test now declares that requirement explicitly.
Production root/namespace checks are not relaxed. The earlier real root-broker
Docker evidence is separate from this environment skip.

## Deployment prerequisite found by read-only inspection

The inspected Docker engine uses cgroup v1. Both the existing goal resources()
adapter and the resident subclass inherited by PR94 require a namespaced v2
root (0::/), cgroup2 mount and v2 memory/swap/PID/CPU files.

A fresh container alone cannot satisfy the original v2-only condition.
A resident-only v1 adapter has now been added, without changing host/Docker
settings or the v2/disposable adapters. Selection uses the actual kernel cgroup
view; a failed v2 check never falls back to v1.

The v1 adapter requires every reported hierarchy at namespace root, matching
rooted read-only controller mounts, protected quota files, finite memory/PIDs,
hierarchical memory accounting, memory+swap equal to memory, and exactly one
CPU by CFS quota/period. Missing memsw accounting, unbounded/unknown values,
host-visible paths, aliases, extra/nested mounts and writable controllers reject.
The Docker v1 parent is a root-owned 0755 tmpfs directory of mountpoints; it may
be rw but contains no quota interface. Each actual controller must be separately
ro/nosuid/nodev/noexec. Read-only root, bounded work/shm, ownership, no nested work
mounts, and no Docker socket/GPU checks remain required.

Seventeen new v1 tests pass in normal/-O modes. The actual existing host-visible
v1 layout is rejected using a real procfs read. Other v1 acceptance/limit cases
are fixtures, not kernel enforcement proof. With this addition, both full
harness runs discover 1109 tests: 1108 pass, one root-PID1 environment skip,
no failures/errors; elapsed subprocess times 57.395 and 56.251 seconds.

The next bounded native check should use one separately approved fresh private
container and the measured resource interfaces, without model/runner/auth.
It verifies this new adapter only, reusing the earlier lifecycle diagnostic
rather than repeating it. Live v1 admission and real E2E remain unverified.
Broad OS compatibility and generic installer work are not included.

The existing work container also has no installed resident entrypoint, runner,
receipt CLI or manager profile. It has a ChatGPT-mode Codex auth file and no gh
binary. File presence is not authorization to transfer credentials or to use
a model; paid-credit/billing state has not been verified.

## Bounded live-E2E proposal, not execution approval

Use the same installed input/write envelope for two independently approved
jobs. Input source and actual main publication base must be pinned after the
reviewed default-branch workflow is introduced; this patch does not introduce it.

- Inputs: LICENSE and README.md only; standard-library CPU work.
- Shared write scope: docs/research/generated/issue95-resident-e2e.
- A: hash LICENSE and create job-a.json plus job-a.md.
- B, only after A finishes and its outside report is confirmed: hash README.md
  and create job-b.json plus job-b.md. Use a separate Issue/claim/PR.
- Each JSON records job, fixed source SHA, input path, byte count and SHA256.
  Reports explain the command/results and separately list unconfirmed external
  ledger/notification facts. Neither input may be changed.
- Oracles at observed input a3c285fb:
  LICENSE, 11358 bytes,
  cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30;
  README.md, 14941 bytes,
  95aa24566d5f182ae0dfbac61fec60b3db0451f0e0eac39c6862ee017295eb6e.
  Re-verify these against the final approved source before creating requests.
- Proposed model: the previous CPU E2E's gpt-6.1-sol. Two fresh jobs/turns total,
  one normal PR per job, no new model turn after failure, no automatic retry.
- Per job: 600 seconds, 120 CPU seconds per process, aggregate 1 CPU/1 GiB/64 PID,
  work tmpfs 256 MiB, captured output 1 MiB; cleanup watchdogs remain explicit.
  These are not token or monetary caps.
- Re-run the same actual Actions run once to test unchanged receipt identity
  and no new worker/PR/notification. A second new event for A separately checks
  claim deduplication if included in the final approved test scope.
- Require stable container/PID1/manager/runner identities and RestartCount=0,
  completed saved results, zero worker descendants/ECHILD and clean workspace
  before the next job. The local observer does not execute the model's task.
- GitHub Actions receipt success, worker completion, PR publication and dot's
  report into the originating chat are separate assertions. Dot reporting
  remains confirmation pending unless externally observed.

Setup approval must identify the fixed source/image/runner release, dedicated
container and persistence, credential sources and access scope, workflow/default
branch operation, intake enablement, failure stop/recovery and the billing
condition. No existing service needs to be stopped for this preparation.
No image build, new container, runner registration, credential transfer,
workflow enablement, Git push/PR, real model job, GPU/data download, Slack or
merge was performed by this patch.

Official billing references: [authentication](https://learn.chatgpt.com/docs/auth)
and [pricing](https://learn.chatgpt.com/docs/pricing). ChatGPT authentication and
API-key billing are different; current paid-credit/plan state is not established
by auth.json alone.

## Resource interpretation references

- [cgroup namespace semantics](https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html)
- [v1 memory controller files](https://www.kernel.org/doc/Documentation/cgroup-v1/memory.txt)
- [CFS quota/period](https://docs.kernel.org/scheduler/sched-bwc.html)
- [Docker memory/swap contract](https://docs.docker.com/engine/containers/resource_constraints/)

The v1 memory and memsw values are measured separately; swap=0 is returned only
when both finite limits are equal, matching Docker's no-swap configuration.
No absent or unreadable limit is substituted with a profile value.
