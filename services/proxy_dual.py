"""One-call DUAL synchronisation for direct links and EasyProxy extractors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from typing import Any
from urllib.parse import urljoin

import aiohttp

from config import check_password
from services.proxy_shared import (
    BYPASS_PROXIES_CONTEXT,
    BYPASS_WARP_CONTEXT,
    SELECTED_PROXY_CONTEXT,
    STRICT_PROXY_CONTEXT,
    ClientTimeout,
    get_public_base_url,
    logger,
    web,
)
from services.dual import service as dual_service
from services.dual.security import resolves_publicly, valid_public_url


class DualLinksError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_LANGUAGE_ALIASES = {
    # Values come from the HLS master LANGUAGE/NAME attributes.
    "eng": {
        "en", "eng", "english", "inglese",
        "english #2", "english 5.1 (dd+)", "english 5.1 (dd+) #2",
    },
    "ita": {
        "it", "ita", "italian", "italiano", "italian 5.1 (dd+)",
    },
    "spa": {"es", "spa", "spanish", "spagnolo", "spanish 5.1 (dd+)"},
    "fra": {
        "fr", "fra", "french", "francese", "french #2",
        "french 5.1 (dd+)", "french 5.1 (dd+) #2",
    },
    "deu": {"de", "deu", "ger", "german", "tedesco", "german 5.1 (dd+)"},
    "hin": {"hi", "hin", "hindi", "hindi 5.1 (dd+)"},
    "rus": {"ru", "rus", "russian", "russo", "russian 5.1 (dd+)"},
}
_SAFE_HEADERS = re.compile(r"^[A-Za-z0-9-]+$")
_DUAL_ERROR_DURATION = 6
_DUAL_ERROR_MESSAGES = {
    "audio": (
        "REQUESTED AUDIO TRACK NOT FOUND",
        "Try another language or source.",
    ),
    "sync": (
        "AUDIO/VIDEO SYNC UNAVAILABLE",
        "Synchronization is not possible for this source.",
    ),
}


def _dual_error_kind(error: BaseException) -> str | None:
    """Map expected DUAL failures to a playable error-video category."""
    status = int(getattr(error, "status", 0) or 0)
    message = str(getattr(error, "message", "") or error).lower()
    if status == 409 or any(
        marker in message
        for marker in ("sync", "synchron", "incompatible", "offset")
    ):
        return "sync"
    if "audio" in message and status in {0, 400, 404, 410, 422, 502}:
        return "audio"
    return None


def _dual_font_file() -> str | None:
    candidates = (
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf"),
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    return next((path for path in candidates if os.path.isfile(path)), None)


def _ffmpeg_filter_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _generate_dual_error_segment(kind: str) -> bytes:
    """Generate one short, widely compatible H.264/AAC MPEG-TS segment."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to generate the DUAL error video")

    title, subtitle = _DUAL_ERROR_MESSAGES[kind]
    font = _dual_font_file()
    font_clause = f"fontfile='{_ffmpeg_filter_escape(font)}':" if font else ""
    drawtext = ",".join([
        f"drawtext={font_clause}text='{title}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=285:box=1:boxcolor=0x111827CC:boxborderw=18",
        f"drawtext={font_clause}text='{subtitle}':fontcolor=0xCBD5E1:fontsize=27:x=(w-text_w)/2:y=375",
    ])
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=c=0x111827:s=1280x720:r=25",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", str(_DUAL_ERROR_DURATION), "-vf", drawtext,
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "25",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
        "-f", "mpegts", "pipe:1",
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg error video generation failed: {detail[-400:]}")
    return completed.stdout


def _normalise_language(value: Any) -> str:
    raw = str(value or "").strip().lower()
    for canonical, aliases in _LANGUAGE_ALIASES.items():
        if raw in aliases:
            return canonical
    return raw


def _attrs(line: str) -> dict[str, str]:
    """Parse the comma-separated HLS attribute list, preserving quoted commas."""
    result: dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)="([^"]*)"|([A-Z0-9-]+)=([^,]*)', line):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(1) else match.group(4)
        result[key] = value.strip()
    return result


