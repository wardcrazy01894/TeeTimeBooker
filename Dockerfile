FROM python:3.12-slim

# Disable .pyc files and buffer flushing; uv link mode required in containers
# (hardlinks don't work across overlay filesystem layers)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install uv --no-cache-dir

WORKDIR /app

# Install runtime deps first WITHOUT the project itself, so this layer stays
# cached unless pyproject/lockfile change. --no-install-project is required
# because the project source (src/) is not copied yet; installing it here would
# either fail or bake an empty package into the venv. README.md is copied now
# because hatchling (build backend) reads it during the later project install.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-install-project --no-dev --frozen

# Add the venv's bin directory to PATH so that console scripts (including
# "teetime") are callable as bare executables. This is required because the
# ACA Jobs override the container command with command: ['teetime'] (no "uv
# run" wrapper), so the venv must be on PATH for the entrypoint to resolve.
ENV PATH="/app/.venv/bin:$PATH"

# Now copy the source and install the project itself into the venv. Without
# this second sync the `teetime` package is never installed and a bare
# `teetime` invocation fails with ModuleNotFoundError (the old `uv run teetime`
# CMD masked this by re-syncing at runtime).
COPY src/ ./src/
RUN uv sync --no-dev --frozen

# Runtime config
COPY config/container.toml ./config/container.toml

# Invoke teetime directly (venv is on PATH above) — matches how ACA Jobs
# invoke it via command: ['teetime']. Drop "uv run" to keep parity.
# Secrets arrive as env vars via ACA secretRef (Key Vault references).
# See infra/AZURE_PLAN.md §7.3 for the full env var inventory.
CMD ["teetime", "run", "--config", "/app/config/container.toml"]
