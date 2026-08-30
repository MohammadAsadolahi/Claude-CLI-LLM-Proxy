# Claude CLI -> OpenAI proxy, containerised.
#
# The proxy shells out to the Claude Code CLI, so the image carries Node + the
# CLI (pinned to the host's version) plus the small FastAPI app. Authentication
# is NOT baked in: docker-compose.yml bind-mounts the host's ~/.claude directory
# (OAuth tokens) and copies ~/.claude.json at start-up — see entrypoint.sh.

FROM node:22-slim

ARG CLAUDE_CODE_VERSION=2.1.204

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLAUDE_PATH=/usr/local/bin/claude \
    PORT=8082

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# The CLI refuses to run as root when permissions are bypassed (a common host
# setting), so run as the image's unprivileged "node" user (uid 1000).
USER node
ENV HOME=/home/node

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8082/health')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
