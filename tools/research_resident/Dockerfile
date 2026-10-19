# Installer concatenates the unchanged committed research_goal/Dockerfile and
# this committed extension. The original Codex distribution checks are retained.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 curl git libicu72 libkrb5-3 libssl3 liblttng-ust1 libunwind8 zlib1g \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10003 actions && useradd --uid 10003 --gid 10003 --no-create-home actions \
 && mkdir -p /runner /opt/c3/bin /opt/c3/actions /control-ipc \
 && chown 10003:10003 /runner && chmod 0700 /runner \
 && chown 10001:10001 /control-ipc && chmod 0711 /control-ipc
ARG RUNNER_VERSION
ARG RUNNER_SHA256
RUN test -n "$RUNNER_VERSION" && test "${#RUNNER_SHA256}" = 64 \
 && curl --fail --location --max-time 120 --output /tmp/runner.tgz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
 && echo "$RUNNER_SHA256  /tmp/runner.tgz" | sha256sum --check --strict \
 && tar -xzf /tmp/runner.tgz --no-same-owner -C /runner \
 && rm /tmp/runner.tgz && chown -R 10003:10003 /runner
COPY tools/research_resident/launcher.py /opt/service/resident_launcher.py
COPY tools/research_resident/register_runner.py /opt/service/register_runner.py
COPY tools/research_resident/submit_issue_event.py /opt/c3/bin/submit-issue-event
COPY issue_event.py /opt/c3/actions/issue_event.py
RUN chmod 0555 /opt/c3/bin/submit-issue-event /opt/c3/actions/issue_event.py \
 && test -x /usr/bin/python3
LABEL c3.purpose="dedicated-resident-controller"
ENTRYPOINT ["python","-I","-B","/opt/service/resident_launcher.py"]
