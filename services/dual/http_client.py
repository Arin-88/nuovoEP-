"""Small aiohttp client helper shared by the in-process DUAL service."""

from __future__ import annotations

from contextlib import asynccontextmanager

import aiohttp

from config import get_connector_for_proxy


def _proxy_settings(proxy: str):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None, None

    if proxy.startswith(("socks5://", "socks5h://", "socks4://", "socks4a://")):
        # Use central connector policy: WARP closes upstream sockets per response.
        return get_connector_for_proxy(proxy), None
    return None, proxy


def create_client_session(proxy: str = "", timeout: float = 30):
    connector, request_proxy = _proxy_settings(proxy)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if connector is not None:
        kwargs["connector"] = connector
    return aiohttp.ClientSession(**kwargs), request_proxy


@asynccontextmanager
async def client_session(proxy: str = "", timeout: float = 30):
    session, request_proxy = create_client_session(proxy, timeout)
    async with session:
        yield session, request_proxy


__all__ = ["client_session", "create_client_session"]
