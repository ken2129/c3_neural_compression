#!/usr/bin/env bash
# Run only on the intended Docker host after reviewing and approving this config.
set -euo pipefail
ALLOW_CANDIDATE=false
if [[ "${1:-}" == --allow-seccomp-candidate && $# == 1 ]]; then
  ALLOW_CANDIDATE=true
elif (( $# != 0 )); then
  echo 'Usage: bash run_checks.sh [--allow-seccomp-candidate]' >&2
  exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
PROJECT=uchishiba-codex-sandbox-check
COMPOSE=(docker compose --env-file /dev/null -p "$PROJECT" -f compose.yaml --profile diagnostic)
CANDIDATE=(docker compose --env-file /dev/null -p "$PROJECT" -f compose.yaml -f compose.seccomp-candidate.yaml --profile diagnostic)
command -v docker >/dev/null
docker compose version
if [[ -n "${DOCKER_HOST:-}" || -n "${DOCKER_CONTEXT:-}" ]]; then
  echo 'STOP: unset Docker endpoint overrides and select the intended local context explicitly.' >&2
  exit 1
fi
endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
[[ "$endpoint" == unix://* ]] || { echo 'STOP: only a local Unix-socket Docker context is allowed for this diagnostic.' >&2; exit 1; }
existing=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
if [[ -n "$existing" ]]; then
  echo 'STOP: this diagnostic project already has containers. No existing container was changed.' >&2
  exit 1
fi
existing_image=$(docker image ls -q uchishiba-codex-sandbox-check:0.160.0-draft)
if [[ -n "$existing_image" ]]; then
  echo 'STOP: diagnostic image tag already exists. Do not overwrite it without reviewing the previous run.' >&2
  exit 1
fi
root=$(docker info --format '{{.DockerRootDir}}')
[[ -n "$root" && -d "$root" ]] || { echo 'STOP: Docker storage path is not locally inspectable; run on the intended Docker host.' >&2; exit 1; }
df -h -- "$root"
available_kib=$(df -Pk -- "$root" | awk 'END {print $4}')
[[ "$available_kib" =~ ^[0-9]+$ ]] || { echo 'STOP: cannot determine available storage.' >&2; exit 1; }
if (( available_kib < 2097152 )); then
  echo 'STOP: less than 2 GiB free. This guard is not a build-size guarantee.' >&2
  exit 1
fi
umask 077
LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/codex-sandbox-check.XXXXXXXX")
echo "Logs: $LOG_DIR"
"${COMPOSE[@]}" config > "$LOG_DIR/baseline-config.yaml"
"${CANDIDATE[@]}" config > "$LOG_DIR/seccomp-config.yaml"
"${COMPOSE[@]}" build sandbox-check 2>&1 | tee "$LOG_DIR/build.log"
docker image inspect uchishiba-codex-sandbox-check:0.160.0-draft --format '{{.Id}}' | tee "$LOG_DIR/image-id.txt"
set +e
"${COMPOSE[@]}" run --rm --no-deps -T sandbox-check 2>&1 | tee "$LOG_DIR/baseline.log"
statuses=("${PIPESTATUS[@]}")
baseline=${statuses[0]}
set -e
(( statuses[1] == 0 )) || { echo "STOP: baseline log could not be saved." >&2; exit 1; }
printf 'baseline_exit=%s\n' "$baseline" | tee "$LOG_DIR/status.txt"
if (( baseline == 0 )); then
  echo 'Baseline passed. No seccomp candidate applied. This is not production acceptance.'
  exit 0
fi
if [[ "$ALLOW_CANDIDATE" != true ]]; then
  echo 'Stopped after baseline. The candidate requires --allow-seccomp-candidate and prior approval.' >&2
  exit "$baseline"
fi
if (( baseline != 1 )) || ! grep -qx 'sandbox_true_exit=1' "$LOG_DIR/baseline.log" || ! grep -Eq '^bwrap: No permissions to create (a )?new namespace([,. :]|$)' "$LOG_DIR/baseline.log"; then
  echo 'STOP: baseline did not show the expected namespace refusal; no wider seccomp policy applied.' >&2
  exit "$baseline"
fi
set +e
"${CANDIDATE[@]}" run --rm --no-deps -T sandbox-check 2>&1 | tee "$LOG_DIR/seccomp.log"
statuses=("${PIPESTATUS[@]}")
candidate=${statuses[0]}
set -e
(( statuses[1] == 0 )) || { echo "STOP: candidate log could not be saved." >&2; exit 1; }
printf 'seccomp_candidate_exit=%s\n' "$candidate" | tee -a "$LOG_DIR/status.txt"
echo "Logs: $LOG_DIR"
echo 'No further relaxation or retries. Report these results before the next step.'
exit "$candidate"
