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
Full correspondence checks matched51 runtime Python/CLI/client files and the
source marker for both the existing deployed SHA and the new public image SHA,
with no mismatches. See the structured evidence below.

## Host prerequisites and refusal conditions

Linux x86_64 with private PID/IPC/cgroup namespaces, pidfd and child subreaping is
required. The observed host uses kernel 5.15 and Docker's cgroup-v1 layout.
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

The composed build pins Codex 0.160.0 and requires explicit official Linux x64
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

## New verification (2026-11-07 to 2026-11-08)

Tested local source: `2aa0f678095b6f7d11095b2da79465b087bdca88`. Public source:
`7b18350af315ec1537bf13dc5f13ea28e736f21b` in PR104. The GitHub connector
published an equivalent commit because the Mac credential helper was unavailable.
Both full Git trees are exactly `193ad301acd23cbb777a1a54b87110b2482db5b8`;
only commit metadata differs. Tests and native diagnostic ran on the local SHA;
the production rebuild uses the public SHA. Subsequent report-only commits do
not change runtime or test bytes.

Tests ran as UID1001 under Linux/Python 3.11 in new containers with 1CPU/1GiB,
64PIDs, no GPU, no network, read-only source/root and a 256 MiB test tmpfs.
The suite uses actual temp executable fixtures, so only that test tmpfs uses
`exec`; the production work tmpfs remains `noexec`.

| Invocation | Discovered | Passed | Skipped | Failures/errors | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| `python -B -m unittest discover -s tests -p 'test_harness_*.py'` | 1112 | 1111 | 1 | 0/0 | 23.969 |
| Same suite with `-O` | 1112 | 1111 | 1 | 0/0 | 23.965 |

Both used the identical discovery suite through `unittest.TextTestRunner` so
skip reasons could be recorded. The skipped test is
`ResidentRealLinuxTests.test_real_procfs_self_observation_matches_broker_anchor_when_pid1_links_are_denied`:
it requires a root-owned PID1; the unprivileged test PID1 is not the root broker.
This skip is not proof of live broker isolation.

The first run, before correcting the test harness's tmpfs flags, reported
5 failures and 2 errors from executable fixtures on a `noexec` mount, with the
same 1112 discovered/1 skip. No runtime code was changed to obtain the pass.
The complete normal and optimized suites were then rerun on an executable test
tmpfs and both passed. These results are new; they do not reuse PR100 counts.

Live read-only observations found PID1/root, runner UID10003 and manager UID10001
unchanged since 2026-10-24, no UID10002 worker, `/work` empty, two
WORKER_FINISHED service rows, ten FINISHED inbox rows and no blocked latch.
`/runner` is 10003:10003/0700 and its four registration records are 10003:10003/0600.
No registration-file contents were read or published.
An independent post-check matched container/image/start time/restart count,
all three permanent process identities, state counts, and every recorded
registration-file owner/mode/inode/mtime. No existing state was repaired/reset.
Structured results and observation limits are in [ISSUE101_EVIDENCE.json](ISSUE101_EVIDENCE.json).

Production-image build/startup and synthetic lifecycle results follow below.
No new real model or GitHub event replay is authorized by the historical
Issue96/97 approvals. New inference, real runner registration, persistent
credential transfer and cutover have not been performed.

### Native model-free lifecycle diagnostic

Executed on 2026-11-08 using the committed `run_diagnostic.py`, not the old E2E
approvals. Its GitHub/approval/Codex/runner peers are fixed synthetic fixtures;
UID switching, native cgroup-v1 resource admission, IPC, SQLite, pidfd/ECHILD
cleanup and process residency are real Docker/Linux operations.

- Source: local `2aa0f678095b6f7d11095b2da79465b087bdca88` (public-equivalent tree above).
- Container: `4fef8ea03bcac1b35a3cd434ed9fb5bd102eb31b2896ca0b13e9e4632f7ed6ef`.
- Image: `sha256:3dcff0a0d594e1803c6c8ca5d514dfa913b08239c0f9a984e7be4b50dde32b8e`.
- Result SHA256: `576e9a318f6c064c431e5c4f8d0b2b6b413efd8b6381783fc89ba85ca1ff9a2c`.
- Run 9.453 seconds; total build/create/run/stop 13.173 seconds. All 18 saved checks
  true: A cancellation, BLOCKED before failure ACK, ECHILD/empty workspace,
  B completed in the same PID1/manager/runner, duplicate suppression, two claims,
  four FINISHED inbox rows and private synthetic records.
- The success observation was running with RestartCount=0. The script then
  deliberately stopped only its new container, leaving it exited 143 with
  RestartCount=0. An independent readback verified the result checksum and
  UID/GID10001/mode0600. Both diagnostic volumes and the stopped container remain.

No real GitHub API/event, model inference, result PR or external notification
was exercised by this diagnostic. These 18 checks do not replace that evidence.

### Production image and startup

The first build of the equivalent local source stopped at the 600 second ceiling
while npm was fetching its pinned native package (exit 130, about 546 MB disk
growth). A subsequent direct fetch of the same public package succeeded.
The public-source rebuild completed without a code or dependency-version change:

| Pin/result | Value |
| --- | --- |
| Public source | `7b18350af315ec1537bf13dc5f13ea28e736f21b` |
| Input source | `6fd11016d64f88721eb08caff2bb8b0988f3d847` |
| Image tag on the inspected engine | `uchishiba-c3-issue101:7b18350a` |
| Immutable image ID | `sha256:c26c26168b575c974f6b4f1a4d2144a8a0a5458485e1c82aaab0c79c4bbee266` |
| Base manifest digest | `python@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39` |
| Runner | `2.338.1` |
| Runner archive SHA256 | `08e9ccb3604d2462c24b5e33604dc30810296ca15cc65f23cffd4317eb144c37` |
| Codex | `0.160.0` (existing pin retained) |
| Build result | exit 0, 88.131 seconds, 2,224,779,264 bytes observed disk growth |
| Build context archive SHA256 | `93d32585e9297fe8384418877f1b76af682db43f82fd551f46eab7f357693eca` |

