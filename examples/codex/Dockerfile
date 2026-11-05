FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
ARG CODEX_VERSION=0.159.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ripgrep ca-certificates bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && mkdir -p /home/node/.codex /workspace \
    && chown node:node /home/node/.codex /workspace \
    && git config --system --add safe.directory /workspace
ENV CODEX_HOME=/home/node/.codex PYTHONDONTWRITEBYTECODE=1
COPY codex-agent.py /app/codex-agent.py
USER node
WORKDIR /workspace
ENTRYPOINT ["python3", "-B", "/app/codex-agent.py"]
