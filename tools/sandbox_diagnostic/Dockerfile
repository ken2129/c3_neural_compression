# Diagnostic draft. Official base manifest digest verified on 2026-10-17.
# APT packages remain unpinned; record the resulting image ID before testing.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
RUN apt-get update \
 && apt-get install -y --no-install-recommends bubblewrap ca-certificates coreutils util-linux procps \
 && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@0.160.0 \
 && npm cache clean --force \
 && groupadd --gid 10001 codex \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash codex \
 && mkdir -p /work
COPY diagnostic.sh /usr/local/bin/codex-sandbox-diagnostic
RUN chmod 0555 /usr/local/bin/codex-sandbox-diagnostic
ENV HOME=/home/codex CODEX_HOME=/home/codex/.codex
WORKDIR /work
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/codex-sandbox-diagnostic"]