Every legacy-builder RUN used CFS quota/period 100000/100000 and 1 GiB memory/no
swap. The 600 second / 4 GiB growth monitor did not trigger in the successful build.
The generated Dockerfile is exactly the committed goal base plus resident
extension. No running-container layer was committed or patched.

The production-image probe passed real private cgroup-v1 admission with
memory 1073741824, swap 0, pids 64, cpu_quota/cpu_period 100000/100000 and
work_bytes 268435456. The image's runner directory was 10003:10003/0700,
with no credentials/registration marker. Fresh-volume copy-up preserved that
owner/mode in the actual production launcher test:

- Container: `426eeff47cb4bdae928e7d3813eb333de1f3a1b12742d5f0bb3781127720b8a1`.
- Dedicated fresh control/runner volumes, intake OFF, no real credentials copied,
  no runner registered and no model launched.
- Observed `RESIDENT_WAITING_FOR_RUNNER` while running, RestartCount=0.
- Only the new test container was then stopped; final status exited 143,
  RestartCount=0. Its volumes are preserved. Production manager startup after
  real registration remains untested; native manager lifecycle above uses fixtures.

The first observer looked only at stdout and missed the phase on stderr, so
it timed out and stopped its own container. The observer was fixed to capture
both streams, then manually started/stopped that same new container once more.
Thus this production setup check had two explicit starts; it is not evidence
of unattended setup or of per-job restarts. The image/volume needed no patches.

#### Build recipe (no authentication, no deployment)

Use a clean checkout of the public source above and fetch the pinned PR93
client object. On a clean build engine, pulling the recorded base manifest and
tagging it `python:3.11-slim-bookworm` selects the measured base for the committed
Dockerfile. The build remains dependent on apt feeds; bit-for-bit reproducibility
of a future fresh build is not claimed. Preserve/reuse the measured image ID
for exact image identity. No registry push was performed.

From that checkout, create a context outside the checkout:

```sh
export C3_ISSUE101_CONTEXT="$(mktemp -d)"
python3 -B - <<'PY'
import importlib.util, os
from pathlib import Path
spec = importlib.util.spec_from_file_location(
    'resident_prepare', 'tools/research_resident/prepare.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.build_context(Path(os.environ['C3_ISSUE101_CONTEXT']), {
    'runtime_source_sha': '7b18350af315ec1537bf13dc5f13ea28e736f21b',
    'input_source_sha': '6fd11016d64f88721eb08caff2bb8b0988f3d847',
}, {'inputs': []})
PY
DOCKER_BUILDKIT=0 docker build --cpu-period 100000 --cpu-quota 100000 \
  --memory 1g --memory-swap 1g --pull=false --network bridge \
  --build-arg SOURCE_SHA=7b18350af315ec1537bf13dc5f13ea28e736f21b \
  --build-arg INPUT_SOURCE_SHA=6fd11016d64f88721eb08caff2bb8b0988f3d847 \
  --build-arg DEPLOYMENT_ID=f2e12fff10527a3c8d8e87725b26d64b \
  --build-arg RUNNER_VERSION=2.338.1 \
  --build-arg RUNNER_SHA256=08e9ccb3604d2462c24b5e33604dc30810296ca15cc65f23cffd4317eb144c37 \
  -f "$C3_ISSUE101_CONTEXT/Dockerfile.resident" \
  -t uchishiba-c3-issue101:7b18350a "$C3_ISSUE101_CONTEXT"
```

The recorded deployment ID above belongs to the stopped startup check. These
are build-only replay arguments, not permission to create another service with
that ID. A new actual deployment needs a fresh approved profile/ID, dedicated
authentication and real inspect manifest. The optional build-metadata JSON in
the measured context held synthetic planning data only and is not copied into
the image; the recipe does not need that observer file.

## Completed scope and verification limits

Issue101 packages the local fixes and demonstrates a build and isolated startup
from the public source. Replacing the running controller is not required to
complete that scope. These reproduction checks retain the existing controller
on its recorded image. The implementation and evidence in PR104 are ready for
review, and merge remains a separate decision.

The real-runtime image reached unregistered-runner wait with no manual image
or volume patch. The separate native model-free diagnostic verified manager
residency, sequential jobs, cleanup and duplicate handling with synthetic peers.
Actual manager/registered-runner coexistence and any new actual model/PR/
notification path remain untested on the new image. The earlier successful
real E2E remains the historical evidence; it is not a new-image test result.

Real registration, credential transfer, additional live-path checks and a
possible future replacement are optional separately scoped operations, not
unfinished requirements to replace the existing controller. No previous
approval is inherited. If a new model-backed check is needed, specify new
Issue/request IDs, model calls, cost ceiling, duration and stop conditions
before approval. Do not close Issue95 or merge related PRs as part of this work.

## Verification cleanup and optional future rollback

For the completed checks, only the newly created diagnostic/startup containers
were stopped. Their images, volumes and evidence were retained. No existing
service, credentials, registration records, claims or artifacts were replaced.
A failed new verification attempt is handled by stopping only its identified
test container and investigating its evidence; the old service keeps running.

The following replacement plan is retained for reference only. No replacement
is requested, approved or required by the completed reproduction checks.
Before a separately requested and confirmed cutover: record exact
source/image/full IDs, reconcile
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
