"""HTTPS client for the separate DUAL offset API."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import aiohttp


logger = logging.getLogger("easyproxy.dual.offsets")


class RemoteOffsetStore:
    """Use the central offset API; no MongoDB connection exists in EasyProxy."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    @staticmethod
    def key(media_key: str, resolution: int, video_fp: str, audio_fp: str) -> str:
        return hashlib.sha1(
            f"v2|{media_key}|{resolution}|{video_fp}|{audio_fp}".encode()
        ).hexdigest()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5),
                    connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
                    headers={"Accept": "application/json"},
                )
        return self._session

    async def _post(self, path: str, payload: dict) -> dict | None:
        try:
            session = await self._get_session()
            async with session.post(f"{self.base_url}{path}", json=payload) as response:
                if response.status >= 400:
                    logger.warning("Remote offset API returned HTTP %s", response.status)
                    return None
                data = await response.json()
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.warning("Remote offset API request failed", exc_info=True)
            return None

    async def lookup(self, payload: dict):
        response = await self._post("/v1/dual/offset/lookup", payload)
        result = response.get("offset") if response else None
        return result if isinstance(result, dict) else None

    async def cache_status(self, payload: dict):
        response = await self._post("/v1/dual/offset/status", payload)
        result = response.get("offset") if response else None
        return result if isinstance(result, dict) else None

    async def report(self, payload: dict, result: dict):
        await self._post(
            "/v1/dual/offset/report",
            {**payload, "offset": result},
        )

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
