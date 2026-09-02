#!/bin/sh
set -eu

PID_FILE="/tmp/easyproxy-warp/wgx.pid"
CONFIG_FILE="/etc/wireguard/wg0.conf"
LOG_FILE="/var/log/wgx.log"
WGX_BIN="/usr/local/bin/wgx"
SOCKS_ADDR="127.0.0.1:1080"

pid_is_wgx() {
    pid="$1"
    [ -r "/proc/${pid}/comm" ] || return 1
    [ "$(tr -d '\n' < "/proc/${pid}/comm")" = "wgx" ]
}

read_pid() {
    [ -s "$PID_FILE" ] || return 1
    pid=$(tr -dc '0-9' < "$PID_FILE")
    [ -n "$pid" ] || return 1
    printf '%s\n' "$pid"
}

start_wgx() {
    if pid=$(read_pid) && pid_is_wgx "$pid"; then
        echo "wgx already running (pid ${pid})."
        return 0
    fi

    rm -f "$PID_FILE"
    [ -x "$WGX_BIN" ] || { echo "wgx binary not found." >&2; return 1; }
    [ -f "$CONFIG_FILE" ] || { echo "WireGuard config not found." >&2; return 1; }

    LOG_LEVEL=error "$WGX_BIN" --socks5 "$SOCKS_ADDR" --config "$CONFIG_FILE" \
        >>"$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "Started wgx (pid ${pid})."
}

stop_wgx() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wgx "$pid"; then
        rm -f "$PID_FILE"
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && pid_is_wgx "$pid"; do
        sleep 1
        i=$((i + 1))
    done

    if pid_is_wgx "$pid"; then
        echo "wgx did not stop within 10 seconds." >&2
        return 1
    fi
    rm -f "$PID_FILE"
}

case "${1:-status}" in
    start)
        start_wgx
        ;;
    stop)
        stop_wgx
        ;;
    restart)
        stop_wgx
        start_wgx
        ;;
    status)
        pid=$(read_pid 2>/dev/null || true)
        if [ -n "$pid" ] && pid_is_wgx "$pid"; then
            echo "wgx running (pid ${pid})."
            exit 0
        fi
        exit 1
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac
