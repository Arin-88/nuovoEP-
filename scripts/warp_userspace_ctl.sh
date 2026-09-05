#!/bin/sh
set -eu

PID_FILE="/tmp/easyproxy-warp/wireproxy.pid"
CONFIG_FILE="/etc/wireguard/wg0.conf"
WIREPROXY_CONFIG="/tmp/easyproxy-warp/wireproxy.conf"
LOG_FILE="/var/log/wireproxy.log"
WIREPROXY_BIN="/usr/local/bin/wireproxy"
SOCKS_ADDR="127.0.0.1:1080"
TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"

pid_is_wireproxy() {
    pid="$1"
    [ -r "/proc/${pid}/comm" ] || return 1
    [ "$(tr -d '\n' < "/proc/${pid}/comm")" = "wireproxy" ]
}

read_pid() {
    [ -s "$PID_FILE" ] || return 1
    pid=$(tr -dc '0-9' < "$PID_FILE")
    [ -n "$pid" ] || return 1
    printf '%s\n' "$pid"
}

write_wireproxy_config() {
    cp "$CONFIG_FILE" "$WIREPROXY_CONFIG"
    printf '\n[Socks5]\nBindAddress = %s\n' "$SOCKS_ADDR" >> "$WIREPROXY_CONFIG"
    chmod 600 "$WIREPROXY_CONFIG"
}

start_wireproxy() {
    if pid=$(read_pid) && pid_is_wireproxy "$pid"; then
        echo "wireproxy already running (pid ${pid})."
        return 0
    fi

    rm -f "$PID_FILE"
    [ -x "$WIREPROXY_BIN" ] || { echo "wireproxy binary not found." >&2; return 1; }
    [ -f "$CONFIG_FILE" ] || { echo "WireGuard config not found." >&2; return 1; }
    mkdir -p "$(dirname "$WIREPROXY_CONFIG")"
    write_wireproxy_config

    if ! "$WIREPROXY_BIN" -n -c "$WIREPROXY_CONFIG" >/dev/null 2>&1; then
        echo "wireproxy config validation failed." >&2
        rm -f "$WIREPROXY_CONFIG"
        return 1
    fi

    "$WIREPROXY_BIN" -c "$WIREPROXY_CONFIG" \
        >>"$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "Started wireproxy (pid ${pid})."
}

stop_wireproxy() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        rm -f "$PID_FILE"
        rm -f "$WIREPROXY_CONFIG"
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && pid_is_wireproxy "$pid"; do
        sleep 1
        i=$((i + 1))
    done

    if pid_is_wireproxy "$pid"; then
        echo "wireproxy did not stop within 10 seconds." >&2
        return 1
    fi
    rm -f "$PID_FILE"
    rm -f "$WIREPROXY_CONFIG"
}

probe_warp() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        echo "WARP probe: wireproxy process is down." >&2
        return 1
    fi

    # --socks5 keeps DNS local. --ipv4 prevents an AAAA result from
    # bypassing the IPv4-only WARP profile or stalling wireproxy.
    trace=$(curl --ipv4 --socks5 "$SOCKS_ADDR" -fsS \
        --connect-timeout 3 --max-time 8 "$TRACE_URL" 2>&1) || {
        echo "WARP probe: SOCKS traffic failed: $trace" >&2
        return 1
    }

    printf '%s\n' "$trace"
    printf '%s\n' "$trace" | grep -Eq '^warp=(on|plus)$' || {
        echo "WARP probe: Cloudflare did not report warp=on/plus." >&2
        return 1
    }
    printf '%s\n' "$trace" | grep -Eq '^ip=[0-9]+(\.[0-9]+){3}$' || {
        echo "WARP probe: egress is not IPv4." >&2
        return 1
    }
}

case "${1:-status}" in
    start)
        start_wireproxy
        ;;
    stop)
        stop_wireproxy
        ;;
    restart)
        stop_wireproxy
        start_wireproxy
        ;;
    probe)
        probe_warp
        ;;
    status)
        pid=$(read_pid 2>/dev/null || true)
        if [ -n "$pid" ] && pid_is_wireproxy "$pid"; then
            echo "wireproxy running (pid ${pid})."
            exit 0
        fi
        exit 1
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac
