FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    PORT=8080 \
    APP_ROLE=api

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Optional: bake the embedding model into the image (offline start). A failed download must not break the build.
ARG PRELOAD_EMBEDDINGS=1
RUN mkdir -p /opt/fastembed && \
    if [ "$PRELOAD_EMBEDDINGS" = "1" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" || true; \
    fi

COPY app ./app
COPY migrations ./migrations
COPY seed ./seed
# The web UI (classic + built React app); served by the api at / (app/api/mounts/herald_ui.py).
COPY static ./static

RUN useradd --create-home --uid 10001 app && chown -R app:app /opt/fastembed
USER app

EXPOSE 8080
CMD ["python", "-m", "app.main"]
