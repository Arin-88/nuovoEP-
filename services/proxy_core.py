import asyncio
import gc
import hmac
import ipaddress
import logging
import os
import re
import time
import urllib.parse
import aiohttp
import base64
import hashlib
import socket
import config as _config
import config_store

import services.proxy_shared as _shared
from services.proxy_shared import (
    logger,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    get_proxy_for_url,
    get_connector_for_proxy,
    get_extractor_proxies,
    mark_proxy_dead,
    BYPASSED_WARP_DOMAINS,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    is_dynamic_warp_bypass_candidate,
    prefer_default_family_for_url,
    resolve_extractor,
)
class SharedSessionWrapper:
    def __init__(self, session):
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def __setattr__(self, name, value):
        setattr(self._session, name, value)

    @property
    def closed(self) -> bool:
        return self._session.closed

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class HLSProxyCoreMixin:

    @staticmethod
    def _pow_search(hmac_hash: str, resource: str, number: str, ts: int, max_iter: int) -> int:
        """CPU-bound PoW search, intended for run_in_executor."""
        import hashlib as _hl
        for i in range(max_iter):
            combined = f"{hmac_hash}{resource}{number}{ts}{i}"
            md5_hash = _hl.md5(combined.encode("utf-8")).hexdigest()
            prefix_value = int(md5_hash[:4], 16)
            if prefix_value < 0x1000:
                return i
        return 0

    async def shorten_hls_url(self, url: str) -> str:
        """Codifica l'URL direttamente in base64 (nessuna memoria usata per mappe)."""
        if not url:
            return ""
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"u_{encoded}"

    async def start_tasks(self):
        """Starts background tasks for the proxy."""
        if any(not task.done() for task in self._background_tasks):
            return
        self._last_warp_reconnect_time = time.time()  # ponytail: startup cooldown to allow initial handshake
        self._background_tasks = {
            asyncio.create_task(self._update_latest_version()),
            asyncio.create_task(self._cleanup_stale_sessions()),
            asyncio.create_task(self._warp_keepalive()),
        }

    async def _cleanup_stale_sessions(self):
        """Periodic cleanup of stale CDN tokens and idle proxy sessions to prevent memory accumulation when idle."""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                
                # 1. Clean up stale HLS/CDN tokens (>300s)
                stale_tokens = [
                    k for k, t in getattr(self, '_renewed_cdn_token_atimes', {}).items()
                    if now - t > 300
                ]
                for k in stale_tokens:
                    self._renewed_cdn_tokens.pop(k, None)
                    self._renewed_cdn_token_atimes.pop(k, None)
                    logger.debug("🧹 Cleaned stale CDN token: %s", k[:8])

                # 2. Clean up idle proxy sessions (>60s)
                if hasattr(self, "_proxy_sessions") and hasattr(self, "_proxy_session_atimes"):
                    _warp_url = _shared.WARP_PROXY_URL
                    stale_proxies = [
                        p for p, t in list(self._proxy_session_atimes.items())
                        if now - t > 60 and p != _warp_url
                    ]
                    for p in stale_proxies:
                        p_sess = self._proxy_sessions.pop(p, None)
                        self._proxy_session_atimes.pop(p, None)
                        if p_sess and not p_sess.closed:
                            await p_sess.close()
                            logger.info(f"[NET] Closed idle proxy session: {p}")

                # 3. Close shared session if idle >30s
                _session_atime = getattr(self, "_session_atime", 0)
                if _session_atime and now - _session_atime > 30:
                    if self.session and not self.session.closed:
                        await self.session.close()
                        logger.info("[NET] Closed idle shared session (idle %.0fs)", now - _session_atime)
                    self.session = None
                    if self.flex_session and not self.flex_session.closed:
                        await self.flex_session.close()
                        logger.info("[NET] Closed idle flex session (idle %.0fs)", now - _session_atime)
                    self.flex_session = None

                # 4. Compact Windows heap to release freed pages
                await self._compact_heap()

                # 5. WireProxy is a Go process: closed sockets can leave heap
                # pages in its RSS. Recycle only after real WARP inactivity;
                # health probes do not count as playback activity.
                await self._recycle_idle_warp(time.monotonic())

            except Exception as e:
                logger.error("Cleanup stale sessions error: %s", e)
                await asyncio.sleep(10)

    async def _compact_heap(self):
        """Release freed heap pages back to the OS (Linux: malloc_trim, Windows: HeapCompact)."""
        try:
            gc.collect()
            import ctypes
            import platform
            if platform.system() == "Windows":
                ctypes.windll.kernel32.HeapCompact(
                    ctypes.windll.kernel32.GetProcessHeap(), 0
                )
            else:
                try:
                    libc = ctypes.CDLL("libc.so.6")
                    libc.malloc_trim(0)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _warp_established_connections() -> int | None:
        """Count clients currently connected to WireProxy's SOCKS listener."""
        paths = ("/proc/net/tcp", "/proc/net/tcp6")
        if not any(os.path.exists(path) for path in paths):
            return None
        count = 0
        try:
            for path in paths:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="ascii") as source:
                    for line in source:
                        fields = line.split()
                        if len(fields) > 3 and fields[1].rsplit(":", 1)[-1].upper() == "0438" and fields[3] == "01":
                            count += 1
        except (OSError, UnicodeError):
            return None
        return count

    async def _recycle_idle_warp(self, now: float) -> None:
        """Restart WireProxy after idle to return retained Go heap to the OS."""
        if not _shared.ENABLE_WARP:
            return
        last_activity = getattr(_config, "WARP_LAST_ACTIVITY", 0.0)
        if not last_activity or now - last_activity < 120:
            return
        if self._warp_established_connections() != 0:
            return
        lock = getattr(self, "_warp_idle_recycle_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._warp_idle_recycle_lock = lock
        async with lock:
            last_activity = getattr(_config, "WARP_LAST_ACTIVITY", 0.0)
            if not last_activity or time.monotonic() - last_activity < 120:
                return
            if self._warp_established_connections() != 0:
                return
            warp_url = _shared.WARP_PROXY_URL
            await self._invalidate_proxy_session(warp_url)
            try:
                rc = await self._run_warp_control("restart")
            except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("[NET] Idle WireProxy recycle failed: %s", exc)
                return
            if rc == 0:
                if await self._wait_for_warp_socket():
                    _config.WARP_LAST_ACTIVITY = 0.0
                    logger.info("[NET] Recycled idle WireProxy after 120s; WARP sockets/RSS reset")
                else:
                    logger.warning("[NET] WireProxy restarted but SOCKS port 1080 did not recover")
            else:
                logger.warning("[NET] Idle WireProxy recycle returned code %s", rc)

    @staticmethod
    async def _wait_for_warp_socket(timeout: float = 20.0) -> bool:
        """Wait until the restarted WireProxy SOCKS listener accepts clients."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 1080), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.25)
        return False


    async def _wireproxy_process_state(self) -> tuple[str, str]:
        """Return wireproxy process state without confusing it with tunnel state."""
        control_script = "/app/scripts/warp_userspace_ctl.sh"
        if not os.path.exists(control_script):
            return "unknown", "control script unavailable"
        try:
            proc = await asyncio.create_subprocess_exec(
                control_script,
                "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
            detail = (stdout or stderr).decode("utf-8", "replace").strip()
            if proc.returncode == 0:
                return "up", detail or "status=running"
            return "down", detail or f"status exit={proc.returncode}"
        except asyncio.TimeoutError:
            return "unknown", "status check timeout"
        except Exception as exc:
            return "unknown", f"status check error={type(exc).__name__}"

    async def _probe_warp(self, timeout_sec: float = 8.0) -> tuple[bool, str]:
        """Check wireproxy process, SOCKS listener, and real WARP HTTP path."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        _WARP_PROXY_URL = _shared.WARP_PROXY_URL
        if not _ENABLE_WARP or not _WARP_PROXY_URL:
            self._warp_ip = ""
            return False, "WARP disabled or proxy URL missing"

        self._warp_ip = ""

        process_state, process_detail = await self._wireproxy_process_state()
        socket_error = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 1080),
                timeout=min(3.0, timeout_sec),
            )
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            socket_error = type(exc).__name__

        if socket_error:
            component = "wireproxy_process" if process_state == "down" else "wireproxy_socket"
            return False, (
                f"component={component} process={process_state}({process_detail}) "
                f"socks=down error={socket_error}"
            )

        try:
            connector = get_connector_for_proxy(
                _WARP_PROXY_URL, limit=0, family=socket.AF_INET, health_check=True
            )
            timeout = ClientTimeout(total=timeout_sec)
            async with ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get("https://www.cloudflare.com/cdn-cgi/trace") as resp:
                    body = await resp.text()

            trace = {
                line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
                for line in body.splitlines()
                if "=" in line
            }
            warp_state = trace.get("warp", "").lower()
            if resp.status != 200:
                return False, (
                    f"component=warp_tunnel process={process_state} socks=up "
                    f"http_status={resp.status}"
                )
            if warp_state not in {"on", "plus"}:
                return False, (
                    f"component=warp_tunnel process={process_state} socks=up "
                    f"warp={warp_state or 'unknown'}"
                )

            warp_ip = trace.get("ip", "")
            try:
                ip_version = ipaddress.ip_address(warp_ip).version
            except ValueError:
                ip_version = None
            if ip_version != 4:
                return False, (
                    f"component=warp_tunnel process={process_state} socks=up "
                    f"warp={warp_state} egress=ipv6-or-unknown ip={warp_ip or 'unknown'}"
                )

            self._warp_ip = warp_ip
            return True, (
                f"process={process_state} socks=up warp={warp_state} "
                f"ip={self._warp_ip or 'unknown'}"
            )
        except Exception as exc:
            return False, (
                f"component=warp_tunnel process={process_state} socks=up "
                f"error={type(exc).__name__}"
            )

    async def _warp_keepalive(self):
        """Probe WARP periodically and reconnect after consecutive failures."""
        consecutive_failures = 0
        while True:
            try:
                await asyncio.sleep(30)
                if not _shared.ENABLE_WARP or not _shared.WARP_PROXY_URL:
                    consecutive_failures = 0
                    continue

                healthy, reason = await self._probe_warp(timeout_sec=8)
                if healthy:
                    if consecutive_failures:
                        logger.warning(
                            "WARP health recovered after %s failed probe(s)",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    continue

                consecutive_failures += 1
                logger.warning(
                    "WARP health check failed (%s/2): %s",
                    consecutive_failures,
                    reason,
                )
                if consecutive_failures < 2:
                    continue

                result = await self.reconnect_warp()
                message = result.get("message", "")
                if result.get("status") == "ok" and message == "WARP userspace tunnel reconnected":
                    logger.warning("WARP auto-reconnect succeeded")
                    consecutive_failures = 0
                else:
                    logger.warning("WARP auto-reconnect skipped/failed: %s", message or result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WARP keepalive error: %s", e)
                await asyncio.sleep(10)

    async def is_warp_healthy(self, timeout_sec: float = 3.0) -> bool:
        """Fast check if WARP proxy socket and HTTP connectivity are working."""
        healthy, _reason = await self._probe_warp(timeout_sec=timeout_sec)
        return healthy

    async def get_warp_status(self) -> str:
        """Returns WARP status and fetches real external IP through WARP proxy."""
        healthy, _reason = await self._probe_warp(timeout_sec=10)
        return "Connected" if healthy else "Disconnected"

    async def _run_warp_control(self, action: str) -> int:
        """Run the explicit userspace WARP control action."""
        proc = await asyncio.create_subprocess_exec(
            "/app/scripts/warp_userspace_ctl.sh",
            action,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=20)

    async def reconnect_warp(self) -> dict:
        """Restart wireproxy and verify the WARP path after reconnect."""
        if not hasattr(self, "_warp_reconnect_lock"):
            self._warp_reconnect_lock = asyncio.Lock()

        now = time.time()
        last_reconnect = getattr(self, "_last_warp_reconnect_time", 0)
        if now - last_reconnect < 60:
            return {"status": "ok", "message": "Cooldown active"}
        if self._warp_reconnect_lock.locked():
            return {"status": "ok", "message": "Reconnect already in progress"}

        async with self._warp_reconnect_lock:
            self._last_warp_reconnect_time = time.time()
            try:
                rc = await self._run_warp_control("restart")
                if rc != 0:
                    return {"status": "error", "message": "wireproxy restart failed"}
                healthy, reason = await self._probe_warp(timeout_sec=8)
                if healthy:
                    return {"status": "ok", "message": "WARP userspace tunnel reconnected"}
                return {
                    "status": "error",
                    "message": f"wireproxy restart completed but WARP probe failed: {reason}",
                }
            except (FileNotFoundError, asyncio.TimeoutError) as exc:
                return {"status": "error", "message": f"WARP reconnect failed: {exc}"}

    async def _stop_warp_proxy(self):
        try:
            await self._run_warp_control("stop")
        except Exception as exc:
            logger.debug("WARP userspace process already stopped: %s", exc)

    async def _update_latest_version(self):
        """Periodically checks GitHub for the latest version in the background."""
        while True:
            try:
                await self._refresh_latest_version()
            except Exception as e:
                logger.error("Version check error: %s", e)
            await asyncio.sleep(3600)

    async def _refresh_latest_version(self):
        """Checks GitHub config.py for the latest version with cache busting.
        Can be called on-demand (e.g. on page refresh).
        Uses its own temporary session to avoid resetting the shared session idle timer.
        """
        try:
            cache_buster = int(time.time())
            url = f"https://raw.githubusercontent.com/realbestia1/EasyProxy/main/config.py?t={cache_buster}"

            connector = TCPConnector(limit=1, limit_per_host=1, keepalive_timeout=5)
            timeout = ClientTimeout(total=5)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url, timeout=2) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
                        if match:
                            new_version = match.group(1)
                            if self.latest_version != new_version:
                                self.latest_version = new_version
                                logger.info(f"🆕 Latest version updated: {self.latest_version}")
                        else:
                            if self.latest_version == "Checking...":
                                self.latest_version = "Unknown"
                    else:
                        if self.latest_version == "Checking...":
                            self.latest_version = "Error"
        except Exception as e:
            if self.latest_version == "Checking...":
                self.latest_version = "Unknown"
            logger.debug(f"Version check skipped or failed: {e}")

    @staticmethod
    def _strip_fake_png_header_from_ts(content: bytes) -> bytes:
        """
        Some providers prepend a fake PNG payload to TS segments.
        embed.st/strmd.st wraps segments as a minimal valid PNG (IHDR+IDAT+IEND,
        ~70 bytes) followed by the raw MPEG-TS stream. ExoPlayer scans for the
        TS sync byte and tolerates this; stricter players (MPV/hls.js used by
        Stremio PC) do not, and stall forever.

        We locate the first 0x47 sync byte that is followed by another 0x47 at
        +188 bytes (the TS packet size), then strip everything before it. If no
        such pattern is found we fall back to the legacy 8-byte PNG signature
        strip so we don't regress providers that only prepend 8 bytes.
        """
        if not content:
            return content

        # Fast path: not a PNG at all -> nothing to do.
        png_sig = b"\x89PNG\r\n\x1a\n"
        if not content.startswith(png_sig):
            return content

        # Generic path: scan for the first TS sync byte (0x47) that repeats at
        # +188. Bound the scan so we never iterate over a huge payload.
        scan_limit = min(4096, len(content) - 188)
        for i in range(0, scan_limit):
            if content[i] == 0x47 and content[i + 188] == 0x47:
                if i <= 8:
                    return content  # already a clean TS
                payload = content[i:]
                # Sanity-check a few more packet boundaries to avoid false positives.
                if len(payload) > 376 and payload[376] != 0x47:
                    continue
                logger.info(
                    "Removed fake PNG header from TS segment (%d -> %d bytes, header=%d)",
                    len(content), len(payload), i,
                )
                return payload

        # Legacy fallback: strip only the 8-byte PNG signature when the bytes
        # right after it look like a TS packet.
        if len(content) > 8:
            ts_payload = content[8:]
            if ts_payload and ts_payload[0] == 0x47:
                if len(ts_payload) <= 188 or ts_payload[188] == 0x47:
                    logger.info(
                        "Removed fake PNG header from TS segment (%d -> %d bytes)",
                        len(content), len(ts_payload),
                    )
                    return ts_payload

        return content

    async def _compute_key_headers(
        self, key_url: str, secret_key: str, user_agent: str = None
    ) -> tuple[int, int, str, str] | None:
        """
        Compute X-Key-Timestamp, X-Key-Nonce, X-Fingerprint, and X-Key-Path for a /key/ URL.

        Algorithm:
        1. Extract resource and number from URL pattern /key/{resource}/{number}
        2. ts = Unix timestamp in seconds
        3. hmac_hash = HMAC-SHA256(resource, secret_key).hex()
        4. nonce = proof-of-work: find i where MD5(hmac+resource+number+ts+i)[:4] < 0x1000
        5. fingerprint = SHA256(useragent + screen_resolution + timezone + language).hex()[:16]
        6. key_path = HMAC-SHA256("resource|number|ts|fingerprint", secret_key).hex()[:16]

        Args:
            key_url: The key URL containing /key/{resource}/{number}
            secret_key: The HMAC secret key
            user_agent: The user agent string for fingerprint calculation

        Returns:
            Tuple of (timestamp, nonce, fingerprint, key_path) or None if URL doesn't match pattern
        """
        # Extract resource and number from URL
        pattern = r"/key/([^/]+)/(\d+)"
        match = re.search(pattern, key_url)

        if not match:
            return None

        resource = match.group(1)
        number = match.group(2)

        ts = int(time.time())

        # Compute HMAC-SHA256
        hmac_hash = hmac.new(
            secret_key.encode("utf-8"), resource.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Proof-of-work loop (CPU-bound, run in thread pool to not block event loop)
        loop = asyncio.get_event_loop()
        nonce = await loop.run_in_executor(None, HLSProxyCoreMixin._pow_search, hmac_hash, resource, number, ts, 50000)

        # Compute fingerprint
        fp_user_agent = (
            user_agent
            if user_agent
            else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
        fp_screen_res = "1920x1080"
        fp_timezone = "UTC"
        fp_language = "en"

        fp_string = f"{fp_user_agent}{fp_screen_res}{fp_timezone}{fp_language}"

        fingerprint = hashlib.sha256(fp_string.encode("utf-8")).hexdigest()[:16]

        # Compute key-path
        key_path_string = f"{resource}|{number}|{ts}|{fingerprint}"
        key_path = hmac.new(
            secret_key.encode("utf-8"), key_path_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

        return ts, nonce, fingerprint, key_path

    async def _get_session(self, prefer_default_family: bool = False, url: str = None):
        if url:
            await self._check_dynamic_warp_bypass(url)
        target_attr = "flex_session" if prefer_default_family else "session"
        session = getattr(self, target_attr)
        if session is None or session.closed:
            connector_kwargs = {
                "limit": 0,
                "limit_per_host": 0,
                "keepalive_timeout": 15,
                "enable_cleanup_closed": True,
                "use_dns_cache": True,
            }
            if not prefer_default_family:
                connector_kwargs["family"] = socket.AF_INET

            connector = TCPConnector(**connector_kwargs)
            session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30),
                connector=connector,
            )
            setattr(self, target_attr, session)
        self._session_atime = time.time()
        return session

    async def _check_dynamic_warp_bypass(self, url: str):
        """Dynamically adds domain to WARP bypass if it matches known patterns."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        if not _ENABLE_WARP:
            return

        try:
            from urllib.parse import urlsplit
            domain = urlsplit(url).netloc
            if not domain: return

            # Sanitize domain: only allow valid hostname characters
            if not re.match(r'^[a-zA-Z0-9.\-*]+$', domain):
                return

            if is_dynamic_warp_bypass_candidate(domain):
                if domain not in BYPASSED_WARP_DOMAINS:
                    base_domain = ".".join(domain.split(".")[-2:])
                    logging.info(f"⚠️ [Dynamic Bypass] Adding {base_domain} (and {domain}) to the EasyProxy WARP exclusion list...")

                    _WARP_EXCLUDE_DOMAINS = _shared.WARP_EXCLUDE_DOMAINS
                    if isinstance(_WARP_EXCLUDE_DOMAINS, list):
                        if base_domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(base_domain)
                        if domain not in _WARP_EXCLUDE_DOMAINS:
                            _WARP_EXCLUDE_DOMAINS.append(domain)

                    BYPASSED_WARP_DOMAINS.add(domain)
                    BYPASSED_WARP_DOMAINS.add(base_domain)
        except Exception as e:
            logging.error(f"❌ Error in dynamic WARP bypass: {e}")

    async def _get_proxy_session(self, url: str, bypass_warp: bool = False, forced_proxy: str | None = None):
        """Create a fresh session or reuse an existing one for the given URL.

        Returns: (session, proxy_url) tuple
        - session: The aiohttp ClientSession (wrapped) to use
        - proxy_url: The proxy URL being used, or None for direct connection
        """
        await self._check_dynamic_warp_bypass(url)

        if forced_proxy:
            forced_proxy = urllib.parse.unquote(forced_proxy)
            if forced_proxy.lower() == "off":
                forced_proxy = None

        # proxy=off disables configured/forced proxies. Keep a forced proxy
        # active when only WARP is bypassed: proxies command over WARP.
        if _shared.BYPASS_PROXIES_CONTEXT.get():
            forced_proxy = None

        # Stale proxy sessions cleanup (>60s idle, aligned with connector keepalive_timeout).
        # WARP session stays pooled, but its connector never keeps upstream TCP
        # connections alive; the WireProxy process/tunnel remains available.
        if hasattr(self, "_proxy_session_atimes"):
            now = time.time()
            _warp_url = _shared.WARP_PROXY_URL
            stale = [p for p, t in self._proxy_session_atimes.items() if now - t > 60 and p != _warp_url]
            for p_url in stale:
                p_sess = self._proxy_sessions.pop(p_url, None)
                self._proxy_session_atimes.pop(p_url, None)
                if p_sess and not p_sess.closed:
                    await p_sess.close()

        proxy = forced_proxy or get_proxy_for_url(url, bypass_warp=bypass_warp)
        if not proxy and not _config.is_direct_connection_allowed(bypass_warp):
            raise aiohttp.ClientConnectionError(
                "No proxy route available; direct fallback disabled"
            )
        if proxy == _shared.WARP_PROXY_URL:
            # Refresh on every real request, not only when the pooled session
            # is created; this prevents recycling during an active HLS stream.
            _config.WARP_LAST_ACTIVITY = time.monotonic()

        prefer_default_family = prefer_default_family_for_url(url)

        if proxy:
            if not hasattr(self, "_proxy_sessions"):
                self._proxy_sessions = {}
                self._proxy_session_atimes = {}

            # Evict oldest session if cache gets too large (e.g. >= 10) to prevent memory leak
            if len(self._proxy_sessions) >= 10 and proxy not in self._proxy_sessions:
                try:
                    _warp_url = _shared.WARP_PROXY_URL
                    candidates = [p for p in self._proxy_sessions if p != _warp_url]
                    if candidates:
                        oldest_proxy = min(candidates, key=lambda p: self._proxy_session_atimes.get(p, 0))
                        oldest_sess = self._proxy_sessions.pop(oldest_proxy, None)
                        self._proxy_session_atimes.pop(oldest_proxy, None)
                        if oldest_sess and not oldest_sess.closed:
                            await oldest_sess.close()
                            logger.info(f"[NET] Evicted oldest proxy session: {oldest_proxy}")
                except Exception as e:
                    logger.warning(f"Failed to evict proxy session: {e}")

            if proxy not in self._proxy_sessions or self._proxy_sessions[proxy].closed:
                logger.info(f"[NET] Creating pooled proxy session: {proxy}")
                try:
                    connector_kwargs = {
                        "limit": 0,
                        "limit_per_host": 0,
                        "family": socket.AF_INET,
                    }
                    if _shared.WARP_PROXY_URL and proxy == _shared.WARP_PROXY_URL:
                        # Do not retain idle CDN sockets inside WireProxy after
                        # a playlist/segment response completes. limit=0 stays:
                        # this changes lifetime only, not concurrency.
                        connector_kwargs["force_close"] = True
                    else:
                        connector_kwargs["keepalive_timeout"] = 15
                    connector = get_connector_for_proxy(proxy, **connector_kwargs)
                    timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=30)
                    session = ClientSession(timeout=timeout, connector=connector)
                    self._proxy_sessions[proxy] = session
                except Exception as e:
                    logger.warning(f"Failed to create proxy connector: {e}")
                    raise
            else:
                session = self._proxy_sessions[proxy]

            self._proxy_session_atimes[proxy] = time.time()
            return SharedSessionWrapper(session), proxy

        session = await self._get_session(prefer_default_family=prefer_default_family)
        return session, None

    async def _invalidate_proxy_session(self, proxy_url: str | None) -> bool:
        """Drop one pooled proxy session so the next request gets a new connector."""
        if not proxy_url or not hasattr(self, "_proxy_sessions"):
            return False
        session = self._proxy_sessions.pop(proxy_url, None)
        if hasattr(self, "_proxy_session_atimes"):
            self._proxy_session_atimes.pop(proxy_url, None)
        if not session:
            return False
        if not session.closed:
            await session.close()
        logger.warning("[NET] Invalidated pooled proxy session: %s", proxy_url)
        return True

    async def _retry_special_cdn_request(self, request_target, headers, disable_ssl: bool):
        """Retry a provider-protected CDN once via an alternate aiohttp route."""
        _ENABLE_WARP = _shared.ENABLE_WARP
        _WARP_PROXY_URL = _shared.WARP_PROXY_URL
        _GLOBAL_PROXIES = _shared.GLOBAL_PROXIES
        retry_proxy = None
        if _ENABLE_WARP and _WARP_PROXY_URL and "127.0.0.1" not in _WARP_PROXY_URL:
            retry_proxy = _WARP_PROXY_URL
        elif _ENABLE_WARP and _WARP_PROXY_URL:
            from config import is_proxy_alive_async
            if await is_proxy_alive_async(_WARP_PROXY_URL):
                retry_proxy = _WARP_PROXY_URL
        elif _GLOBAL_PROXIES:
            retry_proxy = _GLOBAL_PROXIES[0]

        if not retry_proxy:
            return None

        try:
            connector = get_connector_for_proxy(
                retry_proxy,
                limit=0,
                limit_per_host=0,
                keepalive_timeout=15,
                family=socket.AF_INET,
                rdns=True,
            )
            timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
            async with ClientSession(timeout=timeout, connector=connector) as retry_session:
                async with retry_session.get(
                    request_target,
                    headers=headers,
                    ssl=not disable_ssl,
                ) as retry_resp:
                    if retry_resp.status not in [200, 206]:
                        return None
                    return {
                        "status": retry_resp.status,
                        "headers": dict(retry_resp.headers),
                        "body": await retry_resp.read(),
                        "proxy": retry_proxy,
                    }
        except Exception as e:
            logger.warning("Provider CDN retry via alternate route failed: %r", e)
            return None

    @staticmethod
    def _query_flag_is_true(value: str | None) -> bool:
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _should_force_direct_from_query(self, request) -> bool:
        direct_param = request.query.get("direct")
        if self._query_flag_is_true(direct_param):
            return True

        for param_name, param_value in request.query.items():
            if not param_name.startswith("h_"):
                continue
            header_name = param_name[2:].replace("_", "-").lower()
            if header_name in {"x-direct-connection", "x-force-direct"}:
                return self._query_flag_is_true(param_value)

        return False

    async def get_extractor(self, url: str, request_headers: dict, host: str = None, bypass_warp: bool = False):
        """Ottiene l'estrattore appropriato per l'URL."""
        result = await resolve_extractor(
            self,
            url,
            request_headers,
            host=host,
            bypass_warp=bypass_warp,
        )
        if result:
            key = getattr(result, '_cache_key', None) or id(result)
            for ek, ev in self.extractors.items():
                if ev is result:
                    self._extractor_atimes[ek] = time.time()
                    break
        return result

    def _extractor_key_for_instance(self, extractor) -> str | None:
        for key, cached_extractor in self.extractors.items():
            if cached_extractor is extractor:
                return key
        return None

    @staticmethod
    def _stream_key_for_url(url: str | None) -> str | None:
        if not url:
            return None
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def _touch_extractor_activity(self, extractor_key: str | None = None, stream_key: str | None = None):
        now = time.time()
        if extractor_key and extractor_key in self.extractors:
            self._extractor_atimes[extractor_key] = now
            if stream_key:
                self._extractor_stream_atimes[(extractor_key, stream_key)] = now
            return
        for key in self.extractors:
            self._extractor_atimes[key] = now
            if stream_key:
                self._extractor_stream_atimes[(key, stream_key)] = now

    def _mark_proxy_dead_if_allowed(self, proxy_url: str | None, dead_duration: int = 300, extractor_key: str | None = None):
        if not proxy_url:
            return
        normalized_key = (extractor_key or "").replace("_direct", "").replace("_noproxy", "")
        extractor_proxies = get_extractor_proxies(normalized_key)
        if len(extractor_proxies) == 1 and urllib.parse.unquote(proxy_url) == urllib.parse.unquote(extractor_proxies[0]):
            logger.info(
                "Proxy %s failed for extractor %s, but it is the only configured extractor proxy; keeping it alive.",
                proxy_url,
                normalized_key or extractor_key,
            )
            return
        mark_proxy_dead(proxy_url, dead_duration=dead_duration)

    async def _resolve_url_id(self, url_id: str) -> str | None:
        """Risolve un url_id nell'URL originale (solo U_ base64 short URLs)."""
        if not url_id:
            return None
        # U_ IDs are base64-encoded URLs
        if url_id.startswith("u_"):
            try:
                encoded = url_id[2:]
                padding = 4 - len(encoded) % 4
                if padding != 4:
                    encoded += "=" * padding
                return base64.urlsafe_b64decode(encoded).decode()
            except Exception:
                return None
        return None

    async def cleanup(self):
        """Pulizia delle risorse"""
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

        try:
            if self.session and not self.session.closed:
                await self.session.close()
            if self.flex_session and not self.flex_session.closed:
                await self.flex_session.close()

            if hasattr(self, "_proxy_sessions"):
                for p_sess in list(self._proxy_sessions.values()):
                    if not p_sess.closed:
                        await p_sess.close()
                self._proxy_sessions.clear()
                if hasattr(self, "_proxy_session_atimes"):
                    self._proxy_session_atimes.clear()

            for extractor in self.extractors.values():
                if hasattr(extractor, "close"):
                    await extractor.close()
            self.extractors.clear()
            self._extractor_atimes.clear()
            self._extractor_stream_atimes.clear()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
