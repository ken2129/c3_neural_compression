FROM python:3.11-slim-bookworm
RUN groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control \
 && groupadd --gid 10002 worker && useradd --uid 10002 --gid 10002 --no-create-home worker
COPY probe.py /opt/boundary/probe.py
RUN chmod 0555 /opt/boundary/probe.py
ENTRYPOINT ["python", "-I", "-B", "/opt/boundary/probe.py"]
