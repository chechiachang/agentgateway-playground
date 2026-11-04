FROM node:24-bookworm-slim
ARG CODEX_VERSION=0.159.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && mkdir -p /home/node/.codex /workspace \
    && chown node:node /home/node/.codex /workspace
ENV CODEX_HOME=/home/node/.codex PYTHONDONTWRITEBYTECODE=1
COPY codex-agent.py /app/codex-agent.py
USER node
WORKDIR /workspace
ENTRYPOINT ["python3", "-B", "/app/codex-agent.py"]
