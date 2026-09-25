"""Local Tor client exposed as a SOCKS5 proxy."""

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import signal
import stat
import time

import aiohttp
from aiohttp_socks import ProxyConnector

import config_store

logger = logging.getLogger(__name__)

TOR_DATA_DIR = os.path.join(config_store.CONFIG_DIR, "tor")
TORRC_PATH = os.path.join(TOR_DATA_DIR, "torrc")
TOR_LOG_PATH = os.path.join(TOR_DATA_DIR, "tor.log")
TOR_CHECK_URL = "https://check.torproject.org/api/ip"
TOR_CONTROL_HOST = "127.0.0.1"
TOR_CONTROL_PORT = 9051
TOR_BOOTSTRAP_TIMEOUT = 60
TOR_MAX_CIRCUIT_DIRTINESS = "30 days"

_BIND_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.\-\[\]:]+):(?P<port>\d{1,5})$")
_process: asyncio.subprocess.Process | None = None
_lock = asyncio.Lock()


class TorError(Exception):
    """Raised for user-facing Tor errors."""


def available() -> bool:
    return bool(shutil.which("tor"))


def get_bind() -> str:
    return str(config_store.get("tor_bind", "127.0.0.1:9050") or "").strip()


def set_bind(value: str) -> str:
    bind = (value or "").strip()
    match = _BIND_RE.match(bind)
    if not match or not 1 <= int(match.group("port")) <= 65535:
        raise TorError(f"Invalid bind address: {value!r} (expected host:port)")
    host = match.group("host").strip("[]").lower()
    if host != "localhost":
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise TorError("TorProxy bind must use loopback (127.0.0.1, ::1 or localhost)")
    config_store.set("tor_bind", bind)
    return bind


def is_enabled() -> bool:
    return bool(config_store.get("tor_enabled", False))


def set_enabled(value: bool) -> None:
    config_store.set("tor_enabled", bool(value))


def _pid() -> int | None:
    return _process.pid if _process and _process.returncode is None else None


def _split_bind(bind: str) -> tuple[str, int]:
    match = _BIND_RE.match(bind)
    if not match:
        raise TorError(f"Invalid bind address: {bind!r} (expected host:port)")
    host = match.group("host").strip("[]")
    return host, int(match.group("port"))


def _tor_identity() -> tuple[int | None, int | None]:
    """Return the Debian Tor uid/gid when the app can prepare them."""
    if os.name == "nt" or getattr(os, "geteuid", lambda: 1)() != 0:
        return None, None
    try:
        import pwd
        account = pwd.getpwnam("debian-tor")
    except (ImportError, KeyError):
        return None, None
    # Use the account's primary gid; the group name is not guaranteed to
    # match the username on every VPS image.
    return account.pw_uid, account.pw_gid


def _repair_tor_data_permissions(tor_uid: int, tor_gid: int) -> None:
    """Make the bind-mounted Tor state readable/writable by debian-tor."""
    parent_dir = os.path.dirname(os.path.abspath(TOR_DATA_DIR))
    parent_mode = stat.S_IMODE(os.stat(parent_dir).st_mode)
    # Tor only needs to traverse the /data mount; keep its contents private.
    os.chmod(parent_dir, parent_mode | stat.S_IXOTH)
    os.makedirs(TOR_DATA_DIR, exist_ok=True)
    os.chown(TOR_DATA_DIR, tor_uid, tor_gid)
    os.chmod(TOR_DATA_DIR, 0o700)
    for root, dirs, files in os.walk(TOR_DATA_DIR):
        for name in dirs:
            path = os.path.join(root, name)
            os.chown(path, tor_uid, tor_gid)
            os.chmod(path, 0o700)
        for name in files:
            path = os.path.join(root, name)
            os.chown(path, tor_uid, tor_gid)
            os.chmod(path, 0o600)


