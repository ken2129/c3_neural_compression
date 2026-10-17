#!/usr/bin/env bash
# Offline acceptance only. New dedicated Docker volume is retained for inspection.
set -euo pipefail
if [[ $# -ne 1 || "$1" != --allow-new-diagnostic-volume ]]; then
  echo 'Usage: bash tools/container_boundary/check_service.sh --allow-new-diagnostic-volume' >&2
  echo 'Creates a dedicated offline diagnostic image and private named volume; no credentials/model/GPU.' >&2
  exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
project="c3-service-check-$(date -u +%Y%m%d%H%M%S)-$$"
compose=(docker compose --project-name "$project" -f "$root/tools/container_boundary/compose.service.yaml")
command -v python3 >/dev/null
logs=$(mktemp -d "${TMPDIR:-/tmp}/c3-service-check.XXXXXXXX")
"${compose[@]}" config --quiet
"${compose[@]}" build
"${compose[@]}" run --rm --no-deps service-check initialize | tee "$logs/initialize.json"
"${compose[@]}" run --rm --no-deps service-check run | tee "$logs/first.json"
# A new container with the same named volume must not dispatch a second worker.
"${compose[@]}" run --rm --no-deps service-check run | tee "$logs/reopen.json"
python3 - "$logs" <<'PY_CHECK'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
values=[json.loads((root/name).read_text()) for name in ('initialize.json','first.json','reopen.json')]
for value in values:
    if value.get('fixture_only') is not True or value.get('execution_allowed') is not False:
        raise SystemExit('not an offline fixture result')
if values[0].get('initialized') is not True:raise SystemExit('initialization failed')
for value,dispatched,duplicate in ((values[1],True,False),(values[2],False,True)):
    if (value.get('phase')!='COMPLETED' or value.get('dispatched') is not dispatched
        or value.get('duplicate') is not duplicate or value.get('result_saved') is not True):
        raise SystemExit('persistence/duplicate rejection did not pass')
print('persistent_recreate_checks_passed=true')
PY_CHECK
printf 'diagnostic_logs=%s\n' "$logs"
printf 'diagnostic_project=%s\nfixture_only=true\nexecution_allowed=false\n' "$project"
printf 'Dedicated volume retained. No docker volume prune or automatic volume deletion is performed.\n'
