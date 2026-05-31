FROM python:3.12-slim

# Disable .pyc files and buffer flushing; uv link mode required in containers
# (hardlinks don't work across overlay filesystem layers)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install uv --no-cache-dir

WORKDIR /app

# Install runtime deps first (cached layer unless pyproject/lockfile change)
# README.md is required by hatchling (build backend) to install the project itself
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen

# Source and runtime config
COPY src/ ./src/
COPY config/container.toml ./config/container.toml

# Secrets arrive as env vars via ACA secretRef (Key Vault references).
# See infra/AZURE_PLAN.md §7.3 for the full env var inventory.
CMD ["uv", "run", "teetime", "run", "--config", "/app/config/container.toml"]