def _write_torrc() -> None:
    tor_uid, tor_gid = _tor_identity()
    run_as_debian_tor = tor_uid is not None and tor_gid is not None
    if run_as_debian_tor:
        try:
            _repair_tor_data_permissions(tor_uid, tor_gid)
        except OSError as exc:
            raise TorError(
                f"Cannot set permissions on {TOR_DATA_DIR} for debian-tor: {exc}"
            ) from exc
    else:
        os.makedirs(TOR_DATA_DIR, exist_ok=True)
    host, port = _split_bind(get_bind())
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    lines = [
        f"SocksPort {host}:{port}",
        f"DataDirectory {TOR_DATA_DIR}",
        "ClientOnly 1",
        "AvoidDiskWrites 0",
        f"MaxCircuitDirtiness {TOR_MAX_CIRCUIT_DIRTINESS}",
        f"ControlPort {TOR_CONTROL_HOST}:{TOR_CONTROL_PORT}",
        "CookieAuthentication 1",
        f"CookieAuthFile {os.path.join(TOR_DATA_DIR, 'control_auth_cookie')}",
        f"Log notice file {TOR_LOG_PATH}",
    ]
    # Debian's package user prevents Tor from running as root in Docker.
    if run_as_debian_tor:
        lines.append("User debian-tor")
    with open(TORRC_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        if run_as_debian_tor:
            os.chown(TORRC_PATH, tor_uid, tor_gid)
        os.chmod(TORRC_PATH, 0o600)
    except OSError as exc:
        raise TorError(f"Cannot set permissions on {TORRC_PATH}: {exc}") from exc


async def _port_ready(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _control_command(reader, writer, command: str) -> None:
    writer.write((command + "\r\n").encode("ascii"))
    await writer.drain()
    for _ in range(32):
        line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("utf-8", "replace").strip()
        if line.startswith("250 "):
            return
        if line.startswith("4") or line.startswith("5"):
            raise TorError(line)
    raise TorError("Unexpected Tor control response")


async def _bootstrap_progress() -> int | None:
    """Return Tor's bootstrap percentage, or None while the control port is not ready."""
    cookie_path = os.path.join(TOR_DATA_DIR, "control_auth_cookie")
    try:
        with open(cookie_path, "rb") as handle:
            cookie = handle.read()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=5
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        await _control_command(reader, writer, f"AUTHENTICATE {cookie.hex()}")
        writer.write(b"GETINFO status/bootstrap-phase\r\n")
        await writer.drain()
        for _ in range(64):
            line = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("utf-8", "replace")
            match = re.search(r"PROGRESS=(\d+)", line)
            if match:
                return int(match.group(1))
        return None
    except (OSError, asyncio.TimeoutError, TorError):
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _log_tail(lines: int = 24) -> str:
    try:
        with open(TOR_LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            content = "".join(handle.readlines()[-lines:]).strip()
    except OSError:
        return "No Tor log output."
    return content or "No Tor log output."


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if not process or process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def _process_output(process: asyncio.subprocess.Process) -> str:
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return ""
    chunks = []
    for payload in (stderr, stdout):
        if payload:
            text = payload.decode("utf-8", "replace").strip()
            if text:
                chunks.append(text)
    return " | ".join(chunks)


async def _verify_config() -> None:
    process = await asyncio.create_subprocess_exec(
        "tor", "--verify-config", "-f", TORRC_PATH,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise TorError("Tor configuration verification timed out")
    output = " ".join(
        part.decode("utf-8", "replace").strip()
        for part in (stderr, stdout)
        if part and part.decode("utf-8", "replace").strip()
    )
    if process.returncode != 0:
        raise TorError(f"Invalid Tor configuration (code {process.returncode}): {output or 'no output'}")


async def new_identity() -> None:
    """Ask Tor for a new circuit while keeping automatic rotation disabled."""
    if _pid() is None:
        raise TorError("Tor is not running")
    cookie_path = os.path.join(TOR_DATA_DIR, "control_auth_cookie")
    try:
        with open(cookie_path, "rb") as handle:
            cookie = handle.read()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT), timeout=5
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise TorError("Tor control port is unavailable") from exc
    try:
        await _control_command(reader, writer, f"AUTHENTICATE {cookie.hex()}")
        await _control_command(reader, writer, "SIGNAL NEWNYM")
    finally:
        writer.close()
        await writer.wait_closed()


async def start() -> None:
    global _process
    async with _lock:
        if _process is not None:
            if _process.returncode is None:
                return
            _process = None
        if not available():
            raise TorError("Tor is not installed; use the EasyProxy Docker image")
        set_bind(get_bind())
        _write_torrc()
        await _verify_config()
        process = await asyncio.create_subprocess_exec(
            "tor", "-f", TORRC_PATH, "--RunAsDaemon", "0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _process = process
        host, port = _split_bind(get_bind())
        deadline = time.monotonic() + TOR_BOOTSTRAP_TIMEOUT
        ready = False
        try:
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    output = await _process_output(process)
                    detail = " | ".join(part for part in (output, _log_tail()) if part and part != "No Tor log output.")
                    raise TorError(
                        f"Tor exited during startup (code {process.returncode}): {detail or 'no Tor output'}"
                    )
                if await _port_ready(host, port) and await _bootstrap_progress() == 100:
                    logger.info("Tor SOCKS5 ready and bootstrapped on %s:%s", host, port)
                    ready = True
                    return
                await asyncio.sleep(1)
            raise TorError(f"Tor did not bootstrap in time: {_log_tail()}")
        finally:
            if not ready:
                if _process is process:
                    _process = None
                await _terminate(process)
            elif _process is process and process.returncode is not None:
                _process = None


async def stop() -> None:
    global _process
    async with _lock:
        process = _process
        _process = None
        await _terminate(process)


async def restart() -> None:
    await stop()
    await start()


async def check() -> dict:
    result = {"ok": False, "egress_ip": "", "is_tor": False, "http_ms": None, "error": ""}
    if _pid() is None:
        result["error"] = "Tor is not running"
        return result
    connector = ProxyConnector.from_url(f"socks5://{get_bind()}", rdns=True)
    started = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(TOR_CHECK_URL) as response:
                payload = await response.json(content_type=None)
        result["http_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["egress_ip"] = str(payload.get("IP", ""))
        result["is_tor"] = bool(payload.get("IsTor"))
        result["ok"] = result["is_tor"] and bool(result["egress_ip"])
        if not result["ok"]:
            result["error"] = "The connection did not reach the Tor network"
    except Exception as exc:  # noqa: BLE001 - surfaced in admin panel
        result["error"] = str(exc)
    return result


async def logs(lines: int = 120) -> str:
    return _log_tail(lines)


async def status(with_probe: bool = False) -> dict:
    data = {
        "running": _pid() is not None,
        "pid": _pid(),
        "bind": get_bind(),
        "enabled": is_enabled(),
        "available": available(),
        "automatic_rotation": False,
        "probe_ip": "",
    }
    if with_probe and data["running"]:
        result = await check()
        data["probe_ip"] = result.get("egress_ip", "")
    return data


async def ensure_running() -> None:
    if available() and is_enabled() and _pid() is None:
        try:
            await start()
        except TorError as exc:
            logger.warning("Tor could not be started: %s", exc)


async def keepalive_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await ensure_running()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never kill the loop
            logger.exception("Tor keepalive failed")
        await asyncio.sleep(interval)


__all__ = [
    "TorError", "available", "get_bind", "set_bind", "is_enabled", "set_enabled",
    "start", "stop", "restart", "new_identity", "check", "logs", "status", "keepalive_loop",
]
