# The parity client: the third container that talks to the baseline and the
# candidate over a docker network, never over loopback. Built from the same
# pete-mcp-template commit as the reusable workflow that runs it.
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /opt/parity/requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /opt/parity/requirements.txt

COPY parity.py /opt/parity/parity.py

# run.sh starts this with --user set to the runner's uid so the files it
# writes into the mounted output directory are not owned by root.
ENV HOME=/tmp
ENTRYPOINT ["python", "/opt/parity/parity.py"]
