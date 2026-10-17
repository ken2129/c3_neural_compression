FROM python:3.11-slim-bookworm
ARG SOURCE_SHA
RUN test "${#SOURCE_SHA}" = 40 \
 && apt-get update && apt-get install -y --no-install-recommends gh ca-certificates nodejs npm \
 && npm install --global --prefix /opt/codex @openai/codex@0.160.0 \
 && native="$(find /opt/codex/lib/node_modules -type f -path '*/vendor/*/bin/codex')" \
 && test "$(printf '%s\n' "$native" | wc -l)" = 1 && test -n "$native" \
 && native_root="$(dirname "$(dirname "$native")")" \
 && test -f "$native_root/codex-package.json" \
 && test -x "$native_root/bin/codex-code-mode-host" \
 && rm /opt/codex/bin/codex \
 && cp -a "$native_root/." /opt/codex/ \
 && test -f /opt/codex/codex-package.json \
 && test -x /opt/codex/bin/codex-code-mode-host \
 && /opt/codex/bin/codex --version \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control \
 && groupadd --gid 10002 worker && useradd --uid 10002 --gid 10002 --no-create-home worker \
 && mkdir -p /control/gh /control/home /opt/service /opt/c3-runtime \
 && chown -R 10001:10001 /control && chmod 0700 /control /control/gh /control/home \
 && printf '%s\n' "$SOURCE_SHA" > /opt/service/source-sha \
 && printf 'c3-dedicated-pid1-canary-v1\n' > /opt/c3-runtime/dedicated-pid1-canary
LABEL c3.purpose="dedicated-research-controller" c3.source-sha="$SOURCE_SHA"
COPY research_harness/ /opt/service/research_harness/
COPY tools/research_service/launcher.py /opt/service/launcher.py
ENTRYPOINT ["python","-I","-B","/opt/service/launcher.py"]
