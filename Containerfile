FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && chmod -R a+rX /app/.venv

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    AUTOMERGE_CONFIG_ROOT=/etc/auto-merge-agent \
    AUTOMERGE_ACTIVE=false
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# Permissions are set before COPY to avoid duplicating the dependency layer.
# All runtime files are readable by arbitrary OpenShift UIDs. Only /tmp is writable.
USER 10001:0
EXPOSE 8000
ENTRYPOINT ["auto-merge-agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
