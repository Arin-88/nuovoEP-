from services.proxy_shared import PlaylistBuilder, logger
import asyncio
import os
import contextvars
from services.proxy_core import HLSProxyCoreMixin
from services.proxy_dash import HLSProxyDashMixin
from services.proxy_handlers import HLSProxyHandlersMixin
from services.proxy_pages import HLSProxyPagesMixin
from services.proxy_streaming import HLSProxyStreamingMixin
from services.proxy_dual import HLSProxyDualMixin

# ContextVars to isolate extractor state per request/asyncio task to avoid concurrent request interference
_extractors_var = contextvars.ContextVar("extractors", default=None)
_extractor_atimes_var = contextvars.ContextVar("extractor_atimes", default=None)
_extractor_stream_atimes_var = contextvars.ContextVar("extractor_stream_atimes", default=None)


class HLSProxy(
    HLSProxyDualMixin,
    HLSProxyCoreMixin,
    HLSProxyHandlersMixin,
    HLSProxyDashMixin,
    HLSProxyStreamingMixin,
    HLSProxyPagesMixin,
):
    """Proxy HLS per stream, playlist, DASH e segmenti."""

    @property
    def extractors(self):
        val = _extractors_var.get()
        if val is None:
            val = {}
            _extractors_var.set(val)
        return val

    @extractors.setter
    def extractors(self, value):
        _extractors_var.set(value)

    @property
    def _extractor_atimes(self):
        val = _extractor_atimes_var.get()
        if val is None:
            val = {}
            _extractor_atimes_var.set(val)
        return val

    @_extractor_atimes.setter
    def _extractor_atimes(self, value):
        _extractor_atimes_var.set(value)

    @property
    def _extractor_stream_atimes(self):
        val = _extractor_stream_atimes_var.get()
        if val is None:
            val = {}
            _extractor_stream_atimes_var.set(val)
        return val

    @_extractor_stream_atimes.setter
    def _extractor_stream_atimes(self, value):
        _extractor_stream_atimes_var.set(value)

    def __init__(self):
        # Note: self.extractors, self._extractor_atimes, and self._extractor_stream_atimes 
        # are lazily initialized per asyncio task context to prevent concurrent request race conditions.

        # Inizializza il playlist_builder se il modulo è disponibile
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder inizializzato")
        else:
            self.playlist_builder = None

        # Background segment prefetch tasks and bounded in-memory results.
        self.prefetch_tasks = set()
        self._background_tasks = set()
        self._prefetch_semaphore = asyncio.Semaphore(5)
        self._prefetch_lock = asyncio.Lock()
        self._parallel_fetch_stats = {
            "calls": 0,
            "active": 0,
            "active_peak": 0,
            "successes": 0,
            "fallbacks": 0,
            "errors": 0,
            "parts_per_call": 3,
            "bytes_total": 0,
            "max_segment_bytes": 0,
            "last_segment_bytes": 0,
            "last_status": None,
            "last_reason": None,
            "last_duration_ms": 0.0,
            "last_segment": None,
        }

        # Short in-memory cache for generated live HLS media playlists.
        # Entries expire quickly and are never persisted to disk.
        self._hls_playlist_cache = {}
        self._segment_next_urls = {}
        self._segment_prefetch_cache = {}

        # Sessione condivisa per il proxy (no proxy)
        self.session = None
        self.flex_session = None

        # Proxy sessions are created fresh per request — no caching

        # Refreshed CDN tokens for live token substitution after re-extract on 403.
        # stream_key -> (old_base_dir, new_base_dir, new_query_string_with_leading_question_mark)
        self._renewed_cdn_tokens: dict[str, tuple[str, str, str]] = {}
        self._renewed_cdn_token_atimes: dict[str, float] = {}
        # Template cache (read once, serve many)
        self._template_cache = {}
        self._template_cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

        # Version information
        self.latest_version = "Checking..."
        self.warp_status = "Checking..."
        self._warp_ip = ""


__all__ = ["HLSProxy"]
