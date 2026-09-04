import re
from urllib.parse import urlparse

from extractors.base import BaseExtractor, ExtractorError


ADS_ORIGIN = "https://altadefinizionestreaming.tv"
ADS_COOKIE = "sid=32234dfabd14e587764e84405e75e99856c6bef31c6b1752e19897b8ae3d4a21"


class ADSExtractor(BaseExtractor):
    """Resolve AltadefinizioneStreaming's IP-bound CDN URLs."""

    def __init__(self, request_headers: dict, proxies: list = None):
        super().__init__(request_headers, proxies, extractor_name="ads")
        self.mediaflow_endpoint = "proxy_stream_endpoint"

    @staticmethod
    def _cookie_from_kwargs(kwargs: dict) -> str:
        for key, value in kwargs.items():
            if key.lower() == "h_cookie":
                return str(value or "").strip()
        return ADS_COOKIE

    async def extract(self, url: str, **kwargs) -> dict:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"altadefinizionestreaming.tv", "www.altadefinizionestreaming.tv"}
        ):
            raise ExtractorError("ADS: invalid URL")

        if parsed.path.startswith("/api/player-sources/"):
            sources_url = url
        else:
            film_match = re.fullmatch(r"/film/.+-(\d+)/?", parsed.path)
            if not film_match:
                raise ExtractorError("ADS: unsupported direct URL")
            sources_url = f"{ADS_ORIGIN}/api/player-sources/movie/{film_match.group(1)}"

        cookie = self._cookie_from_kwargs(kwargs)
        if not cookie:
            raise ExtractorError("ADS: cookie unavailable")

        api_headers = {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": f"{ADS_ORIGIN}/",
            "Accept": "application/json,text/plain,*/*",
            "Cookie": cookie,
        }
        response = await self._make_request(sources_url, headers=api_headers)
        payload = response.json
        sources = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(sources, list):
            raise ExtractorError("ADS: invalid player-sources response")

        source = next(
            (
                item for item in sources
                if isinstance(item, dict)
                and str(item.get("provider") or "").lower() == "cdn"
                and item.get("url")
            ),
            None,
        )
        stream_url = str(source.get("url") if source else "").strip()
        stream_parsed = urlparse(stream_url)
        if stream_parsed.scheme not in {"http", "https"} or not stream_parsed.hostname:
            raise ExtractorError("ADS: CDN source not found")

        return {
            "destination_url": stream_url,
            "request_headers": {
                "User-Agent": self.base_headers["User-Agent"],
                "Referer": f"{ADS_ORIGIN}/",
                "Accept": "*/*",
            },
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "selected_proxy": self._session_proxy,
        }
