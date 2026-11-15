#!/usr/bin/env bash
# Proposed one-time installation. Never run this against an existing project.
set -euo pipefail
if [[ $# -ne 3 || "$1" != --approve-dedicated-container ]]; then
  echo 'Usage: bash tools/research_service/prepare.sh --approve-dedicated-container NEW_PROJECT REVIEWED_COMMIT' >&2
  echo 'Creates a new image, dedicated container, private volume and outbound bridge network access; starts fixed-CPU-only polling.' >&2
  echo 'Does not configure credentials or start Codex/models/GPU. Review deployment instructions first.' >&2
  exit 2
fi
project=$2
export C3_SOURCE_SHA=$3
[[ "$project" =~ ^[a-z][a-z0-9-]{5,48}$ && "$C3_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 2
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
[[ "$(git -C "$root" rev-parse HEAD)" == "$C3_SOURCE_SHA" ]] || { echo 'Reviewed commit mismatch' >&2; exit 1; }
git -C "$root" diff --quiet HEAD -- research_harness tools/research_service
[[ -z "$(git -C "$root" ls-files --others --exclude-standard -- research_harness tools/research_service)" ]] || { echo 'Untracked service sources rejected' >&2; exit 1; }
[[ -z "$(git -C "$root" ls-files --others --ignored --exclude-standard -- research_harness tools/research_service)" ]] || { echo 'Ignored build-context sources rejected; use a fresh checkout' >&2; exit 1; }
command -v python3 >/dev/null
compose=(docker compose --project-name "$project" -f "$root/tools/research_service/compose.yaml")
"${compose[@]}" config --quiet
[[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]] || { echo 'Project already has containers; refusing to modify it' >&2; exit 1; }
[[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$project")" ]] || { echo 'Project already has volumes; refusing to reuse them' >&2; exit 1; }
if docker volume inspect "${project}_control-state" >/dev/null 2>&1; then
  echo 'Planned volume name already exists; refusing to overwrite it' >&2; exit 1
fi
"${compose[@]}" create --build controller
id=$("${compose[@]}" ps -aq controller)
[[ "$id" =~ ^[0-9a-f]{64}$ ]] || exit 1
work=$(mktemp -d "${TMPDIR:-/tmp}/c3-deploy.XXXXXXXX")
chmod 700 "$work"
docker inspect "$id" > "$work/container.json"
volume=$(python3 - "$work/container.json" <<'PY'
import json,sys
items=json.load(open(sys.argv[1]))[0]['Mounts']
match=[m['Name'] for m in items if m.get('Type')=='volume' and m.get('Destination')=='/control']
if len(match)!=1:raise SystemExit('unexpected control mount')
print(match[0])
PY
)
docker volume inspect "$volume" > "$work/volume.json"
docker ps -aq --no-trunc --filter "volume=$volume" > "$work/consumers.txt"
python3 - "$root" "$work" "$volume" "$C3_SOURCE_SHA" <<'PY'
import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from research_harness.deployment_contract import validate_dedicated_deployment
root=pathlib.Path(sys.argv[2]);item=json.loads((root/'container.json').read_text())[0]
volume=json.loads((root/'volume.json').read_text())[0]
consumers=(root/'consumers.txt').read_text().splitlines()
data=dict(inspect=item,volume_inspect=volume,consumers=consumers,volume_name=sys.argv[3],source_sha=sys.argv[4],initialize_new_state=True)
validate_dedicated_deployment(item,consumers,source_sha=sys.argv[4],volume_name=sys.argv[3],volume_inspect=volume)
(root/'deployment.json').write_text(json.dumps(data))
(root/'deployment.json').chmod(0o644)  # Parent directory is private; destination /control is mode 0700.
PY
# An explicit archive avoids propagating the host checkout user's UID/GID.
python3 - "$root" "$work" <<'PYARCHIVE'
import pathlib,sys
sys.path.insert(0,sys.argv[1])
from research_harness.deployment_archive import build_archive
p=pathlib.Path(sys.argv[2]);(p/'deployment.tar').write_bytes(build_archive((p/'deployment.json').read_bytes()))
PYARCHIVE
docker cp -a - "$id:/control/" < "$work/deployment.tar"
docker cp -a "$id:/control/deployment.json" - > "$work/deployment-readback.tar"
python3 - "$root" "$work" <<'PYVERIFY'
import pathlib,sys
sys.path.insert(0,sys.argv[1])
from research_harness.deployment_archive import verify_archive
p=pathlib.Path(sys.argv[2]);verify_archive((p/'deployment-readback.tar').read_bytes(),(p/'deployment.json').read_bytes())
PYVERIFY
docker start "$id"
printf 'Dedicated fixed-CPU service created: %s\n' "$id"
printf 'Protected installation records: %s\n' "$work"
printf 'GitHub manager authentication and an explicit approved CPU-canary Issue are still required. No model/GPU support is enabled.\n'