def _safe_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DualLinksError(400, "headers must be an object")
    headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name)
        value = str(raw_value).strip()
        if not _SAFE_HEADERS.fullmatch(name) or not value or "\r" in value or "\n" in value:
            raise DualLinksError(400, "invalid source header")
        if len(value) > 1024:
            raise DualLinksError(400, "source header is too long")
        headers[name] = value
    return headers


def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF:" in text or "#EXT-X-MEDIA:" in text


def _playlist_durations(text: str) -> list[float]:
    durations: list[float] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#EXTINF:"):
            continue
        try:
            durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        except ValueError:
            return []
    return durations


def _same_audio_timeline(left: str, right: str) -> bool:
    left_durations = _playlist_durations(left)
    right_durations = _playlist_durations(right)
    return (
        bool(left_durations)
        and len(left_durations) == len(right_durations)
        and all(abs(a - b) <= 0.02 for a, b in zip(left_durations, right_durations))
    )


def _master_entries(text: str, base_url: str) -> tuple[list[dict], list[dict]]:
    variants: list[dict] = []
    audios: list[dict] = []
    pending_variant: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = _attrs(line.split(":", 1)[1])
            if attributes.get("TYPE", "").upper() == "AUDIO":
                uri = attributes.get("URI")
                if uri:
                    attributes["url"] = urljoin(base_url, uri)
                    audios.append(attributes)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attributes = _attrs(line.split(":", 1)[1])
            width, height = 0, 0
            resolution = attributes.get("RESOLUTION", "")
            if "x" in resolution.lower():
                try:
                    width, height = (int(item) for item in re.split("x", resolution, flags=re.I))
                except ValueError:
                    pass
            pending_variant = {
                "attributes": attributes,
                "width": width,
                "height": height,
            }
        elif pending_variant is not None and line and not line.startswith("#"):
            pending_variant["url"] = urljoin(base_url, line)
            variants.append(pending_variant)
            pending_variant = None
    return variants, audios


def _language_match(item: dict, wanted: str) -> bool:
    values = {
        str(item.get("LANGUAGE") or "").lower(),
        str(item.get("NAME") or "").lower(),
        str(item.get("GROUP-ID") or "").lower(),
    }
    aliases = _LANGUAGE_ALIASES.get(wanted, {wanted})
    return any(value in aliases or any(alias in value for alias in aliases) for value in values)


def _audio_quality(item: dict) -> tuple[int, int, int, int]:
    """Prefer the best rendition when the user selects only a language."""
    name = str(item.get("NAME") or "").lower()
    try:
        channels = int(str(item.get("CHANNELS") or "0").split(",", 1)[0])
    except ValueError:
        channels = 0
    surround = 1 if channels >= 6 or any(value in name for value in ("5.1", "7.1", "surround")) else 0
    codec = 2 if any(value in name for value in ("eac3", "dd+", "dolby", "atmos")) else 1 if "ac-3" in name else 0
    return surround, codec, channels, len(name)


