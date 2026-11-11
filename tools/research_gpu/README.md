# Resident GPU experiment connection

The resident controller retains its CPU-only Codex isolation. An optional,
owner-approved GPU stage executes an **installed, pinned recipe** in an existing
GPU work container, then starts the CPU Codex worker to analyze the exported
numbers, create figures and publish its normal result PR. No Codex call is kept
idle during the GPU stage and no GPU/Docker/SSH access is given to that worker.

This implementation is opt-in and uninstalled. The legacy controller remains
OFF. Tests use synthetic GPU observations and CPU commands, not real GPUs,
SSH credentials, a model call or GitHub publication. PR109 is the earlier
manual GPU pilot, not an E2E validation of this new resident connection.

Validation on 2026-11-11: all 1,193 harness tests ran both normally and with
Python `-O` (1,191 passed, two existing privilege-dependent tests skipped per
run). The disposable Linux test container had one CPU, 1 GiB RAM, 64 PIDs,
no network/GPU/host binds, and was removed afterwards. GPU observations and
the CPU analysis/PR endpoint were fixtures; subprocesses, pipes, SQLite claims,
owner verification, cancellation and descendant cleanup were exercised.
The C3 collector also read the existing completed pilot condition A with CUDA
hidden and returned one image's selected numerical fields (630-byte JSON),
without rerunning the experiment or writing the private inputs.

## One job

1. The existing Actions delivery, owner approval and global `enabled` checks
   validate the Issue. Unknown machines, uninstalled recipes and budgets above
   the installed ceiling are rejected before claiming or contacting the target.
2. CPU workspace/preflight and the existing durable once-only claim occur.
3. The manager connects over a single SSH stdio session and invokes a fixed
   `docker exec -i --user UID CONTAINER PYTHON -I -B AGENT --registry REGISTRY`.
   The Issue selects only machine/recipe names and bounded wait/run durations;
   it supplies no SSH arguments, host paths, container name or shell command.
4. The agent verifies the pinned recipe and assets, waits for an allowlisted
   GPU UUID with no compute process, utilization <=5% and memory <=256 MiB,
   obtains its local cooperative lock and rechecks immediately before launch.
5. It runs the trusted recipe, monitors time, disk/RSS/thread limits and GPU
   competition, and optionally runs a fixed CPU collection command. Logs,
   inputs, weights and full outputs remain private in the GPU container.
6. Linux subreaping, pidfds and `ECHILD` verify that its descendants, including
   escaped sessions, stopped. Only the installed public text export list is
   returned, in a canonical record no larger than 16 KiB.
7. The manager saves the receipt privately. The trusted broker stages its data
   in `/work/worker-home/gpu-result.json`; only then can the CPU Codex worker
   start. It analyzes data and publishes using the existing direct-PR path.

The GPU wait/run budget extends the resident job allocation, **not** the CPU
worker's `max_seconds`, memory or GPU policy. The agent reserves ten seconds
inside its allocation for bounded cleanup (the minimum GPU allocation is 30
seconds). The resident additionally allows bounded transport/finalization time.
One accepted job may finish after global OFF; new jobs still wait. Approval
revocation, expiry or configuration changes cancel the current GPU stage.

A verified recipe failure can reach the analysis worker as a failed result, so
it can report negative/missing results without rerunning. A lost connection or
invalid/missing cleanup receipt blocks the resident service; CPU namespace
cleanup cannot prove that a remote GPU process stopped. Interrupted claims and
existing agent run directories are never automatically reset or retried.

## Configuration stays in the existing runtime file

Add optional `gpu_targets` to the protected `/control/goal-runtime.json`.
Keep its existing single `enabled` switch and agent model setting. Omitting
`gpu_targets` preserves the previous CPU-only behavior. Non-switch changes
require the existing reviewed deployment/restart procedure.

Example fragment, with deliberately non-executable placeholders:

```json
{
  "gpu_targets": {
    "lab-gpu": {
      "transport": "ssh-docker-stdio-v1",
      "ssh_host": "gpu.example.test",
      "ssh_user": "research",
      "ssh_port": 22,
      "container": "research-work",
      "container_uid": 1001,
      "python": "/private/agent-venv/bin/python",
      "agent": "/private/reviewed-source/tools/research_gpu/agent.py",
      "registry": "/private/gpu-recipes.json",
      "identity_file": "/control/gpu-lab.key",
      "known_hosts_file": "/control/gpu-lab.known-hosts",
      "recipes": {
        "c3-image-500": {
          "definition_sha256": "REVIEWED_PRIVATE_RECIPE_DIGEST",
          "max_seconds": 1200,
          "max_wait_seconds": 1800,
          "public_outputs": ["public/result.json"]
        }
      }
    }
  }
}
```

The formally approved direct-PR plan adds only:

```json
{
  "gpu_experiment": {
    "machine": "lab-gpu",
    "recipe": "c3-image-500",
    "max_wait_seconds": 1800,
    "max_seconds": 1200
  }
}
```

