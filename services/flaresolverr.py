"""On-demand FlareSolverr runner used by the VixSrc extractor.

The process is deliberately not started with EasyProxy.  It is spawned only
when a Cloudflare challenge is detected and is terminated after the API call
has returned the solved page/cookies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlsplit

import aiohttp
import psutil


logger = logging.getLogger(__name__)


class FlareSolverrError(RuntimeError):
    """Raised when the local FlareSolverr process/API cannot solve a request."""


@dataclass(frozen=True)
class FlareSolverrSolution:
    response: str
    status: int
    url: str
    cookies: tuple[dict, ...]
    user_agent: str

    @property
    def cookie_header(self) -> str:
        return "; ".join(
            f"{cookie.get('name')}={cookie.get('value', '')}"
            for cookie in self.cookies
            if cookie.get("name")
        )


def cookie_header_to_list(cookie_header: str | None) -> list[dict]:
    """Convert a normal Cookie header into FlareSolverr's cookie format."""
    cookies = []
    for item in (cookie_header or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if name and separator:
            cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def proxy_payload(proxy_url: str) -> dict:
    """Return FlareSolverr's proxy object without leaking credentials in URL logs."""
    parsed = urlsplit(proxy_url)
    payload = {"url": proxy_url}
    if parsed.username and parsed.password and parsed.hostname and parsed.port:
        payload["url"] = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        payload["username"] = parsed.username
        payload["password"] = parsed.password
    return payload


class FlareSolverrManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._owns_process = False
        self._session: aiohttp.ClientSession | None = None

    @property
    def api_url(self) -> str:
        configured = os.getenv("FLARESOLVERR_API_URL", "http://127.0.0.1:8191").rstrip("/")
        return configured if configured.endswith("/v1") else configured + "/v1"

    @staticmethod
    def _int_env(name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(os.getenv(name, str(default))))
        except (TypeError, ValueError):
            return default

    def _command(self) -> list[str] | None:
        configured = os.getenv("FLARESOLVERR_COMMAND", "").strip()
        if configured:
            return shlex.split(configured, posix=os.name != "nt")

        root = Path(os.getenv("FLARESOLVERR_DIR", "/opt/flaresolverr"))
        source_script = root / "src" / "flaresolverr.py"
        if source_script.is_file():
            return [sys.executable, str(source_script)]

        binary = shutil.which("flaresolverr")
        return [binary] if binary else None

    async def _api_available(self) -> bool:
        try:
            # Health checks must not install their 2s timeout on the session
            # used later for the actual browser-solving request.
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            ) as session:
                async with session.get(self.api_url, allow_redirects=False) as response:
                    # GET /v1 commonly returns 405; that still proves the API is up.
                    return response.status < 500
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return

        command = self._command()
        if not command:
            raise FlareSolverrError(
                "FlareSolverr non installato: configura FLARESOLVERR_COMMAND "
                "oppure ricostruisci l'immagine EasyProxy."
            )

        parsed_api = urlparse(self.api_url)
        api_host = parsed_api.hostname or "127.0.0.1"
        api_port = parsed_api.port or 8191
        env = os.environ.copy()
        env.update(
            {
                "HOST": "127.0.0.1",
                "PORT": str(api_port),
                "HEADLESS": "true",
                "LOG_LEVEL": os.getenv("FLARESOLVERR_LOG_LEVEL", "error"),
                "LOG_HTML": "false",
                "DISABLE_MEDIA": "true",
                "PROMETHEUS_ENABLED": "false",
            }
        )

        # An explicitly configured API may already be managed outside this
        # process.  The default local API is spawned below when needed.
        if api_host not in {"127.0.0.1", "localhost", "::1"}:
            if await self._api_available():
                return
            raise FlareSolverrError(f"FlareSolverr API non raggiungibile: {self.api_url}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=os.getenv("FLARESOLVERR_DIR", "/opt/flaresolverr"),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            self._owns_process = True
        except (OSError, ValueError) as exc:
            raise FlareSolverrError(f"Avvio FlareSolverr fallito: {exc}") from exc

        deadline = asyncio.get_running_loop().time() + self._int_env(
            "FLARESOLVERR_START_TIMEOUT", 30
        )
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                raise FlareSolverrError(
                    f"FlareSolverr terminato subito (exit={self._process.returncode})"
                )
            if await self._api_available():
                logger.info("FlareSolverr avviato on-demand su 127.0.0.1:%s", api_port)
                return
            await asyncio.sleep(0.25)

        raise FlareSolverrError("Timeout avvio API FlareSolverr")

    async def _stop(self) -> None:
        process = self._process
        self._process = None
        owns_process = self._owns_process
        self._owns_process = False

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

        if process and owns_process and process.returncode is None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                if process.returncode is None:
                    try:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        await process.wait()
                    except (ProcessLookupError, OSError):
                        pass

        # FlareSolverr can leave Chromium outside its process group.  Only
        # target its temporary browser profiles, never a user's normal browser.
        browser_processes = []
        for candidate in psutil.process_iter(["name", "cmdline"]):
            try:
                command = " ".join(candidate.info.get("cmdline") or [])
                lowered = command.lower()
                name = (candidate.info.get("name") or "").lower()
                if candidate.pid == os.getpid():
                    continue
                if "--user-data-dir=/tmp/" not in lowered:
                    continue
                if "chromium" in name or "chrome" in name or "chromedriver" in name:
                    browser_processes.append(candidate)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        for browser_process in browser_processes:
            try:
                browser_process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if browser_processes:
            _, alive = psutil.wait_procs(browser_processes, timeout=2)
            for browser_process in alive:
                try:
                    browser_process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            psutil.wait_procs(alive, timeout=2)

        if process and owns_process:
            logger.info("FlareSolverr terminato dopo il recupero dei cookie")

    async def solve(
        self,
        url: str,
        proxy_url: str | None,
        cookie_header: str | None = None,
        allow_direct: bool = False,
    ) -> FlareSolverrSolution:
        """Solve one challenge and stop the owned process before returning."""
        if not proxy_url and not allow_direct:
            raise FlareSolverrError(
                "Route FlareSolverr assente: direct fallback disabilitato mentre WARP è attivo"
            )

        async with self._lock:
            try:
                await self._start()
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(
                            total=self._int_env("FLARESOLVERR_REQUEST_TIMEOUT", 90)
                        ),
                        headers={"Content-Type": "application/json"},
                    )

                payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": self._int_env("FLARESOLVERR_MAX_TIMEOUT_MS", 60000),
                    "returnOnlyCookies": False,
                    "disableMedia": True,
                }
                cookies = cookie_header_to_list(cookie_header)
                if cookies:
                    payload["cookies"] = cookies
                if proxy_url:
                    payload["proxy"] = proxy_payload(proxy_url)

                async with self._session.post(self.api_url, json=payload) as response:
                    response.raise_for_status()
                    result = await response.json(content_type=None)

                if result.get("status") != "ok":
                    message = result.get("message") or "risposta non valida"
                    raise FlareSolverrError(f"FlareSolverr: {message}")

                raw_solution = result.get("solution") or {}
                raw_cookies = raw_solution.get("cookies") or []
                solution = FlareSolverrSolution(
                    response=str(raw_solution.get("response") or ""),
                    status=int(raw_solution.get("status") or 200),
                    url=str(raw_solution.get("url") or url),
                    cookies=tuple(cookie for cookie in raw_cookies if isinstance(cookie, dict)),
                    user_agent=str(raw_solution.get("userAgent") or ""),
                )
                if not solution.response and not solution.cookies:
                    raise FlareSolverrError("FlareSolverr non ha restituito HTML o cookie")
                return solution
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
                detail = str(exc) or type(exc).__name__
                raise FlareSolverrError(f"Richiesta FlareSolverr fallita [{detail}]") from exc
            finally:
                # Deliberately stop immediately after the solution is copied.
                await self._stop()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop()


_MANAGER = FlareSolverrManager()


async def solve_cloudflare(
    url: str,
    proxy_url: str | None,
    cookie_header: str | None = None,
    allow_direct: bool = False,
) -> FlareSolverrSolution:
    return await _MANAGER.solve(url, proxy_url, cookie_header, allow_direct)


async def shutdown_flare_solver() -> None:
    await _MANAGER.shutdown()