class HLSProxyDualMixin:
    """Resolve video/audio sources, then run the in-process DUAL pipeline."""

    @staticmethod
    def _spec_url(spec: dict) -> str:
        value = spec.get("url") or spec.get("d")
        if not value:
            raise DualLinksError(400, "source url is required")
        value = str(value).strip()
        if not valid_public_url(value, require_https=False):
            raise DualLinksError(400, "source url must be a public http(s) URL")
        return value

    @staticmethod
    def _routing(spec: dict) -> tuple[bool, bool, str | None]:
        raw_proxy = str(spec.get("proxy") or spec.get("proxy_url") or "").strip()
        proxy_off = raw_proxy.lower() == "off" or bool(spec.get("proxy_off"))
        forced_proxy = None if proxy_off or not raw_proxy else urllib.parse.unquote(raw_proxy)
        warp_off = str(spec.get("warp") or "").lower() == "off" or bool(spec.get("warp_off"))
        return warp_off, proxy_off, forced_proxy

    async def _resolve_dual_spec(self, spec: Any) -> dict:
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict):
            raise DualLinksError(400, "source must be an object or URL string")

        target_url = self._spec_url(spec)
        headers = _safe_headers(spec.get("headers"))
        extractor_name = str(spec.get("extractor") or spec.get("host") or "").strip().lower()
        warp_off, proxy_off, forced_proxy = self._routing(spec)
        if not extractor_name:
            if not await resolves_publicly(target_url, require_https=False):
                raise DualLinksError(400, "source URL does not resolve publicly")
            return {
                "url": target_url,
                "headers": headers,
                "extractor_name": "",
                "warp_off": warp_off,
                "proxy_off": proxy_off,
                "forced_proxy": forced_proxy,
            }

        bypass_token = BYPASS_WARP_CONTEXT.set(warp_off)
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(proxy_off)
        selected_token = SELECTED_PROXY_CONTEXT.set(forced_proxy)
        strict_token = STRICT_PROXY_CONTEXT.set(bool(forced_proxy))
        extractor = None
        extractor_key = None
        try:
            extractor = await self.get_extractor(
                target_url,
                headers,
                host=extractor_name,
                bypass_warp=warp_off,
            )
            result = await extractor.extract(
                target_url,
                request_headers=headers,
                bypass_warp=warp_off,
                proxy=forced_proxy,
            )
            extractor_key = self._extractor_key_for_instance(extractor)
            base_name = (extractor_key or extractor_name).replace("_direct", "").replace("_noproxy", "")
            selected_proxy = result.get("selected_proxy")
            if not selected_proxy:
                selected_proxy = (
                    getattr(extractor, "last_used_proxy", None)
                    or getattr(extractor, "selected_proxy", None)
                    or getattr(extractor, "_session_proxy", None)
                    or getattr(extractor, "session_proxy", None)
                )
            stream_url = str(result.get("destination_url") or "").strip()
            if not stream_url or not valid_public_url(stream_url, require_https=False):
                raise DualLinksError(502, "extractor returned an invalid media URL")
            if not await resolves_publicly(stream_url, require_https=False):
                raise DualLinksError(502, "extractor media URL does not resolve publicly")
            if warp_off and selected_proxy and "127.0.0.1" in selected_proxy:
                selected_proxy = None
            return {
                "url": stream_url,
                "headers": _safe_headers(result.get("request_headers") or headers),
                "extractor_name": base_name,
                "warp_off": bool(result.get("bypass_warp", warp_off)),
                "proxy_off": proxy_off,
                "forced_proxy": selected_proxy or forced_proxy,
                "manifest": result.get("captured_manifest") or "",
            }
        except DualLinksError:
            raise
        except Exception as exc:
            raise DualLinksError(502, f"extractor failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if extractor:
                try:
                    extractor_key = self._extractor_key_for_instance(extractor) or extractor_key
                except Exception:
                    pass
                if extractor_key and extractor_key in self.extractors:
                    self.extractors.pop(extractor_key, None)
                    self._extractor_atimes.pop(extractor_key, None)
                    for key in [key for key in self._extractor_stream_atimes if key[0] == extractor_key]:
                        self._extractor_stream_atimes.pop(key, None)
                if hasattr(extractor, "close"):
                    try:
                        await extractor.close()
                    except Exception:
                        pass
            BYPASS_WARP_CONTEXT.reset(bypass_token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(selected_token)
            STRICT_PROXY_CONTEXT.reset(strict_token)

    async def _fetch_dual(self, source: dict, *, binary: bool = False) -> tuple[Any, str]:
        url = str(source["url"])
        bypass_token = BYPASS_WARP_CONTEXT.set(bool(source.get("warp_off")))
        proxy_bypass_token = BYPASS_PROXIES_CONTEXT.set(bool(source.get("proxy_off")))
        selected_token = SELECTED_PROXY_CONTEXT.set(source.get("forced_proxy"))
        strict_token = STRICT_PROXY_CONTEXT.set(bool(source.get("forced_proxy")))
        try:
            session, _ = await self._get_proxy_session(
                url,
                bypass_warp=bool(source.get("warp_off")),
                forced_proxy=source.get("forced_proxy"),
            )
            async with session.get(
                url,
                headers=source.get("headers") or {},
                allow_redirects=True,
                timeout=ClientTimeout(total=45, connect=15, sock_connect=15, sock_read=45),
            ) as response:
                if response.status >= 400:
                    raise DualLinksError(502, f"source returned HTTP {response.status}")
                final_url = str(response.url)
                if binary:
                    return await response.read(), final_url
                return await response.text(), final_url
        except DualLinksError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DualLinksError(502, f"source fetch failed: {type(exc).__name__}") from exc
        finally:
            BYPASS_WARP_CONTEXT.reset(bypass_token)
            BYPASS_PROXIES_CONTEXT.reset(proxy_bypass_token)
            SELECTED_PROXY_CONTEXT.reset(selected_token)
            STRICT_PROXY_CONTEXT.reset(strict_token)

    async def _manifest(self, source: dict) -> tuple[str, str]:
        captured = str(source.get("manifest") or "")
        if captured:
            return captured, str(source["url"])
        text, final_url = await self._fetch_dual(source)
        return str(text), final_url

    @staticmethod
    def _pick_video(text: str, base_url: str, requested: int) -> tuple[str, int, str | None]:
        variants, audios = _master_entries(text, base_url)
        if not variants:
            return base_url, requested or 1080, None
        target = requested or max(item["height"] for item in variants)
        exact = [item for item in variants if item["height"] == target]
        candidates = exact or sorted(
            variants,
            key=lambda item: (abs((item["height"] or target) - target), -(item["height"] or 0)),
        )
        selected = candidates[0]
        group = selected["attributes"].get("AUDIO", "")
        reference = None
        reference_candidates = [item for item in audios if not group or item.get("GROUP-ID") == group]
        if reference_candidates:
            english = [item for item in reference_candidates if _language_match(item, "eng")]
            reference = (english or reference_candidates)[0].get("url")
        return selected["url"], selected["height"] or target or 1080, reference

    @staticmethod
    def _pick_audio(
        text: str,
        base_url: str,
        language: str,
        requested_alias: str = "",
        bypass_language: bool = False,
    ) -> tuple[str, dict]:
        if not _is_master(text):
            return base_url, {"manifest": text, "base_url": base_url}
        variants, audios = _master_entries(text, base_url)
        requested_alias = str(requested_alias or "").strip().lower()
        exact_alias = [
            item for item in audios
            if str(item.get("NAME") or "").strip().lower() == requested_alias
        ] if requested_alias else []
        matches = exact_alias or [item for item in audios if _language_match(item, language)]
        if not matches:
            if bypass_language and not audios and variants:
                def variant_quality(item: dict) -> tuple[int, int]:
                    try:
                        bandwidth = int(item.get("attributes", {}).get("BANDWIDTH") or 0)
                    except (TypeError, ValueError):
                        bandwidth = 0
                    return item.get("height") or 0, bandwidth

                selected = max(variants, key=variant_quality)
                logger.warning(
                    "[DUAL] no separate audio renditions; bypass enabled, using muxed variant %s",
                    selected.get("url") or "",
                )
                return selected["url"], {
                    "language": language,
                    "name": "Muxed audio",
                    "hls_language": language,
                    "language_bypassed": True,
                    "muxed_audio": True,
                }
            if bypass_language and audios:
                fallback_pool = [
                    item for item in audios
                    if str(item.get("DEFAULT") or "").strip().upper() == "YES"
                ] or audios
                selected = max(
                    fallback_pool,
                    key=lambda item: (
                        str(item.get("DEFAULT") or "").strip().upper() == "YES",
                        *_audio_quality(item),
                    ),
                )
                logger.warning(
                    "[DUAL] audio language '%s' not matched; bypass enabled, using '%s'",
                    language,
                    selected.get("NAME") or selected.get("URI") or "default track",
                )
                return selected["url"], {
                    "language": language,
                    "name": selected.get("NAME") or "",
                    "hls_language": selected.get("LANGUAGE") or language,
                    "language_bypassed": True,
                }
            available = sorted({item.get("LANGUAGE") or item.get("NAME") or "unknown" for item in audios})
            suffix = ", ".join(available[:12]) or "none"
            raise DualLinksError(400, f"audio language '{language}' not found; available: {suffix}")
        selected = matches[0] if exact_alias else max(matches, key=_audio_quality)
        return selected["url"], {
            "language": language,
            "name": selected.get("NAME") or "",
            "hls_language": selected.get("LANGUAGE") or "",
        }

    @staticmethod
    def _audio_key(playlist: str) -> str:
        for raw in playlist.splitlines():
            line = raw.strip()
            if not line.startswith("#EXT-X-KEY:"):
                continue
            attrs = _attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "").upper()
            if method == "NONE":
                return ""
            if method != "AES-128" or not attrs.get("URI"):
                raise DualLinksError(400, "selected audio uses unsupported encryption")
            return attrs["URI"]
        return ""

    @staticmethod
    def _fingerprint(url: str, headers: dict) -> str:
        selected = "|".join(f"{key}:{headers[key]}" for key in sorted(headers))
        return hashlib.sha1(f"{url}|{selected}".encode()).hexdigest()[:20]

    async def _prepare_dual_audio(
        self,
        request,
        source: dict,
        playlist: str,
        playlist_base: str,
        token: str,
        media_key: str,
        language: str,
    ) -> dict:
        key_uri = self._audio_key(playlist)
        key_bytes = b""
        if key_uri:
            key_source = dict(source)
            key_source["url"] = urljoin(playlist_base, key_uri)
            key_bytes, _ = await self._fetch_dual(key_source, binary=True)
            if len(key_bytes) != 16:
                raise DualLinksError(400, "audio AES key must be 16 bytes")
        return await self._dual_json(
            request,
            "POST",
            "/dual/aprep",
            {
                "token": token,
                "playlist": playlist,
                "key": base64.b64encode(key_bytes).decode(),
                "mediaKey": media_key,
                "lang": language,
                "baseUrl": playlist_base,
                "headers": source.get("headers") or {},
                "warp_off": bool(source.get("warp_off")),
                "proxy_off": bool(source.get("proxy_off")),
                "proxy_url": source.get("forced_proxy") or "",
                "extractor_name": source.get("extractor_name") or "",
            },
        )

    async def _dual_json(self, request, method: str, path: str, body: dict | None = None) -> dict:
        if method != "POST":
            raise DualLinksError(500, "unsupported internal DUAL method")
        try:
            if path == "/session":
                return await dual_service.create_session_data()
            if path == "/dual/aprep":
                return await dual_service.prepare_audio_data(body or {}, request)
            if path == "/sync":
                return await dual_service.sync_audio_data(body or {}, request)
            raise DualLinksError(500, f"unsupported internal DUAL path: {path}")
        except dual_service.DualServiceError as exc:
            raise DualLinksError(exc.status, str(exc.detail)) from exc

    @staticmethod
    def _decode_dual_descriptor(value: str) -> dict:
        encoded = str(value or "").strip()
        if not encoded or len(encoded) > 256 * 1024:
            raise DualLinksError(400, "dual descriptor is missing or too large")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DualLinksError(400, "invalid Base64 JSON dual descriptor") from exc
        if not isinstance(body, dict):
            raise DualLinksError(400, "dual descriptor must contain a JSON object")
        return body

    @staticmethod
    def _audio_url_with_sync(url: str, sync: dict) -> str:
        parts = urllib.parse.urlsplit(str(url))
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        try:
            offset_ms = int(round(float(sync.get("offset") or 0) * 1000))
        except (TypeError, ValueError):
            offset_ms = 0
        try:
            rate_nano = int(round(float(sync.get("rate") or 1) * 1_000_000_000))
        except (TypeError, ValueError):
            rate_nano = 1_000_000_000
        query.update({"o": str(offset_ms), "r": str(rate_nano)})
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    @staticmethod
    def _dual_master(result: dict) -> str:
        resolution = int(result["resolution"])
        width = max(2, round(resolution * 16 / 9))
        bandwidth = 25_000_000 if resolution >= 2160 else 8_000_000
        language = str(result.get("audio_hls_language") or result["audio_lang"])
        name = str(result.get("audio_name") or result["audio_lang"]).replace('"', "'")
        audio_url = str(result["audio_url"]).replace('"', "%22")
        video_url = str(result["video_url"]).replace('"', "%22")
        return "\n".join([
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="dual-audio",LANGUAGE="{language}",NAME="{name}",DEFAULT=YES,AUTOSELECT=YES,URI="{audio_url}"',
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{resolution},CODECS="avc1.640028,mp4a.40.2",AUDIO="dual-audio"',
            video_url,
            "",
        ])

    async def _dual_error_segment(self, kind: str) -> bytes:
        if kind not in _DUAL_ERROR_MESSAGES:
            raise ValueError("unknown DUAL error video")
        cache = getattr(self, "_dual_error_segments", None)
        lock = getattr(self, "_dual_error_lock", None)
        if cache is None or lock is None:
            raise RuntimeError("DUAL error video cache is not initialized")
        if kind not in cache:
            async with lock:
                if kind not in cache:
                    cache[kind] = await asyncio.to_thread(
                        _generate_dual_error_segment, kind
                    )
        return cache[kind]

    async def _dual_error_manifest(self, request, kind: str) -> web.Response:
        try:
            await self._dual_error_segment(kind)
        except Exception as exc:
            logger.error("DUAL error video unavailable: %s", exc)
            return web.json_response(
                {
                    "status": "error",
                    "detail": "DUAL error video unavailable; FFmpeg is required",
                },
                status=502,
            )

        segment_url = f"{get_public_base_url(request)}/dual/error/{kind}.ts"
        api_password = request.query.get("api_password")
        if api_password:
            segment_url = f"{segment_url}?{urllib.parse.urlencode({'api_password': api_password})}"
        manifest = "\n".join([
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{_DUAL_ERROR_DURATION}",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f"#EXTINF:{_DUAL_ERROR_DURATION}.000,",
            segment_url,
            "#EXT-X-ENDLIST",
            "",
        ])
        return web.Response(
            text=manifest,
            content_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    async def handle_dual_error_m3u8(self, request):
        """Return a short playable HLS video explaining an expected DUAL failure."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        kind = str(request.match_info.get("kind") or "").lower()
        if kind not in _DUAL_ERROR_MESSAGES:
            return web.Response(status=404, text="Unknown DUAL error video")
        if request.method == "HEAD":
            return web.Response(
                content_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )
        return await self._dual_error_manifest(request, kind)

    async def handle_dual_error_segment(self, request):
        """Serve the cached MPEG-TS segment used by the DUAL error HLS video."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        kind = str(request.match_info.get("kind") or "").lower()
        if kind not in _DUAL_ERROR_MESSAGES:
            return web.Response(status=404, text="Unknown DUAL error video")
        if request.method == "HEAD":
            return web.Response(
                content_type="video/mp2t",
                headers={"Cache-Control": "no-store"},
            )
        try:
            segment = await self._dual_error_segment(kind)
        except Exception as exc:
            logger.error("DUAL error segment unavailable: %s", exc)
            return web.Response(status=502, text="DUAL error video unavailable")
        return web.Response(
            body=segment,
            content_type="video/mp2t",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    async def _build_dual_result(self, request, body: dict) -> dict:
        requested_audio_lang = str(
            body.get("audio_lang") or body.get("audioLanguage") or ""
        ).strip()
        bypass_audio_language = body.get(
            "bypass_audio_language", body.get("audio_language_bypass", False)
        )
        bypass_audio_language = (
            bypass_audio_language is True
            or str(bypass_audio_language).strip().lower() in {"1", "true", "yes", "on"}
        )
        audio_lang = _normalise_language(requested_audio_lang)
        if audio_lang not in _LANGUAGE_ALIASES:
            raise DualLinksError(400, "audio_lang is required; use a standard language code such as ita or eng")

        video_spec = body.get("video") or body.get("video_url")
        audio_spec = body.get("audio") or body.get("audio_url")
        video = await self._resolve_dual_spec(video_spec)
        audio = await self._resolve_dual_spec(audio_spec)

        video_text, video_base = await self._manifest(video)
        requested_resolution = int(body.get("resolution") or 0)
        video_url, resolution, auto_reference = self._pick_video(
            video_text, video_base, requested_resolution
        )
        reference_audio_url = str(body.get("reference_audio_url") or "").strip() or auto_reference

        audio_text, audio_base = await self._manifest(audio)
        selected_audio_url, audio_meta = self._pick_audio(
            audio_text,
            audio_base,
            audio_lang,
            requested_audio_lang,
            bypass_audio_language,
        )
        audio_media = dict(audio)
        audio_media["url"] = selected_audio_url
        audio_media["manifest"] = ""
        audio_playlist, audio_playlist_base = await self._manifest(audio_media)

        media_key = str(body.get("media_key") or body.get("mediaKey") or "").strip()
        if not media_key:
            media_key = hashlib.sha1(str(body.get("video_url") or video["url"]).encode()).hexdigest()[:24]
        video_fingerprint = str(body.get("video_fingerprint") or "").strip()
        if not video_fingerprint:
            video_fingerprint = self._fingerprint(video_url, video.get("headers") or {})

        session_result = await self._dual_json(request, "POST", "/session", {})
        token = str(session_result.get("token") or "")
        if not token:
            raise DualLinksError(502, "DUAL service did not return a session token")

        prepared = await self._prepare_dual_audio(
            request,
            audio_media,
            audio_playlist,
            audio_playlist_base,
            token,
            media_key,
            audio_lang,
        )
        audio_hid = str(prepared.get("hid") or "")
        if not audio_hid:
            raise DualLinksError(502, "DUAL service did not register the selected audio")

        video_routing = {
            "warp_off": bool(video.get("warp_off")),
            "proxy_off": bool(video.get("proxy_off")),
            "proxy_url": video.get("forced_proxy") or "",
            "extractor_name": video.get("extractor_name") or "",
        }
        def sync_body(candidate: dict) -> dict:
            return {
                "token": token,
                "media_key": media_key,
                "resolution": resolution,
                "video_url": video_url,
                "video_headers": video.get("headers") or {},
                "reference_audio_url": reference_audio_url,
                "audio_hid": candidate.get("hid") or "",
                "audio_fingerprint": candidate.get("audio_fingerprint") or "",
                "video_fingerprint": video_fingerprint,
                **video_routing,
            }

        synced = await self._dual_json(
            request, "POST", "/sync", sync_body(prepared)
        )
        bridge_used = False
        if str(synced.get("status") or "") != "ok" and audio_lang != "eng":
            try:
                bridge_url, bridge_meta = self._pick_audio(audio_text, audio_base, "eng")
                if bridge_url != selected_audio_url:
                    bridge_media = dict(audio)
                    bridge_media["url"] = bridge_url
                    bridge_media["manifest"] = ""
                    bridge_playlist, bridge_base = await self._manifest(bridge_media)
                    if _same_audio_timeline(audio_playlist, bridge_playlist):
                        bridge = await self._prepare_dual_audio(
                            request,
                            bridge_media,
                            bridge_playlist,
                            bridge_base,
                            token,
                            media_key,
                            "eng",
                        )
                        bridge_sync = await self._dual_json(
                            request, "POST", "/sync", sync_body(bridge)
                        )
                        if str(bridge_sync.get("status") or "") == "ok":
                            synced = bridge_sync
                            bridge_used = True
                            # Refresh requested output after the extra sync work.
                            prepared = await self._prepare_dual_audio(
                                request,
                                audio_media,
                                audio_playlist,
                                audio_playlist_base,
                                token,
                                media_key,
                                audio_lang,
                            )
                            audio_hid = str(prepared.get("hid") or "")
                            logger.info(
                                "[DUAL] direct %s sync failed; English bridge '%s' succeeded",
                                audio_lang,
                                bridge_meta.get("name") or "English",
                            )
            except DualLinksError as exc:
                logger.warning("[DUAL] English sync bridge unavailable: %s", exc.message)
        status = str(synced.get("status") or "")
        if status != "ok":
            detail = synced.get("message") or synced.get("detail") or "DUAL sync failed"
            raise DualLinksError(409, str(detail))
        result = {
            "status": status,
            "audio_lang": audio_lang,
            "audio_name": audio_meta.get("name") or "",
            "audio_hls_language": audio_meta.get("hls_language") or audio_lang,
            "resolution": resolution,
            "video_url": video_url,
            "reference_audio_url": reference_audio_url,
            "audio_url": self._audio_url_with_sync(str(prepared.get("url") or ""), synced),
            "audio_hid": audio_hid,
            "sync": synced,
        }
        if bridge_used:
            result["sync_bridge"] = "eng"
        result["m3u8"] = self._dual_master(result)
        return result

    async def handle_dual_sync_links(self, request):
        """Sync a direct/extracted video with a selected-language audio track."""
        if not check_password(request):
            return web.json_response({"detail": "Unauthorized: Invalid API Password"}, status=401)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"detail": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"detail": "JSON body must be an object"}, status=400)
        try:
            result = await self._build_dual_result(request, body)
            result.pop("m3u8", None)
            return web.json_response(result, status=200)
        except DualLinksError as exc:
            return web.json_response({"status": "error", "detail": exc.message}, status=exc.status)
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "detail": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("DUAL link sync failed")
            return web.json_response(
                {"status": "error", "detail": f"dual link sync failed: {type(exc).__name__}"},
                status=502,
            )

    async def handle_dual_server_m3u8(self, request):
        """Build a combined HLS master from a Base64 JSON descriptor."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")
        if request.method == "HEAD":
            return web.Response(
                content_type="application/vnd.apple.mpegurl",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
            )
        try:
            body = self._decode_dual_descriptor(request.query.get("d", ""))
            bypass_query = str(
                request.query.get("bypass_audio_language") or ""
            ).strip().lower()
            if bypass_query in {"1", "true", "yes", "on"}:
                body["bypass_audio_language"] = True
            result = await self._build_dual_result(request, body)
            return web.Response(
                text=result["m3u8"],
                content_type="application/vnd.apple.mpegurl",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
            )
        except DualLinksError as exc:
            error_kind = _dual_error_kind(exc)
            if error_kind:
                return await self._dual_error_manifest(request, error_kind)
            return web.json_response({"status": "error", "detail": exc.message}, status=exc.status)
        except (ValueError, TypeError) as exc:
            error_kind = _dual_error_kind(exc)
            if error_kind:
                return await self._dual_error_manifest(request, error_kind)
            return web.json_response({"status": "error", "detail": str(exc)}, status=400)
        except Exception as exc:
            error_kind = _dual_error_kind(exc)
            if error_kind:
                return await self._dual_error_manifest(request, error_kind)
            logger.exception("DUAL server master build failed")
            return web.json_response(
                {"status": "error", "detail": f"dual server build failed: {type(exc).__name__}"},
                status=502,
            )


__all__ = ["HLSProxyDualMixin"]
