FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . .

RUN uv sync --frozen

FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app /app

EXPOSE 8765

# Until empty-state serve mode is available, mount your work directory and pass
# audio/transcript explicitly, for example:
# docker run --rm -p 8765:8765 -v "$PWD/samples:/data" podedit:dev \
#   podedit serve --host 0.0.0.0 --port 8765 \
#   --audio /data/foo.m4a --transcript /data/foo.transcript.json
CMD ["uv", "run", "podedit", "serve", "--host", "0.0.0.0", "--port", "8765"]
