# syntax=docker/dockerfile:1.7
#
# Dockerfile for the E2E test ring (dotfiles-nen.9).
#
# Build with the repo root as the context (so COPY can pull in
# pyproject.toml / uv.lock / my_setup/):
#
#   docker build -t my-setup-e2e:test -f tests/docker/Dockerfile .
#
# A repo-root .dockerignore keeps the context lean. The conftest
# `docker_image` fixture invokes the same form.
#
# Layer order is stable→volatile so GHA cache misses on source edits
# don't invalidate the slow binary-install layers:
#
#   1. base image
#   2. apt deps (curl, ca-certificates, gnupg)
#   3. Microsoft repo + pinned `code` install
#   4. nodejs + pinned `@anthropic-ai/claude-code`
#   5. uv install
#   6. pyproject + uv.lock → uv sync (Python deps)
#   7. project source (COPY . .)
#
# Pinned versions (open question 1 resolution):
#   - claude-code:  pinned via `@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}`
#   - code:         pinned to ${VSCODE_VERSION}-stable from Microsoft's repo
# Both can be bumped centrally here without touching test variants.

FROM debian:12-slim

ARG VSCODE_VERSION=1.95.3
ARG CLAUDE_CODE_VERSION=2.0.0
ARG NODE_MAJOR=20

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/root/.local/bin:${PATH}"

# ----------------------------------------------------------------------
# Layer 2: apt prerequisites (stable, rarely changes)
# ----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        git \
        patch \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------
# Layer 3: Microsoft VSCode apt repo + pinned `code` install
# ----------------------------------------------------------------------
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
        > /etc/apt/sources.list.d/vscode.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "code=${VSCODE_VERSION}-*" \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------
# Layer 4: nodejs + pinned @anthropic-ai/claude-code via npm -g
# ----------------------------------------------------------------------
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

# ----------------------------------------------------------------------
# Layer 5: uv install (Astral installer pins itself via the script)
# ----------------------------------------------------------------------
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# ----------------------------------------------------------------------
# Layer 6: Python deps (changes only when pyproject/uv.lock changes)
# ----------------------------------------------------------------------
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
RUN uv sync --extra dev

# ----------------------------------------------------------------------
# Layer 7: project source (most volatile — must be last)
# ----------------------------------------------------------------------
COPY . .
RUN uv sync --extra dev

# Default to a no-op shell — tests `docker exec` everything they need.
CMD ["sleep", "infinity"]
