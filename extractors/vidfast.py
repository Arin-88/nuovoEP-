"""VidFast page -> unlocked HLS extractor.

VidFast keeps the playback URL behind its own player bundle.  The extractor
uses the site's decoder in ``scripts/vidfast_runner.mjs``; it does not open a
visible browser window.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any
from urllib.parse import urlparse

from config import get_preferred_proxy_for_url
from extractors.base import ExtractorError

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "vidfast_runner.mjs",
)
_MOVIE_RE = re.compile(r"^/movie/\d+/?$", re.IGNORECASE)
_TV_RE = re.compile(r"^/tv/\d+/\d+/\d+/?$", re.IGNORECASE)


class VidFastExtractor:
    """Resolve ``vidfast.vc`` movie/TV pages to a playable HLS URL."""

    def __init__(self, request_headers: dict | None = None, proxies: list | None = None):
        self.request_headers = request_headers or {}
        self.proxies = proxies or []
        self.extractor_name = "vidfast"
        self.mediaflow_endpoint = "hls_proxy"
        self.last_used_proxy = None

    @staticmethod
    def _node_bin() -> str | None:
        return shutil.which("node")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"vidfast.vc", "www.vidfast.vc"}:
            raise ExtractorError("VidFast: expected a vidfast.vc movie or TV URL")
        if not (_MOVIE_RE.fullmatch(parsed.path) or _TV_RE.fullmatch(parsed.path)):
            raise ExtractorError("VidFast: unsupported URL; expected /movie/<id> or /tv/<id>/<season>/<episode>")

    async def extract(self, url: str, **kwargs) -> dict[str, Any]:
        self._validate_url(url)
        node = self._node_bin()
        if not node:
            raise ExtractorError("VidFast: Node.js is required for headless extraction")
        if not os.path.exists(_RUNNER):
            raise ExtractorError(f"VidFast: runner script not found at {_RUNNER}")

        forced_proxy = kwargs.get("proxy")
        if forced_proxy:
            proxy = str(forced_proxy)
        else:
            proxy = await get_preferred_proxy_for_url(
                url, self.extractor_name, self.proxies
            )
        runner_proxy = (
            proxy
            if proxy and str(proxy).lower().startswith(("http://", "https://"))
            else None
        )
        self.last_used_proxy = runner_proxy

        env = dict(os.environ)
        # Node's native fetch supports HTTP(S) ProxyAgent when undici is
        # available.  SOCKS/WARP remains handled by Python-side media proxying.
        if runner_proxy:
            env["VIDFAST_PROXY"] = str(runner_proxy)
        else:
            env.pop("VIDFAST_PROXY", None)
        if kwargs.get("background_refresh") or kwargs.get("force_refresh"):
            env["VIDFAST_DEBUG"] = "1"

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                node,
                _RUNNER,
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except FileNotFoundError as exc:
            raise ExtractorError(f"VidFast: failed to start Node.js: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ExtractorError("VidFast: resolver timed out after 120 seconds") from exc
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        raw = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        payload = None
        for line in reversed(raw.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break

        if not payload:
            detail = stderr.decode("utf-8", errors="replace")[-800:] if stderr else raw[-800:]
            raise ExtractorError(f"VidFast: resolver returned no JSON ({detail or proc.returncode})")
        if proc.returncode != 0 or payload.get("error"):
            error = str(payload.get("error") or "resolver failed")
            raise ExtractorError(f"VidFast: {error}")

        stream_url = str(payload.get("url") or "").strip()
        if not stream_url.startswith(("http://", "https://")):
            raise ExtractorError("VidFast: resolver returned no playable URL")

        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        logger.info("VidFast: extracted %s via %s", stream_url[:100], payload.get("server", "server"))
        return {
            "destination_url": stream_url,
            "request_headers": headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self.last_used_proxy is None,
            "bypass_warp": bool(kwargs.get("bypass_warp")),
        }

    async def close(self):
        return None
