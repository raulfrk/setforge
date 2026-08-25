# syntax=docker/dockerfile:1.7
#
# Dockerfile for the E2E test ring.
#
# The PR smoke lane builds the named `smoke` target. It keeps the real Debian,
# VS Code, gitleaks, shells, Python, and SetForge boundaries used by smoke tests
# while omitting Claude, Go, and Rust. The final/default `full` target retains
# every tool used by the exhaustive Docker ring.
#
# Build from the repository root:
#
#   docker build --target smoke -t setforge-e2e:smoke-<hash> -f tests/docker/Dockerfile .
#   docker build --target full  -t setforge-e2e:full-<hash>  -f tests/docker/Dockerfile .
#
# Runtime COPY inputs are intentionally explicit and mirror the content hash in
# tests/docker/image.py. Unrelated repository files must not invalidate images.

FROM debian:12-slim AS system-base

ARG VSCODE_VERSION=1.95.3
ARG CLAUDE_CODE_VERSION=2.1.126
ARG NODE_MAJOR=20
ARG PYTHON_VERSION=3.13
ARG GITLEAKS_VERSION=8.21.2
ARG GO_VERSION=1.22.5
ARG RUST_VERSION=1.79.0

ENV DEBIAN_FRONTEND=noninteractive

# Shared system boundary: all smoke and full tests run against the same Debian
# packages and pinned gitleaks binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        git \
        patch \
        zsh \
        fish \
        shellcheck \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
        | tar -xz -C /usr/local/bin gitleaks \
    && chmod +x /usr/local/bin/gitleaks

RUN useradd --create-home --shell /bin/bash tester \
    && mkdir -p /workspace \
    && chown tester:tester /workspace

FROM system-base AS python-runtime

USER tester
ENV HOME=/home/tester
ENV GOBIN=/home/tester/go/bin
ENV PATH=/home/tester/.local/bin:/home/tester/.cargo/bin:${GOBIN}:${PATH}
WORKDIR /workspace

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /home/tester/.local/bin/uv python install ${PYTHON_VERSION}

FROM python-runtime AS vscode-base

USER root
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/code stable main" \
        > /etc/apt/sources.list.d/vscode.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "code=${VSCODE_VERSION}-*" \
    && rm -rf /var/lib/apt/lists/*

# Lock-sensitive dependencies sit after stable tools so pyproject/lock edits do
# not rebuild VS Code (or Claude/Go/Rust in the full graph).
FROM vscode-base AS smoke-deps

USER tester
COPY --chown=tester:tester pyproject.toml uv.lock ./
RUN uv sync --extra dev --python ${PYTHON_VERSION} --no-install-project

# The PR target stops before the expensive full-only toolchain layers.
FROM smoke-deps AS smoke

COPY --chown=tester:tester README.md ./
COPY --chown=tester:tester .gitignore ./
COPY --chown=tester:tester setforge/ ./setforge/
COPY --chown=tester:tester tests/fixtures/e2e/ ./tests/fixtures/e2e/
COPY --chown=tester:tester tests/docker/_button_bar_demo.py ./tests/docker/_button_bar_demo.py
COPY --chown=tester:tester tests/docker/offline_extension.py ./tests/docker/offline_extension.py
RUN uv sync --extra dev --python ${PYTHON_VERSION}

CMD ["sleep", "infinity"]

FROM vscode-base AS full-tools

USER root
# claude-code's native binary is an optional dependency. Bound npm's retry
# window, retry install+verification as a unit, and never cache a stub-only
# installation as a successful layer.
RUN curl --retry 5 --retry-connrefused --retry-delay 5 -fsSL \
        "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm config set fetch-retries 2 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && for attempt in 1 2 3 4 5 6; do \
         echo "claude-code install attempt ${attempt}" \
         && npm install -g --include=optional \
              "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
         && claude --version && break \
         || { echo "attempt ${attempt} failed; retrying"; sleep 15; }; \
       done \
    && claude --version

RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
        | tar -xz -C /usr/local
ENV PATH=/usr/local/go/bin:${PATH}

USER tester
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y \
            --profile minimal \
            --default-toolchain ${RUST_VERSION}

FROM full-tools AS full-deps

COPY --chown=tester:tester pyproject.toml uv.lock ./
RUN uv sync --extra dev --python ${PYTHON_VERSION} --no-install-project

FROM full-deps AS full

COPY --chown=tester:tester README.md ./
COPY --chown=tester:tester .gitignore ./
COPY --chown=tester:tester setforge/ ./setforge/
COPY --chown=tester:tester tests/fixtures/e2e/ ./tests/fixtures/e2e/
COPY --chown=tester:tester tests/docker/_button_bar_demo.py ./tests/docker/_button_bar_demo.py
COPY --chown=tester:tester tests/docker/offline_extension.py ./tests/docker/offline_extension.py
RUN uv sync --extra dev --python ${PYTHON_VERSION}

CMD ["sleep", "infinity"]
