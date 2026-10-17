#!/bin/sh
# No model, repository, credentials, or network needed.
set -eu
umask 077
mkdir -p "$CODEX_HOME"
printf 'Codex: '; codex --version
printf 'Identity: '; id
printf 'AppArmor: '; cat /proc/self/attr/current || true
grep -E '^(NoNewPrivs|Seccomp|CapEff):' /proc/self/status
set +e
timeout --kill-after=2s 10s codex -c 'sandbox_mode="read-only"' sandbox /usr/bin/true
result=$?
set -e
printf 'sandbox_true_exit=%s\n' "$result"
exit "$result"
