FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY vistula_sim ./vistula_sim

RUN useradd --create-home --uid 10001 sim
USER sim

ENV PORT=8095 \
    TARGET_API_URL=http://api:8080
EXPOSE 8095

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8095'), timeout=2)"

CMD ["sh", "-c", "exec uvicorn --factory vistula_sim.app:create_app --host 0.0.0.0 --port ${PORT:-8095}"]
