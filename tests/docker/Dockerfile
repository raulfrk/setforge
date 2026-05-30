# syntax=docker/dockerfile:1.7
#
# Dockerfile for the E2E test ring.
#
# Build with the repo root as the context (so COPY can pull in
# pyproject.toml / uv.lock / setforge/ / tracked/):
#
#   docker build -t setforge-e2e:test-<inputs-hash> -f tests/docker/Dockerfile .
#
# A repo-root .dockerignore keeps the context lean. The conftest
# `docker_image` fixture invokes the same form; the `-<inputs-hash>`
# suffix is derived from the content of all hashed inputs (see
# `_image_tag` in conftest.py) and changes whenever
# anything baked into the image changes.
#
# Layer order is stable→volatile so GHA cache misses on source edits
# don't invalidate the slow binary-install layers:
#
#   1. base image
#   2. apt deps + Microsoft repo + pinned `code` (root)
#   3. nodejs + pinned `@anthropic-ai/claude-code` (root)
#   4. create non-root user `tester` (open question 3 resolution —
#      `code --list-extensions` refuses to run as root)
#   5. uv install + managed Python 3.13 (as tester)
#   6. pyproject + uv.lock → uv sync (Python deps; as tester)
#   7. project source COPY + final uv sync (as tester)
#
# Pinned versions (open question 1 resolution):
#   - claude-code:  pinned via `@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}`
#   - code:         pinned to ${VSCODE_VERSION}-stable from Microsoft's repo
#   - python:       uv-managed ${PYTHON_VERSION} (Debian 12 apt only
#                   ships 3.11; setforge uses PEP 695 generics so >= 3.12
#                   is required).

FROM debian:12-slim

ARG VSCODE_VERSION=1.95.3
# claude-code 2.1.x is required for the `claude plugin` subcommand
# (which the comprehensive variant exercises). Pinned at the current
# npm `stable` dist-tag at spec-writing time.
ARG CLAUDE_CODE_VERSION=2.1.126
ARG NODE_MAJOR=20
ARG PYTHON_VERSION=3.13
# Gitleaks v8.x — used by the pre-deploy secrets scan
# Docker e2e cases. Pinned to a recent stable release.
ARG GITLEAKS_VERSION=8.21.2

ENV DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------
# Layer 2: apt prerequisites (stable, rarely changes)
# ----------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        git \
        patch \
        zsh \
        fish \
        shellcheck \
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
# Layer 4b: gitleaks (pinned) — supports setforge install's pre-deploy
# secrets scan e2e cases. Installed to /usr/local/bin so it lives on
# PATH for the `tester` user. The "no-gitleaks" case removes/renames
# this binary at test time.
# ----------------------------------------------------------------------
RUN curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks \
    && chmod +x /usr/local/bin/gitleaks

# ----------------------------------------------------------------------
# Create non-root user; everything from here on runs as `tester`.
# ----------------------------------------------------------------------
RUN useradd --create-home --shell /bin/bash tester \
    && mkdir -p /workspace \
    && chown tester:tester /workspace
USER tester
ENV HOME=/home/tester
ENV PATH=/home/tester/.local/bin:${PATH}
WORKDIR /workspace

# ----------------------------------------------------------------------
# Layer 5: uv install + managed Python (as tester)
# ----------------------------------------------------------------------
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /home/tester/.local/bin/uv python install ${PYTHON_VERSION}

# ----------------------------------------------------------------------
# Layer 6: Python deps (changes only when pyproject/uv.lock change).
# Skips project install — `--no-install-project` avoids a wasted
# re-install in the final layer that COPYs setforge/ source.
# ----------------------------------------------------------------------
COPY --chown=tester:tester pyproject.toml uv.lock ./
RUN uv sync --extra dev --python ${PYTHON_VERSION} --no-install-project

# ----------------------------------------------------------------------
# Layer 7: project source (most volatile — must be last)
# ----------------------------------------------------------------------
COPY --chown=tester:tester . .
# Drop any host-build .venv that survived .dockerignore (defense-in-depth)
# and re-sync against the prepared dep tree.
RUN rm -rf .venv \
    && uv sync --extra dev --python ${PYTHON_VERSION}

# Default to a no-op shell — tests `docker exec` everything they need.
CMD ["sleep", "infinity"]
