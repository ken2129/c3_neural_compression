FROM python:3.11-slim-bookworm
ARG SOURCE_SHA
ARG INPUT_SOURCE_SHA
ARG DEPLOYMENT_ID
RUN test "${#SOURCE_SHA}" = 40 \
 && apt-get update && apt-get install -y --no-install-recommends gh ca-certificates nodejs npm \
 && npm install --global --prefix /opt/codex @openai/codex@0.160.0 \
 && native="$(find /opt/codex/lib/node_modules -type f -path '*/vendor/*/bin/codex')" \
 && test "$(printf '%s\n' "$native" | wc -l)" = 1 && test -n "$native" \
 && native_root="$(dirname "$(dirname "$native")")" \
 && test -f "$native_root/codex-package.json" \
 && test -x "$native_root/bin/codex-code-mode-host" \
 && test -f "$native_root/codex-resources/bwrap" && test ! -L "$native_root/codex-resources/bwrap" \
 && test -x "$native_root/codex-resources/bwrap" \
 && rm /opt/codex/bin/codex \
 && cp -a "$native_root/." /opt/codex/ \
 && test -f /opt/codex/codex-package.json \
 && test -x /opt/codex/bin/codex-code-mode-host \
 && test -f /opt/codex/codex-resources/bwrap && test ! -L /opt/codex/codex-resources/bwrap \
 && test -x /opt/codex/codex-resources/bwrap \
 && /opt/codex/bin/codex --version \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control \
 && groupadd --gid 10002 worker && useradd --uid 10002 --gid 10002 --no-create-home --shell /bin/bash worker \
 && mkdir -p /control/gh /control/home /opt/service /opt/c3-runtime \
 && chown -R 10001:10001 /control && chmod 0700 /control /control/gh /control/home \
 && printf '%s\n' "$SOURCE_SHA" > /opt/service/source-sha \
 && printf 'c3-dedicated-pid1-goal-v1\n' > /opt/c3-runtime/dedicated-pid1-goal
LABEL c3.deployment-id="$DEPLOYMENT_ID" c3.purpose="dedicated-goal-controller" c3.source-sha="$SOURCE_SHA" c3.input-source-sha="$INPUT_SOURCE_SHA"
COPY research_harness/ /opt/service/research_harness/
COPY tools/research_goal/launcher.py /opt/service/goal_launcher.py
COPY goal-inputs/ /opt/goal-inputs/
COPY goal-inputs.json /opt/c3-runtime/goal-inputs.json
ENTRYPOINT ["python","-I","-B","/opt/service/goal_launcher.py"]
