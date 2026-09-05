#!/bin/bash
export PYTHONPATH=/app

WARP_LICENSE_KEY="${WARP_LICENSE_KEY:-}"
WARP_PROXY_HOST="127.0.0.1"
WARP_PROXY_PORT="1080"
WARP_DIR="/tmp/easyproxy-warp"
WARPCTL="/app/scripts/warp_userspace_ctl.sh"

start_userspace_warp() {
    echo "Starting Cloudflare WARP via wgcf + wireproxy userspace SOCKS5..."

    if ! command -v wgcf >/dev/null 2>&1 || \
       ! command -v wireproxy >/dev/null 2>&1; then
        echo "wgcf or wireproxy not found. Rebuild the image."
        return 1
    fi

    mkdir -p "$WARP_DIR"
    cd "$WARP_DIR" || return 1

    if [ ! -f wgcf-account.toml ]; then
        yes | wgcf register --accept-tos || return 1
    fi

    if [ -n "$WARP_LICENSE_KEY" ]; then
        wgcf update --license-key "$WARP_LICENSE_KEY" || true
    fi

    rm -f wgcf-profile.conf
    wgcf generate || return 1

    # WARP is deliberately IPv4-only. Do not install an IPv6 default route.
    sed -i -E '/^[[:space:]]*AllowedIPs[[:space:]]*=/ s/,[[:space:]]*::\/0//g' wgcf-profile.conf
    if grep -Eq '^[[:space:]]*AllowedIPs[[:space:]]*=.*::\/0' wgcf-profile.conf; then
        echo "Could not create IPv4-only WARP profile (IPv6 route remains)." >&2
        return 1
    fi

    install -m 600 wgcf-profile.conf /etc/wireguard/wg0.conf

    "$WARPCTL" start || return 1

    echo "Waiting for wireproxy SOCKS5 on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}..."
    for i in $(seq 1 20); do
        if ! "$WARPCTL" status >/dev/null 2>&1; then
            echo "wireproxy exited during startup."
            return 1
        fi
        if nc -z "$WARP_PROXY_HOST" "$WARP_PROXY_PORT" && "$WARPCTL" probe >/dev/null 2>&1; then
            echo "WARP userspace WireGuard + wireproxy SOCKS5 ready on ${WARP_PROXY_HOST}:${WARP_PROXY_PORT}."
            return 0
        fi
        sleep 1
    done

    echo "wireproxy SOCKS5 not detected."
    return 1
}

# EasyProxy watchdog checks wireproxy/WARP and reconnects after consecutive failures.
cleanup() {
    "$WARPCTL" stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

start_userspace_warp || echo "WARP unavailable; EasyProxy will continue without the WARP proxy."

echo "Starting EasyProxy..."
cd /app || exit 1
python app.py
