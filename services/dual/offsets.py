"""Shared MongoDB cache for DUAL synchronization offsets."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError


logger = logging.getLogger("easyproxy.dual.offsets")

# Shared offset database used by every EasyProxy instance.
SHARED_MONGO_URI = "mongodb+srv://easyproxy:1R8GrKM9unOG7K63@easyproxy.g3pkclx.mongodb.net/?appName=easyproxy"
SHARED_MONGO_DATABASE = "easyproxy"
SHARED_MONGO_COLLECTION = "offsets"


class OffsetStore:
    """Shared offset cache.

    Only synchronization metadata is stored. Media, source URLs and tokens are
    never persisted. MongoDB calls run in worker threads so the aiohttp event
    loop is not blocked by the synchronous PyMongo client.
    """

    def __init__(
        self,
        path: str | None = None,
        mongo_uri: str | None = None,
        db_name: str | None = None,
        collection_name: str | None = None,
    ):
        del path  # Kept for constructor compatibility; SQLite is intentionally gone.
        self.mongo_uri = (mongo_uri or SHARED_MONGO_URI).strip()
        self.db_name = (db_name or SHARED_MONGO_DATABASE).strip()
        self.collection_name = (collection_name or SHARED_MONGO_COLLECTION).strip()
        self._client = MongoClient(
            self.mongo_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            appname="easyproxy-offsets",
        )
        self._collection = self._client[self.db_name][self.collection_name]
        self._ensure_indexes()
        logger.info(
            "DUAL offset cache: shared MongoDB database=%s collection=%s",
            self.db_name,
            self.collection_name,
        )

    def _ensure_indexes(self) -> None:
        self._collection.create_index([("cache_key", ASCENDING)], unique=True)
        self._collection.create_index(
            [
                ("media_key", ASCENDING),
                ("resolution", ASCENDING),
                ("video_fingerprint", ASCENDING),
                ("status", ASCENDING),
                ("updated_at", DESCENDING),
            ]
        )

    @staticmethod
    def key(media_key: str, resolution: int, video_fp: str, audio_fp: str) -> str:
        import hashlib

        return hashlib.sha1(
            f"v2|{media_key}|{resolution}|{video_fp}|{audio_fp}".encode()
        ).hexdigest()

    @staticmethod
    def _clean_document(document: dict[str, Any] | None):
        if not document:
            return None
        result = dict(document)
        result.pop("_id", None)
        details = result.get("details")
        if isinstance(details, str):
            try:
                result["details"] = json.loads(details)
            except (TypeError, ValueError):
                result["details"] = {}
        return result

    def _mongo_get(self, cache_key: str):
        try:
            return self._clean_document(
                self._collection.find_one({"cache_key": cache_key})
            )
        except PyMongoError:
            logger.warning("MongoDB offset lookup failed", exc_info=True)
            return None

    async def lookup(self, payload: dict):
        return await asyncio.to_thread(self._mongo_get, payload["cache_key"])

    @staticmethod
    def _cache_fields(payload: dict):
        media_key = str(payload.get("mediaKey") or payload.get("media_key") or "")
        try:
            resolution = int(payload.get("resolution") or 0)
        except (TypeError, ValueError):
            return None
        video_fp = str(
            payload.get("videoFingerprint")
            or payload.get("video_fingerprint")
            or ""
        )
        audio_fp = str(
            payload.get("audioFingerprint")
            or payload.get("audio_fingerprint")
            or ""
        )
        if not media_key or resolution <= 0 or not video_fp:
            return None
        return media_key, resolution, video_fp, audio_fp

    def _cache_status(self, payload: dict):
        fields = self._cache_fields(payload)
        if not fields:
            return None
        media_key, resolution, video_fp, audio_fp = fields
        try:
            if audio_fp:
                document = self._collection.find_one(
                    {"cache_key": self.key(media_key, resolution, video_fp, audio_fp)}
                )
            else:
                base_filter = {
                    "media_key": media_key,
                    "resolution": resolution,
                    "video_fingerprint": video_fp,
                }
                document = self._collection.find_one(
                    {**base_filter, "status": "ok"},
                    sort=[("updated_at", DESCENDING)],
                )
                if document is None:
                    document = self._collection.find_one(
                        base_filter, sort=[("updated_at", DESCENDING)]
                    )
            document = self._clean_document(document)
        except PyMongoError:
            logger.warning("MongoDB offset status lookup failed", exc_info=True)
            return None
        if not document:
            return None
        return {
            "status": document.get("status"),
            "offset": document.get("offset_seconds"),
            "rate": document.get("rate"),
            "confidence": document.get("confidence"),
            "updated_at": document.get("updated_at"),
        }

    async def cache_status(self, payload: dict):
        return await asyncio.to_thread(self._cache_status, payload)

    def _mongo_put(self, payload: dict, result: dict):
        document = {
            "cache_key": payload["cache_key"],
            "media_key": payload["media_key"],
            "resolution": int(payload["resolution"]),
            "video_fingerprint": payload["video_fingerprint"],
            "audio_fingerprint": payload["audio_fingerprint"],
            "offset_seconds": result.get("offset"),
            "rate": result.get("rate", 1.0),
            "confidence": result.get("confidence", 0.0),
            "status": result.get("status", "incompatible"),
            "details": dict(result),
            "updated_at": time.time(),
        }
        self._collection.replace_one(
            {"cache_key": document["cache_key"]}, document, upsert=True
        )

    async def report(self, payload: dict, result: dict):
        try:
            await asyncio.to_thread(self._mongo_put, payload, result)
        except PyMongoError:
            # Playback remains available; only the shared cache write failed.
            logger.warning("MongoDB offset report failed", exc_info=True)

    def close(self) -> None:
        self._client.close()
