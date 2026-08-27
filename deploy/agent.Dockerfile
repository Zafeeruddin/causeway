# The agent needs every VPN client plus the tunnel and recording tooling.
# It is the only container that gets NET_ADMIN.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openfortivpn \
        openconnect \
        wireguard-tools \
        openssh-client \
        sshpass \
        iproute2 \
        iptables \
        ppp \
        ffmpeg \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev
COPY app ./app

ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.agent"]
