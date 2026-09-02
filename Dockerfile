# Monolithic Dockerfile for EasyProxy
# Optimized EasyProxy runtime
# Compatible with AMD64 and ARM64 (Oracle VPS)

# wgx SOCKS5 mode is a userspace WireGuard client: no kernel interface,
# TUN device, NET_ADMIN, or sysctl is needed.
FROM debian:bookworm-slim AS wgx-builder

ARG WGX_COMMIT=333b25d79b8cd228b82fa9412b405bb48fced891
COPY wgx-compat.patch /tmp/wgx-compat.patch
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential \
    curl \
    libuv1-dev \
    libsodium-dev \
    libc-ares-dev \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/wgx \
    && curl -fL "https://github.com/wuruxu/wgx/archive/${WGX_COMMIT}.tar.gz" \
        -o /tmp/wgx.tar.gz \
    && tar -xzf /tmp/wgx.tar.gz --strip-components=1 -C /tmp/wgx \
    && patch -d /tmp/wgx -p1 < /tmp/wgx-compat.patch \
    && make -C /tmp/wgx \
    && rm -f /tmp/wgx.tar.gz

FROM python:3.12-slim-bookworm

# 1. Environment Settings
WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    tar \
    nodejs \
    netcat-openbsd \
    procps \
    ffmpeg \
    fonts-dejavu \
    ca-certificates \
    libuv1 \
    libsodium23 \
    libc-ares2 \
    && rm -rf /var/lib/apt/lists/*

# WARP config generator. wgx consumes the generated WireGuard profile in
# userspace and exposes the local SOCKS5 relay.
ARG WGCF_VERSION=2.2.29
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) wgcf_arch="amd64" ;; \
        arm64) wgcf_arch="arm64" ;; \
        armhf) wgcf_arch="armv7" ;; \
        *) echo "Unsupported architecture for wgcf: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fL "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_${wgcf_arch}" -o /usr/local/bin/wgcf; \
    chmod +x /usr/local/bin/wgcf; \
    mkdir -p /etc/wireguard

COPY --from=wgx-builder /tmp/wgx/wgx /usr/local/bin/wgx

# Install Ookla Speedtest CLI for the admin panel speedtest
ARG SPEEDTEST_VERSION=1.2.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) speedtest_arch="x86_64" ;; \
        arm64) speedtest_arch="aarch64" ;; \
        *) echo "Unsupported architecture for speedtest: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://install.speedtest.net/app/cli/ookla-speedtest-${SPEEDTEST_VERSION}-linux-${speedtest_arch}.tgz" -o /tmp/speedtest.tgz; \
    tar -xzf /tmp/speedtest.tgz -C /usr/local/bin speedtest; \
    chmod +x /usr/local/bin/speedtest; \
    rm -f /tmp/speedtest.tgz

# 2. EasyProxy Dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 3. Environment Settings
ENV PYTHONPATH=/app

# Copia esplicita
COPY . .

RUN chmod +x entrypoint.sh scripts/warp_userspace_ctl.sh

# 5. Metadata & Ports
LABEL org.opencontainers.image.title="EasyProxy Monolith"
LABEL org.opencontainers.image.description="All-in-one HLS Proxy with integrated CF Turnstile Solver"
EXPOSE 7860
VOLUME ["/data"]

# 6. Execution
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
