FROM python:3.12-slim

# ffprobe is used by the stream-handshake gate; ffmpeg by the recorder.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev
COPY app ./app

ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
