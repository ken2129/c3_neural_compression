# Issue101: resident fixes and reproducible images

## Scope and source provenance

This implementation is stacked on PR94 HEAD
`f9e6b9a6beb95cbe34396b078a8cd5ba92ab6083`, not on main. PR93 and PR94 were
both open/unmerged when checked on 2026-11-07. Main remains
`6fd11016d64f88721eb08caff2bb8b0988f3d847`; its installed workflow is separate
from PR94's runtime. Do not duplicate or merge those PRs as part of Issue101.

The original limited real E2E passed: Issue96/97 produced PR98/99, cleanup and
real duplicate delivery were recorded in PR100 at
`4f2620ffd1f2669f357184457e3177c804774a34`. Those results are historical evidence,
not a test run of this new revision. Setup required manual runner permission
repair; RestartCount=0 during the experiment did not prove unattended setup.

Read-only inspection on 2026-11-07 found:

| Role | Full identifier |
| --- | --- |
| Running container | `5d84a79805f98a9c148277447adec0ecebaf5396d146972262df2e0b4dc81f5d` |
| Deployed runtime | `13d200cf2c019a1dcad84c1cbe74e850e4e82ca2` |
| Running image | `sha256:eda225e7cf757f093a84d8cc61ac48fceddc1613140c4e35896a7c655272a7d4` |
| Started (UTC) | `2026-10-24T09:34:03.003555664Z` |
| Restart count | `0` |
| Local source HEAD | `2fce23f28f986cee92edd9afda768fd01cc74ead` (clean working tree) |

Original local fixes, retained by `cherry-pick -x` in this PR:

| Original full SHA | Purpose | In old running image |
| --- | --- | --- |
| `a0c2f910e5f3b5943a4c7bdc58724b0972793ba6` | Startup claim gate/recovery before receipt listener | Yes |
| `cd558c31424981380c712c4feda32021599befe7` | Resident-only private cgroup-v1 admission | Yes |
| `6f25e2e7557c61fd5a2c913b6d1bc54494d4bd9d` | Restore runner mode0700 after archive extraction | No: later, not rebuilt/deployed |

`13d200cf...` only added native probe documentation after the first two fixes.
Local `2fce23f...` documented the E2E results. Neither is an extra runtime fix.
The startup launcher, resident runtime and v1 adapter hashes match the published
PR100 evidence. New commits additionally test existing registered runner state
and rejection without repair.

## Host prerequisites and refusal conditions

Linux with private PID/IPC/cgroup namespaces, pidfd and child subreaping is
required. The observed host uses kernel5.15 and Docker's cgroup-v1 layout.
UID10001(manager), UID10002(worker) and UID10003(runner) remain separate.
Use read-only root, default seccomp/AppArmor, no-new-privileges and only
SETUID/SETGID capabilities. Never add GPU, Docker socket, host namespaces,
SYS_ADMIN, SYS_PTRACE or privileged mode to make admission pass.

The v1 reader requires all kernel membership paths to be `/`, an exact matching
set of private read-only controller mounts, protected regular quota files,
hierarchical memory accounting, finite memory <=1GiB, memory+swap equal to
memory, 2..64 PIDs, exactly one CPU via CFS quota/period, bounded private work
and shared-memory tmpfs, and no host control sockets/GPU devices. Its root-owned
mode0755 parent tmpfs contains mountpoints only; individual controllers must be
ro/nosuid/nodev/noexec. Missing/unreadable memsw, host-visible paths, extra/nested
mounts or unknown/unbounded limits fail closed. A v2 rejection never falls back
to v1. Disposable/v2 admission rules are retained.

## Build and authentication procedure

Use a clean checkout of the public source SHA recorded with the resulting
image, including the pinned PR93 client object. Follow `prepare.py`'s committed
`build_context` implementation: it reads committed Git blobs, joins the base
goal Dockerfile and resident extension, verifies the fixed client SHA256, and
adds only approved input blobs. Do not build by copying a running container,
committing its writable layer, or sending HOME/credentials as build context.

The composed build pins Codex0.160.0 and requires explicit official Linux x64
runner version/SHA256. Retain the measured base-image digest and resulting
image ID with the source SHA and build arguments. A mutable base tag alone is
not an immutable image identifier.

For a separately approved new deployment, use the README's installer with
dedicated private file inputs and an intake policy initially OFF. It creates a
stopped container with two fresh exclusive volumes. Initial authentication is
outside the image: manager GitHub hosts, worker token/config and ChatGPT auth
enter the protected control volume; one-time runner registration is supplied
through stdin to the fixed helper as UID10003. Never print or bake these values
into a build layer. Registration and repo enablement are distinct steps.

Fresh runner volume copy-up now receives UID/GID10003, mode0700 after official
archive extraction. Existing volumes mask the image and are not automatically
repaired. Bootstrap refuses unsafe owners/modes and preserves registration
files. The old running volume had already been manually repaired and must not
be treated as proof of the new image's initial mode.

## New verification (2026-11-07)

Results will be recorded here after execution, with normal/-O totals, each skip
reason, failures, source/image IDs and the distinction between a production
image startup check and the synthetic lifecycle diagnostic. No new real model
or GitHub event replay is authorized by the historical Issue96/97 approvals.

## Cutover and rollback

Implementation/PR creation does not replace the live container. Before a
separately confirmed cutover: record exact source/image/full IDs, reconcile
pending/active inbox rows and uncertain claims without resetting them, confirm
no worker is running, record private volume ownership, and prepare protected
consistent backups. Never expose or copy secrets into this public report.

Avoid two active runners/managers dispatching the same requests. Identify the
one old service to stop and the newly verified replacement explicitly. Starting
a new service requires an accurate fresh deployment manifest and exclusive
volumes; changing image metadata or reusing the old deployment ID is not a
migration. Preserve old claims, outcomes, registration state and artifacts.
Do not replay Issue96/97 or reuse their approval/expiry/ledger.

Rollback plan: preserve the old image/container and its volumes; stop only the
identified replacement, verify no surviving worker/ambiguous job, and reconcile
any receipts/claims created after cutover before restarting the old service.
Do not reset claims, remove latches, register replacements with `--replace`,
delete old artifacts or run `compose down -v`. If new state cannot be reconciled,
keep intake blocked for investigation rather than silently switching back.
Normal job completion must retain PID1/manager/runner and clean only the owned
worker/descendants and temporary workspace.