This is an example, not an execution request. The existing Issue-content
approval, deployment identity, source revision and installed input/write scope
are still required. Copying a title or this fragment does not authorize a job.

## Installed GPU-side recipe

Place a reviewed source snapshot and a private registry **inside the existing
work container**. The agent needs only Python's standard library, Linux pidfds /
subreaping and `/usr/bin/nvidia-smi`; model dependencies remain the recipe's own
environment. There is no host-side daemon, mount, Docker socket or listening
port to add. Keep the registry private (0600) and its run root owned by the
installed non-root UID (0700).

Registry schema:

```text
schema: c3-gpu-registry-v1
root: private directory for fresh run-id subdirectories
recipes:
  versioned-recipe-name:
    argv: fixed model command and arguments
    collect_argv: fixed CPU export command, or []
    cwd: fixed source directory
    environment: fixed project input/dependency settings
    source_files: absolute source/input/weight paths mapped to SHA256
    gpu_uuids: explicit permitted device UUIDs
    limits:
      max_seconds, max_wait_seconds, cpu_cores, memory_bytes,
      max_processes, max_artifact_bytes, disk_reserve_bytes, idle_samples
    public_outputs: exact relative .json/.csv/.md/.txt export paths
```

`definition_sha256` is the SHA256 of the recipe's canonical JSON; use
`research_harness.gpu_contract.definition_digest`. The manager and agent must
agree on it. Operator settings are pinned by the existing deployment identity
and immutable non-switch config comparison. New recipe content needs review and
an explicit installation; mutable Issue text cannot supply replacement code.

Only `{run_dir}` and `{gpu_uuid}` are substituted into installed argv/env text.
Set model output paths under `{run_dir}`. CPU affinity and common numerical
thread counts are bounded; CUDA/cache/temp paths are directed into the run.
The GPU phase uses `JAX_PLATFORMS=cuda,cpu` because fixed C3 bookkeeping uses
the CPU backend. CPU collection has CUDA hidden and `JAX_PLATFORMS=cpu`.

For C3, an installed command can run the existing pinned
`c3_neural_compression.experiments.image` CLI with its reviewed config,
`C3_COCO_ROOT`, `C3_COCO_ANNOTATIONS`, `C3_SUBSET_IDS` and
`C3_OUTPUT_ROOT={run_dir}/codec`. A fixed `collect_argv` can call
`collect_c3.py {run_dir}/codec {run_dir}/public/result.json`. That CPU collector
requires exactly one completed comparison attempt, validates its ledger,
checks bits/denominator and teacher consistency, and exports selected numbers.
It never copies original metrics, image IDs, checksums, paths, weights or PNGs.
Other models provide their own installed commands/collectors; common control
does not import C3.

These are trusted installed recipes in the shared work container, not arbitrary
model-generated programs. RSS/process count/storage checks are sampled and do
not provide a cgroup hard limit or filesystem sandbox. GPU locks coordinate
agents; they do not reserve the device against unrelated host users. A detected
conflict stops only this agent's descendants, never another person's job.
The first version selects one GPU and expects one GPU compute process; multi-GPU
and multiple compute-process recipes (for example DDP) need a later backend
contract, rather than weakening the competition check here.

## SSH material and installation

The manager needs an explicitly supplied key authorized to SSH to the selected
host and run Docker exec. This is host-login/Docker authority in the trusted
manager, not a weaker permission created by this transport. It is never passed
to the CPU worker. This change does not discover or copy the user's existing
Mac key, change authorized_keys, or install credentials anywhere automatically.

The resident installer accepts `--gpu-ssh-material PRIVATE_MAPPING.json` only
when the reviewed profile declares targets. Its private mapping must name
exactly the registered destinations and explicit private local sources:

```json
{
  "/control/gpu-lab.key": "/explicit/private/dedicated-key",
  "/control/gpu-lab.known-hosts": "/explicit/private/reviewed-known-hosts"
}
```

Only flat `gpu-*.key` / `gpu-*.known-hosts` records are accepted; bytes are copied
to the manager control volume with UID/GID 10001 and mode 0600 and read back.
Strict host-key checking, no agent/port forwarding, no SSH proxy/local command
and no shared ControlMaster are enforced. The legacy disposable installer
refuses GPU targets. The resident image adds only openssh-client.

Actual deployment, credential transfer and real GPU E2E are separate from this
code-only change. Keep the existing deployment OFF until those concrete steps
are reviewed. Future additional transports/machines are tracked by Issue110;
the first supported backend is SSH to Docker on a Linux GPU host.

## Tests

`tests/test_harness_gpu*.py` covers contracts, bounded real pipes, cancellation,
private export validation and Linux descendant cleanup with synthetic device
observations and CPU commands. `test_harness_resident_gpu.py` combines formal
owner approval, real SQLite claims, the agent subprocess and a modeled CPU
broker/analysis/PR report. It verifies ordering, OFF, cancellation and duplicate
prevention without a model call. This does not prove the production UID/cgroup
boundary, a real SSH/GPU deployment, or actual PR creation by Codex.
