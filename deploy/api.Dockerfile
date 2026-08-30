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

# `uv sync` runs before the source is copied -- so the layer cache survives a
# code-only change -- which means the project itself is never installed and the
# `cam` console script pyproject declares is never generated. The module is on
# sys.path regardless, so a two-line shim gives the documented command a real
# entry point without paying for a second dependency resolution.
RUN printf '#!/bin/sh\nexec python -m app.cli "$@"\n' > /usr/local/bin/cam \
    && chmod +x /usr/local/bin/cam
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
