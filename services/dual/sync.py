import asyncio
import hashlib
import logging
import math
import os
import re
import shutil
import statistics
import tempfile
from array import array
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from .audio import AudioStore
from .http_client import create_client_session
from .offsets import RemoteOffsetStore
from .security import resolves_publicly, valid_public_url
from .routing import RoutingOptions, from_values


logger = logging.getLogger("easyproxy.dual.sync")


class _MediaResponse:
    def __init__(self, status_code: int, headers: dict, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class SyncEngine:
    MAX_REDIRECTS = 5
    MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
    MAX_MEDIA_BYTES = 32 * 1024 * 1024
    MAX_SYNC_BYTES = 512 * 1024 * 1024

    def __init__(self, audio: AudioStore, offsets: RemoteOffsetStore, proxy: str = ""):
        self.audio = audio
        self.offsets = offsets
        self.routing = RoutingOptions(forced_proxy=proxy.strip() or None)
        self.sample_seconds = 5
        self._sync_semaphore = asyncio.Semaphore(1)
        self._media_download_semaphore = asyncio.Semaphore(6)
        self._downloaded_bytes = 0
        self._download_cache: dict[tuple[str, tuple[tuple[str, str], ...]], Path] = {}
        self._download_locks: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Lock] = {}
        self._download_cache_dir: Path | None = None
        self._http_sessions: dict[str, tuple[aiohttp.ClientSession, str | None]] = {}

    def _account_bytes(self, amount: int) -> None:
        self._downloaded_bytes += amount
        if self._downloaded_bytes > self.MAX_SYNC_BYTES:
            raise RuntimeError("sync download budget exceeded")

    def _client_for(self, proxy: str):
        cached = self._http_sessions.get(proxy)
        if cached is not None and not cached[0].closed:
            return cached
        created = create_client_session(proxy, timeout=30)
        self._http_sessions[proxy] = created
        return created

    async def _get(
        self,
        url: str,
        headers: dict,
        destination: Path | None = None,
        max_bytes: int = MAX_PLAYLIST_BYTES,
    ):
        current_url = url
        for redirect_count in range(self.MAX_REDIRECTS + 1):
            if not valid_public_url(current_url) or not await resolves_publicly(current_url):
                raise ValueError("media URL is not public HTTPS")
            proxy = self.routing.proxy_for(current_url)
            try:
                client, request_proxy = self._client_for(proxy)
                async with client.get(
                    current_url,
                    headers=headers,
                    proxy=request_proxy,
                    allow_redirects=False,
                ) as response:
                    status_code = response.status
                    response_headers = dict(response.headers)
                    if status_code in (301, 302, 303, 307, 308):
                        location = response_headers.get("location", "").strip()
                        if not location:
                            raise ValueError("media redirect has no location")
                        next_url = urljoin(current_url, location)
                        if not valid_public_url(next_url) or not await resolves_publicly(next_url):
                            raise ValueError("media redirect is not public HTTPS")
                        if redirect_count >= self.MAX_REDIRECTS:
                            raise ValueError("too many media redirects")
                        current_url = next_url
                        continue

                    if status_code >= 400:
                        raise RuntimeError(f"media fetch failed: HTTP {status_code}")
                    content_length = response_headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise ValueError("media response too large")
                        except ValueError as exc:
                            if str(exc) == "media response too large":
                                raise
                            raise ValueError("invalid media content length") from exc

                    if destination is not None:
                        temporary = destination.with_name(f".{destination.name}.part")
                        try:
                            total = 0
                            with temporary.open("wb") as output:
                                async for chunk in response.content.iter_chunked(64 * 1024):
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise ValueError("media response too large")
                                    output.write(chunk)
                            self._account_bytes(total)
                            temporary.replace(destination)
                            content = b""
                        finally:
                            temporary.unlink(missing_ok=True)
                    else:
                        chunks = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("media response too large")
                            chunks.append(chunk)
                        self._account_bytes(total)
                        content = b"".join(chunks)
                    return _MediaResponse(status_code, response_headers, content)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("media fetch timed out") from exc
            except aiohttp.ClientError as exc:
                raise RuntimeError(f"media fetch failed: {exc}") from exc
        raise ValueError("too many media redirects")

    @staticmethod
    def _download_key(url: str, headers: dict) -> tuple[str, tuple[tuple[str, str], ...]]:
        return str(url), tuple(sorted((str(key), str(value)) for key, value in (headers or {}).items()))

    @staticmethod
    async def _gather_strict(*awaitables):
        """Cancel and drain siblings before propagating an error or cancellation."""
        tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _playlist(text: str, master_url: str):
        entries, pending, elapsed = [], None, 0.0
        map_url = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-MAP:"):
                import re
                match = re.search(r'URI="([^"]+)"', line)
                map_url = urljoin(master_url, match.group(1)) if match else None
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                entries.append({"url": urljoin(master_url, line), "duration": pending, "start": elapsed})
                elapsed += pending
                pending = None
        if not entries:
            raise ValueError("empty media playlist")
        return entries, map_url

    async def _video_entries(self, url: str, headers: dict):
        response = await self._get(url, headers)
        return self._playlist(response.text, url)

    async def _download(self, url: str, path: Path, headers: dict):
        key = self._download_key(url, headers)
        lock = self._download_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._download_cache.get(key)
            if cached and cached.is_file():
                shutil.copyfile(cached, path)
                return

            target = path
            if self._download_cache_dir is not None:
                digest = hashlib.sha256(repr(key).encode()).hexdigest()
                target = self._download_cache_dir / f"{digest}.bin"
            async with self._media_download_semaphore:
                await self._get(url, headers, destination=target, max_bytes=self.MAX_MEDIA_BYTES)
            if self._download_cache_dir is not None:
                self._download_cache[key] = target
                if target != path:
                    shutil.copyfile(target, path)

    def _sample_entries(self, entries, position: float, sample_seconds: float | None = None):
        sample_seconds = self.sample_seconds if sample_seconds is None else sample_seconds
        target = next((i for i, item in enumerate(entries)
                       if item["start"] <= position < item["start"] + item["duration"]),
                      len(entries) - 1)
        first = max(0, target - 1)
        local_seek = max(0.0, position - entries[first]["start"])
        selected, available = [], 0.0
        for item in entries[first:]:
            selected.append(item)
            available += item["duration"]
            if available >= local_seek + sample_seconds + 5.0:
                break
        return selected, local_seek, sum(item["duration"] for item in entries)

    async def _decode_video(
        self, url: str, headers: dict, position: float, directory: Path,
        sample_seconds: float | None = None,
    ):
        _, entries, map_url = (url, *await self._video_entries(url, headers))
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{int(max(x['duration'] for x in selected)) + 1}"]
        downloads = []
        if map_url:
            downloads.append(self._download(map_url, directory / "video-init.mp4", headers))
            lines.append('#EXT-X-MAP:URI="video-init.mp4"')
        for number, item in enumerate(selected):
            name = f"video-{number}.m4s"
            downloads.append(self._download(item["url"], directory / name, headers))
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "video.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _decode_reference_audio(
        self, url: str, headers: dict, position: float, directory: Path,
        sample_seconds: float | None = None,
    ):
        response = await self._get(url, headers)
        entries, map_url = self._playlist(response.text, url)
        selected, local_seek, duration = self._sample_entries(entries, position, sample_seconds)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in selected)) + 1}"]
        downloads = []
        key_line = next((line.strip() for line in response.text.splitlines()
                         if line.strip().startswith("#EXT-X-KEY:")), "")
        if key_line and "METHOD=NONE" not in key_line.upper():
            key_match = re.search(r'URI="([^"]+)"', key_line)
            if key_match:
                downloads.append(self._download(
                    urljoin(url, key_match.group(1)), directory / "reference.key", headers
                ))
                key_line = re.sub(r'URI="[^"]+"', 'URI="reference.key"', key_line, count=1)
            lines.append(key_line)
        if map_url:
            downloads.append(self._download(map_url, directory / "reference-init.mp4", headers))
            lines.append('#EXT-X-MAP:URI="reference-init.mp4"')
        for number, item in enumerate(selected):
            name = f"reference-{number}.m4s"
            downloads.append(self._download(item["url"], directory / name, headers))
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "reference.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, duration

    async def _media_start_time(self, url: str, headers: dict) -> float:
        """Read the initial timestamp of Cinejoy's video-only fMP4 stream."""
        response = await self._get(url, headers)
        match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', response.text)
        first = next((line.strip() for line in response.text.splitlines()
                      if line.strip() and not line.startswith("#")), "")
        if not match or not first:
            return 0.0
        root = Path(tempfile.mkdtemp(prefix="cinejoy-start-"))
        try:
            init_path = root / "init.mp4"
            segment_path = root / "first.m4s"
            await asyncio.gather(
                self._download(urljoin(url, match.group(1)), init_path, headers),
                self._download(urljoin(url, first), segment_path, headers),
            )
            sample = root / "sample.mp4"
            with sample.open("wb") as output:
                for part in (init_path, segment_path):
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            process = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "stream=start_time",
                "-of", "default=nw=1:nk=1", str(sample),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            values = output.decode(errors="replace").strip().splitlines()
            return float(values[0]) if values else 0.0
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def _decode_audio(self, hid: str, position: float, directory: Path):
        metadata = self.audio.metadata(hid)
        index = next((i for i, start in enumerate(metadata["starts"]) if start <= position < start + metadata["durs"][i]), len(metadata["segs"]) - 1)
        first = max(0, index - 1)
        local_seek = max(0.0, position - metadata["starts"][first])
        selected = []
        available = 0.0
        for item in range(first, len(metadata["segs"])):
            selected.append(item)
            available += metadata["durs"][item]
            if available >= local_seek + self.sample_seconds + 5:
                break
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(metadata['durs'][item] for item in selected)) + 1}"]
        key_bytes = self.audio.key_bytes(hid)
        if key_bytes:
            iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="audio.key"{iv}')
            (directory / "audio.key").write_bytes(key_bytes)
        downloads = []
        for number, item in enumerate(selected):
            name = f"audio-{number}.ts"
            downloads.append(self._download(
                metadata["segs"][item], directory / name, metadata.get("headers") or {}
            ))
            lines += [f"#EXTINF:{metadata['durs'][item]:.6f},", name]
        await self._gather_strict(*downloads)
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "audio.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(metadata["durs"])

    @staticmethod
    async def _pcm(
        playlist: Path, seek: float, output: Path, audio_map: bool = True,
        sample_seconds: float = 5,
    ):
        command = ["ffmpeg", "-v", "error", "-threads", "1", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", str(playlist), "-ss", f"{max(0.0, seek):.3f}", "-t", f"{sample_seconds:g}"]
        if audio_map:
            command += ["-map", "0:a:0", "-vn"]
        command += ["-ac", "1", "-ar", "8000", "-f", "s16le", "-y", str(output)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        minimum_size = min(160000, int(sample_seconds * 8000 * 2 * .75))
        if process.returncode or not output.exists() or output.stat().st_size < minimum_size:
            raise RuntimeError((error.decode(errors="replace") or "sample decode failed")[:300])

    async def _muxed_reference_matches_video(
        self,
        video_url: str,
        reference_url: str,
        headers: dict,
        position: float,
        root: Path,
    ) -> bool:
        sample_seconds = 8.0
        video_dir = root / "reference-check-video"
        reference_dir = root / "reference-check-low"
        video_dir.mkdir()
        reference_dir.mkdir()
        try:
            video_result, reference_result = await self._gather_strict(
                self._decode_video(
                    video_url, headers, position, video_dir, sample_seconds
                ),
                self._decode_reference_audio(
                    reference_url, headers, position, reference_dir, sample_seconds
                ),
            )
            video_playlist, video_seek, _ = video_result
            reference_playlist, reference_seek, _ = reference_result
            video_pcm = root / "reference-check-video.pcm"
            reference_pcm = root / "reference-check-low.pcm"
            await self._gather_strict(
                self._pcm(video_playlist, video_seek, video_pcm, sample_seconds=sample_seconds),
                self._pcm(reference_playlist, reference_seek, reference_pcm, sample_seconds=sample_seconds),
            )
            video_envelope, reference_envelope = await asyncio.gather(
                asyncio.to_thread(self._envelope, video_pcm),
                asyncio.to_thread(self._envelope, reference_pcm),
            )
            lag, correlation = await asyncio.to_thread(
                self._lag, video_envelope, reference_envelope, 1
            )
            return correlation >= .75 and abs(lag) <= .10
        except (ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            logger.warning("[DUAL] muxed reference validation failed: %s", exc)
            return False

    @staticmethod
    def _envelope(path: Path):
        values = array("h")
        values.frombytes(path.read_bytes())
        if not values:
            return []
        if os.sys.byteorder != "little":
            values.byteswap()
        step, window = 80, 160
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + abs(value))
        envelope = []
        for center in range(0, len(values), step):
            lo, hi = max(0, center - window // 2), min(len(values), center + window // 2)
            envelope.append((prefix[hi] - prefix[lo]) / max(1, hi - lo))
        mean = sum(envelope) / len(envelope)
        std = math.sqrt(sum((value - mean) ** 2 for value in envelope) / len(envelope)) or 1.0
        return [(value - mean) / std for value in envelope]

    @staticmethod
    def _lag(reference, candidate, max_seconds=15):
        best = (-2.0, 0)
        for lag in range(-max_seconds * 100, max_seconds * 100 + 1):
            if lag >= 0:
                size = min(len(reference), len(candidate) - lag)
                left, right = reference[:size], candidate[lag:lag + size]
            else:
                size = min(len(candidate), len(reference) + lag)
                left, right = reference[-lag:-lag + size], candidate[:size]
            if size < 500:
                continue
            lm, rm = sum(left) / size, sum(right) / size
            lv = sum((value - lm) ** 2 for value in left)
            rv = sum((value - rm) ** 2 for value in right)
            denominator = math.sqrt(lv * rv)
            if denominator:
                correlation = sum((left[i] - lm) * (right[i] - rm) for i in range(size)) / denominator
                if correlation > best[0]:
                    best = correlation, lag
        return best[1] / 100.0, best[0]

    async def measure(self, payload: dict):
        # Offset detection invokes several downloads and decoder processes.
        # Serialize it so concurrent DUAL sessions cannot multiply the
        # CPU/RAM/network cost on a personal EasyProxy instance.
        audio_hid = str(payload.get("audio_hid") or "")
        self.audio.pin(audio_hid)
        try:
            async with self._sync_semaphore:
                self.routing = from_values(payload.get("_routing"), payload)
                self._download_cache = {}
                cache_directory = tempfile.TemporaryDirectory(prefix="dual-sync-cache-")
                self._download_cache_dir = Path(cache_directory.name)
                try:
                    return await self._measure(payload)
                finally:
                    self._download_cache.clear()
                    self._download_locks.clear()
                    self._download_cache_dir = None
                    sessions = [item[0] for item in self._http_sessions.values()]
                    self._http_sessions.clear()
                    await asyncio.gather(*(session.close() for session in sessions))
                    cache_directory.cleanup()
        finally:
            self.audio.unpin(audio_hid)

    async def _measure(self, payload: dict):
        self._downloaded_bytes = 0
        media_key = str(payload.get("media_key") or "")
        resolution = int(payload.get("resolution") or 0)
        video_url = str(payload.get("video_url") or "")
        video_headers = payload.get("video_headers") if isinstance(payload.get("video_headers"), dict) else {}
        reference_audio_url = str(
            payload.get("reference_audio_url") or payload.get("referenceAudio") or ""
        ).strip()
        validate_muxed_reference = bool(payload.get("validate_muxed_reference"))
        audio_hid = str(payload.get("audio_hid") or "")
        video_fp = str(payload.get("video_fingerprint") or "")
        metadata = self.audio.metadata(audio_hid)
        audio_fp = str(payload.get("audio_fingerprint") or metadata.get("source_fingerprint") or "")
        cache_key = self.offsets.key(media_key, resolution, video_fp, audio_fp)
        payload["cache_key"] = cache_key
        lookup = await self.offsets.lookup({"cache_key": cache_key, "media_key": media_key, "resolution": resolution, "video_fingerprint": video_fp, "audio_fingerprint": audio_fp, "vpsAccess": payload.get("vpsAccess", "")})
        if lookup:
            cached_details = lookup.get("details") or lookup
            cached_status = str(cached_details.get("status") or lookup.get("status") or "")
            # Negative sync results are not authoritative: a low-quality sample,
            # a temporary CDN failure, or a provider edition change can produce
            # them. Re-measure instead of returning a permanent 409.
            missing_reference_validation = (
                validate_muxed_reference
                and "reference_matches_video" not in cached_details
            )
            if cached_status == "ok" and not missing_reference_validation and not (
                reference_audio_url and not cached_details.get("video_start_time")
            ):
                result = {"status": "ok", "cached": True, **cached_details}
                result["cache_key"] = cache_key
                return result
            lookup = None
        video_entries, _ = await self._video_entries(video_url, video_headers)
        video_duration = sum(item["duration"] for item in video_entries)
        video_start_time = 0.0
        if reference_audio_url:
            video_start_time = await self._media_start_time(video_url, video_headers)
        reference_duration = video_duration
        reference_entries = []
        if reference_audio_url:
            reference_entries, _ = await self._video_entries(reference_audio_url, video_headers)
            reference_duration = sum(item["duration"] for item in reference_entries)
            if abs(reference_duration - video_duration) > 1.0:
                logger.warning(
                    "[DUAL] reference/video playlist durations differ: video=%.3fs reference=%.3fs; continuing with correlation",
                    video_duration,
                    reference_duration,
                )
        audio_duration = sum(metadata["durs"])
        reference_matches_video = None
        measurements = []

        async def collect_one(position: float, index: int, batch: str):
            video_dir = root / f"{batch}-video-{index}"
            audio_dir = root / f"{batch}-audio-{index}"
            video_dir.mkdir(), audio_dir.mkdir()
            if reference_audio_url:
                reference_task = self._decode_reference_audio(
                    reference_audio_url, video_headers, position, video_dir
                )
            else:
                reference_task = self._decode_video(
                    video_url, video_headers, position, video_dir
                )
            reference_result, audio_result = await self._gather_strict(
                reference_task,
                self._decode_audio(audio_hid, position, audio_dir),
            )
            reference_playlist, reference_seek, _ = reference_result
            audio_playlist, audio_seek, _ = audio_result
            video_pcm = root / f"{batch}-video-{index}.pcm"
            audio_pcm = root / f"{batch}-audio-{index}.pcm"
            samples = await asyncio.gather(
                self._pcm(
                    reference_playlist,
                    reference_seek,
                    video_pcm,
                    sample_seconds=self.sample_seconds,
                ),
                self._pcm(
                    audio_playlist,
                    audio_seek,
                    audio_pcm,
                    sample_seconds=self.sample_seconds,
                ),
                return_exceptions=True,
            )
            failure = next((sample for sample in samples if isinstance(sample, BaseException)), None)
            if failure is not None:
                raise RuntimeError(f"{type(failure).__name__}: {str(failure)[:260]}")
            video_envelope, audio_envelope = await asyncio.gather(
                asyncio.to_thread(self._envelope, video_pcm),
                asyncio.to_thread(self._envelope, audio_pcm),
            )
            lag, correlation = await asyncio.to_thread(
                self._lag, video_envelope, audio_envelope
            )
            return {"position": position, "lag": lag, "offset": lag, "correlation": correlation}

        async def collect(positions, batch: str = "main"):
            start_index = len(measurements)
            measurements.extend(await self._gather_strict(*(
                collect_one(position, start_index + index, batch)
                for index, position in enumerate(positions)
            )))

        with tempfile.TemporaryDirectory(prefix="dual-sync-") as directory:
            root = Path(directory)
            validation_task = None
            if validate_muxed_reference:
                timeline_matches = (
                    len(video_entries) == len(reference_entries)
                    and all(
                        abs(video_item["duration"] - reference_item["duration"]) <= .02
                        for video_item, reference_item in zip(video_entries, reference_entries)
                    )
                )
                if timeline_matches:
                    validation_task = asyncio.create_task(
                        self._muxed_reference_matches_video(
                            video_url,
                            reference_audio_url,
                            video_headers,
                            min(video_duration, reference_duration) * .4,
                            root,
                        )
                    )
                else:
                    reference_matches_video = False
                if reference_matches_video is False:
                    logger.warning(
                        "[DUAL] lowest muxed rendition differs from selected video; using selected video audio"
                    )
                    reference_audio_url = ""
                    reference_duration = video_duration
                    video_start_time = 0.0

            common = min(video_duration, reference_duration, audio_duration)
            if common < 90:
                raise ValueError("media too short")
            fast_positions = sorted({common * .2, common * .4, common * .8})
            all_positions = sorted({
                min(60.0, common * .1),
                common * .2,
                common * .4,
                common * .6,
                common * .8,
                max(30.0, common - 90.0),
            })
            additional_positions = [
                position for position in all_positions if position not in fast_positions
            ]
            if validation_task is not None:
                _, reference_matches_video = await self._gather_strict(
                    collect(fast_positions), validation_task
                )
                if not reference_matches_video:
                    logger.warning(
                        "[DUAL] lowest muxed rendition differs from selected video; using selected video audio"
                    )
                    reference_audio_url = ""
                    reference_duration = video_duration
                    video_start_time = 0.0
                    measurements.clear()
                    await collect(fast_positions, "fallback")
            else:
                await collect(fast_positions)
            fast_valid = [item for item in measurements if item["correlation"] >= .65]
            if len(fast_valid) >= 3:
                measured = statistics.median(item["offset"] for item in fast_valid)
                deviation = max(abs(item["offset"] - measured) for item in fast_valid)
                if deviation <= .25:
                    result = {
                        "status": "ok",
                        "offset": round(-measured + video_start_time, 3),
                        "rate": 1.0,
                        "confidence": min(item["correlation"] for item in fast_valid),
                        "deviation": deviation,
                        "sync_mode": "fast",
                        "video_duration": video_duration,
                        "audio_duration": audio_duration,
                        "measurements": measurements,
                    }
                    if reference_audio_url:
                        result["video_start_time"] = round(video_start_time, 3)
                    if validate_muxed_reference:
                        result["reference_matches_video"] = bool(reference_matches_video)
                    result["cache_key"] = cache_key
                    return result

            await collect(additional_positions)
        # Require a quorum across the timeline. Some valid scenes have little
        # audio signal and produce low correlation; rejecting those alone made
        # otherwise stable sources fail. The offset deviation check remains
        # strict, so unrelated editions still stay incompatible.
        valid = [item for item in measurements if item["correlation"] >= .70]
        if len(valid) < 3:
            result = {"status": "incompatible", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
        else:
            measured = statistics.median(item["offset"] for item in valid)
            deviation = max(abs(item["offset"] - measured) for item in valid)
            result = {"status": "ok" if deviation <= .25 else "incompatible", "offset": round(-measured + video_start_time, 3), "rate": 1.0, "confidence": min(item["correlation"] for item in valid), "deviation": deviation, "sync_mode": "constant", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
            if reference_audio_url:
                result["video_start_time"] = round(video_start_time, 3)
        if validate_muxed_reference:
            result["reference_matches_video"] = bool(reference_matches_video)
        result["cache_key"] = cache_key
        return result
