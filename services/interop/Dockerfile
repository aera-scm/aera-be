FROM ghcr.io/astral-sh/uv:0.12.18 AS uv
FROM python:3.12.14-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-default-groups --no-install-project
COPY services ./services
RUN useradd --uid 10001 --create-home app
USER app
EXPOSE 8080
CMD ["/app/.venv/bin/python", "-m", "services.interop.app"]
