import xml.etree.ElementTree as ET
import urllib.parse
from urllib.parse import urljoin
import logging
import os
import re

logger = logging.getLogger(__name__)

class MPDToHLSConverter:
    """Converte manifest MPD (DASH) in playlist HLS (m3u8) on-the-fly."""
    
    def __init__(self):
        self.ns = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013'
        }

    @staticmethod
    def _expand_segment_template(template: str, rep_id: str, bandwidth: str, number: int = None, timestamp: int = None) -> str:
        """Expand DASH template identifiers, including zero-padded $Number%05d$."""
        value = template.replace('$RepresentationID$', str(rep_id))
        value = value.replace('$Bandwidth$', str(bandwidth))
        if number is not None:
            value = re.sub(
                r'\$Number%0(\d+)d\$',
                lambda match: str(number).zfill(int(match.group(1))),
                value,
            )
            value = value.replace('$Number$', str(number))
        if timestamp is not None:
            value = value.replace('$Time$', str(timestamp))
        return value

    @staticmethod
    def _hls_codec(codec: str) -> str:
        """Normalize DASH codec names for HLS/fMP4 player selection."""
        if not codec:
            return codec
        # HLS clients (notably VLC) commonly advertise HEVC as hvc1,
        # while DASH manifests use the equivalent hev1 sample entry.
        return re.sub(r"^hev1(?=\.|$)", "hvc1", codec)

    @staticmethod
    def _nominal_segment_duration_units(segments):
        """Return a stable duration for live MEDIA-SEQUENCE calculations.

        Live DASH audio timelines can alternate a few milliseconds around the
        nominal duration.  Using the first segment's duration makes the HLS
        sequence jump when the rolling window starts on a different sample.
        The median is stable across those small variations.
        """
        durations = sorted(
            int(segment.get("d", 0))
            for segment in segments
            if int(segment.get("d", 0)) > 0
        )
        if not durations:
            return 1
        return max(1, durations[len(durations) // 2])
    
    def _extract_header_params(self, params: str) -> str:
        """Estrae solo i parametri necessari dalla query string originale.
        
        Estrae:
        - h_* (headers personalizzati)
        - api_password (autenticazione)
        - clearkey (chiavi DRM)
        
        Questo evita di passare parametri di controllo duplicati (d=, rep_id=, format=, etc.)
        che possono causare problemi di parsing degli URL.
        """
        if not params:
            return ""
        
        header_params = []
        for param in params.split('&'):
            if param.startswith('h_') or param.startswith('api_password=') or param.startswith('drm_token=') or param.startswith('clearkey=') or param.startswith('ext=') or param.startswith('warp=') or param.startswith('proxy='):
                header_params.append(param)
        
        if header_params:
            return '&' + '&'.join(header_params)
        return ""

    def convert_master_playlist(self, manifest_content: str, proxy_base: str, original_url: str, params: str) -> str:
        """Genera la Master Playlist HLS dagli AdaptationSet del MPD."""
        try:
            if 'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
            
            root = ET.fromstring(manifest_content)
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            
            # Trova AdaptationSet Video e Audio
            video_sets = []
            audio_sets = []
            
            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                mime_type = adaptation_set.get('mimeType', '')
                content_type = adaptation_set.get('contentType', '')
                
                if 'video' in mime_type or 'video' in content_type:
                    video_sets.append(adaptation_set)
                elif 'audio' in mime_type or 'audio' in content_type:
                    audio_sets.append(adaptation_set)
            
            # Fallback per detection a livello Representation.
            # Va eseguito indipendentemente per video e audio: un MPD può
            # dichiarare l'audio su AdaptationSet e il video solo sui figli.
            def representation_matches(rep, kind):
                rep_type = ' '.join(
                    (
                        rep.get('mimeType', ''),
                        rep.get('contentType', ''),
                    )
                ).lower()
                if kind in rep_type:
                    return True

                codecs = rep.get('codecs', '').lower()
                if kind == 'video':
                    return bool(
                        rep.get('width')
                        or rep.get('height')
                        or any(codec in codecs for codec in (
                            'avc', 'hev', 'hvc', 'vp8', 'vp9', 'av01'
                        ))
                    )
                return any(codec in codecs for codec in (
                    'mp4a', 'aac', 'ac-3', 'ec-3', 'opus', 'vorbis'
                ))

            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                representations = adaptation_set.findall('mpd:Representation', self.ns)
                if (
                    adaptation_set not in video_sets
                    and any(representation_matches(rep, 'video') for rep in representations)
                ):
                    video_sets.append(adaptation_set)
                if (
                    adaptation_set not in audio_sets
                    and any(representation_matches(rep, 'audio') for rep in representations)
                ):
                    audio_sets.append(adaptation_set)

            logger.debug(
                "MPD master tracks detected: video=%d audio=%d",
                len(video_sets),
                len(audio_sets),
            )

            # --- GESTIONE AUDIO (EXT-X-MEDIA) ---
            audio_group_id = 'audio'
            has_audio = False
            
            # Raccogli e ordina le rappresentazioni audio per dare priorità a AAC (mp4a) rispetto a Dolby Digital Plus (ec3)
            audio_reps = []
            for adaptation_set in audio_sets:
                for representation in adaptation_set.findall('mpd:Representation', self.ns):
                    audio_reps.append((adaptation_set, representation))
            
            def sort_audio_func(item):
                rep = item[1]
                rep_id = rep.get('id', '').lower()
                codecs = rep.get('codecs', '').lower()
                if 'mp4a' in rep_id or 'aac' in rep_id or 'mp4a' in codecs or 'aac' in codecs:
                    return 0
                return 1
                
            audio_reps.sort(key=sort_audio_func)

            audio_codecs_list = []
            for _, representation in audio_reps:
                acodec = self._hls_codec(representation.get('codecs'))
                if acodec and acodec not in audio_codecs_list:
                    audio_codecs_list.append(acodec)

            for adaptation_set, representation in audio_reps:
                rep_id = representation.get('id')
                bandwidth = representation.get('bandwidth', '128000') # Default fallback
                
                # Costruisci URL Media Playlist Audio
                encoded_url = urllib.parse.quote(original_url, safe='')
                header_params = self._extract_header_params(params)
                media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{header_params}"
                
                # Usa GROUP-ID 'audio' e NAME basato su ID o lingua
                lang = adaptation_set.get('lang', 'und')
                name = f"Audio {lang} ({bandwidth})"
                
                # EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="...",DEFAULT=YES,AUTOSELECT=YES,URI="..."
                # Impostiamo DEFAULT=YES solo per il primo (che ora sarà AAC se disponibile)
                default_attr = "YES" if not has_audio else "NO"
                
                media_line = f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group_id}",NAME="{name}",LANGUAGE="{lang}",DEFAULT={default_attr},AUTOSELECT=YES,URI="{media_url}"'
                lines.append(media_line)
                has_audio = True

            if has_audio or "format=hls" in params:
                lines[1] = '#EXT-X-VERSION:6'

            # --- GESTIONE VIDEO (EXT-X-STREAM-INF) ---
            # Calcola max height per forzare qualità massima (fix iOS/Stremio)
            max_height = 0
            for adaptation_set in video_sets:
                for rep in adaptation_set.findall('mpd:Representation', self.ns):
                    rep_id = rep.get('id', '')
                    if 'iframe' in rep_id.lower() or 'i-frame' in rep_id.lower():
                        continue
                    try:
                        h = int(rep.get("height", 0))
                        if h > max_height: max_height = h
                    except Exception:
                        logger.debug("Skipping representation without height")
                        pass

            for adaptation_set in video_sets:
                for representation in adaptation_set.findall('mpd:Representation', self.ns):
                    rep_id = representation.get('id', '')
                    if 'iframe' in rep_id.lower() or 'i-frame' in rep_id.lower():
                        continue
                    try:
                        curr_h = int(representation.get("height", 0))
                        if curr_h < max_height: continue
                    except Exception:
                        logger.debug("Representation height parse failed, keeping it")
                        pass

                    rep_id = representation.get('id')
                    bandwidth = representation.get('bandwidth')
                    width = representation.get('width')
                    height = representation.get('height')
                    frame_rate = representation.get('frameRate')
                    codecs = self._hls_codec(representation.get('codecs'))
                    
                    encoded_url = urllib.parse.quote(original_url, safe='')
                    header_params = self._extract_header_params(params)
                    media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{header_params}"
                    
                    # Determine codecs (must combine video and audio codecs for HLS spec compliance)
                    combined_codecs = []
                    if codecs:
                        combined_codecs.append(codecs)
                    if has_audio:
                        combined_codecs.extend(audio_codecs_list)

                    inf = f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}'
                    if width and height:
                        inf += f',RESOLUTION={width}x{height}'
                    if frame_rate:
                        inf += f',FRAME-RATE={frame_rate}'
                    if combined_codecs:
                        inf += f',CODECS="{",".join(combined_codecs)}"'
                    
                    # Collega il gruppo audio se presente
                    if has_audio:
                        inf += f',AUDIO="{audio_group_id}"'
                    
                    lines.append(inf)
                    lines.append(media_url)
            
            return '\n'.join(lines)
        except Exception as e:
            logging.error(f"Error converting Master Playlist: {e}")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)

    def convert_media_playlist(self, manifest_content: str, rep_id: str, proxy_base: str, original_url: str, params: str, clearkey_param: str = None) -> str:
        """Genera la Media Playlist HLS per una specifica Representation."""
        try:
            if 'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
                
            root = ET.fromstring(manifest_content)
            
            # --- RILEVAMENTO LIVE vs VOD ---
            mpd_type = root.get('type', 'static')
            is_live = mpd_type.lower() == 'dynamic'
            
            # Trova la Representation specifica
            representation = None
            adaptation_set = None
            
            # Cerca in tutti gli AdaptationSet
            for aset in root.findall('.//mpd:AdaptationSet', self.ns):
                rep = aset.find(f'mpd:Representation[@id="{rep_id}"]', self.ns)
                if rep is not None:
                    representation = rep
                    adaptation_set = aset
                    break
            
            if representation is None:
                logger.error(f"❌ Representation {rep_id} not found in manifest.")
                return "#EXTM3U\n#EXT-X-ERROR: Representation not found"

            # Keep the track kind on generated segment requests.  DASH commonly
            # uses `.m4s` for both audio and video; iOS uses the response MIME to
            # attach the fMP4 stream to the correct rendition.
            adaptation_kind = (
                adaptation_set.get('contentType', '')
                or adaptation_set.get('mimeType', '')
                or representation.get('mimeType', '')
            ).lower()
            media_type_param = '&media_type=audio' if 'audio' in adaptation_kind else ''

            # fMP4 richiede HLS versione 6 o 7, ma per .ts output usiamo v3 per compatibilità
            # Per LIVE: non usare VOD e non aggiungere ENDLIST
            if is_live:
                lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            else:
                # Target duration is emitted once below from the actual segment
                # durations.  A duplicate tag makes some HLS.js versions treat
                # this VOD playlist as invalid/live and reload it forever.
                lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-PLAYLIST-TYPE:VOD']
            
            # --- GESTIONE DRM (ClearKey) ---
            # Decrittazione lato server con mp4decrypt
            server_side_decryption = False
            decryption_params = ""
            
            if clearkey_param:
                try:
                    # Supporta formato multi-key: "KID1:KEY1,KID2:KEY2"
                    # O "KID:KEY" (legacy simple)
                    kids = []
                    keys = []
                    
                    # Split by comma first to handle multiple pairs
                    pairs = clearkey_param.split(',')
                    for pair in pairs:
                        if ':' in pair:
                            k_id, k_val = pair.split(':')
                            kids.append(k_id.strip())
                            keys.append(k_val.strip())
                    
                    if not kids or not keys:
                        raise ValueError(f"Invalid clearkey format: {clearkey_param}")
                        
                    kid_hex = ",".join(kids)
                    key_hex = ",".join(keys)
                    
                    # Rileva chiave nulla (placeholder) - se TUTTE le chiavi sono tutti zeri
                    is_null_key = all(k.replace('0', '') == '' for k in kids + keys)
                    
                    if is_null_key:
                        # Chiave nulla: usa comunque l'endpoint decrypt per il remux a TS
                        # ma aggiungi flag per saltare la decrittazione vera e propria
                        logger.debug(f"🔓 Null key detected - using remux endpoint without decryption")
                        server_side_decryption = True
                        drm_token = next(
                            (
                                item.split("=", 1)[1]
                                for item in params.split("&")
                                if item.startswith("drm_token=")
                            ),
                            "",
                        )
                        decryption_params = (
                            f"&drm_token={drm_token}&skip_decrypt=1"
                            if drm_token
                            else f"&key={key_hex}&key_id={kid_hex}&skip_decrypt=1"
                        )
                    else:
                        server_side_decryption = True
                        drm_token = next(
                            (
                                item.split("=", 1)[1]
                                for item in params.split("&")
                                if item.startswith("drm_token=")
                            ),
                            "",
                        )
                        decryption_params = (
                            f"&drm_token={drm_token}"
                            if drm_token
                            else f"&key={key_hex}&key_id={kid_hex}"
                        )
                        key_count = len(kids)
                        logger.debug(f"🔐 ClearKey enabled - {key_count} key pair(s) for server-side decryption")
                except Exception as e:
                    logger.error(f"Error parsing clearkey_param: {e}")

            # --- Check for forced extension ---
            ext_param = "mp4" # Default to MP4/fMP4 since remuxing is disabled
            if "ext=ts" in params:
                 ext_param = "ts"
            
            if ext_param == "ts" and not server_side_decryption:
                 logger.debug(f"🔄 Concatenation requested (ext=ts)")
                 server_side_decryption = True
                 # Use dummy key/id to satisfy the endpoint requirement, and set skip_decrypt=1
                 decryption_params = "&key=00000000000000000000000000000000&key_id=00000000000000000000000000000000&skip_decrypt=1"

            # --- GESTIONE SEGMENTI ---
            # SegmentTemplate è il caso più comune per lo streaming live/vod moderno
            segment_template = representation.find('mpd:SegmentTemplate', self.ns)
            if segment_template is None:
                # Fallback: cerca nell'AdaptationSet
                segment_template = adaptation_set.find('mpd:SegmentTemplate', self.ns)
            
            if segment_template is not None:
                timescale = int(segment_template.get('timescale', '1'))
                presentation_time_offset = int(segment_template.get('presentationTimeOffset', '0'))
                initialization = segment_template.get('initialization')
                media = segment_template.get('media')
                start_number = int(segment_template.get('startNumber', '1'))
                
                # Risolvi URL base
                base_url_tag = root.find('mpd:BaseURL', self.ns)
                base_url = base_url_tag.text if base_url_tag is not None else os.path.dirname(original_url)
                if not base_url.endswith('/'): base_url += '/'

                # --- INITIALIZATION SEGMENT (EXT-X-MAP) ---
                encoded_init_url = ""
                # Get bandwidth from representation
                bandwidth = representation.get('bandwidth', '')
                
                if initialization:
                    # Processing initialization segment
                    init_url = self._expand_segment_template(
                        initialization, rep_id, bandwidth
                    )
                    full_init_url = urljoin(base_url, init_url)
                    encoded_init_url = urllib.parse.quote(full_init_url, safe='')
                    
                    header_params = self._extract_header_params(params)
                    if server_side_decryption:
                        proxy_init_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_init_url}&is_init=1{decryption_params}{media_type_param}{header_params}"
                    else:
                        proxy_init_url = f"{proxy_base}/segment/init.mp4?base_url={encoded_init_url}{media_type_param}{header_params}"
                    lines.append(f'#EXT-X-MAP:URI="{proxy_init_url}"')
                    lines[1] = '#EXT-X-VERSION:6'

                # --- SEGMENT TIMELINE ---
                segment_timeline = segment_template.find('mpd:SegmentTimeline', self.ns)
                if segment_timeline is not None:
                    # Prima raccogli tutti i segmenti
                    all_segments = []
                    current_time = 0
                    segment_number = start_number
                    
                    for s in segment_timeline.findall('mpd:S', self.ns):
                        t = s.get('t')
                        if t: current_time = int(t)
                        d = int(s.get('d'))
                        r = int(s.get('r', '0'))
                        
                        duration_sec = d / timescale
                        
                        # Ripeti per r + 1 volte
                        for _ in range(r + 1):
                            all_segments.append({
                                'time': current_time,
                                'number': segment_number,
                                'duration': duration_sec,
                                'd': d
                            })
                            current_time += d
                            segment_number += 1
                    
                    # Per LIVE: FILTRA solo gli ultimi N segmenti per forzare partenza dal live edge
                    # Questo è necessario perché molti player (Stremio, ExoPlayer) ignorano EXT-X-START
                    # Per VOD: prendi tutti normalmente
                    segments_to_use = all_segments
                    
                    if is_live and len(all_segments) > 0:
                        # Calculate global last time and global first time across all video and audio representations in this MPD XML
                        global_last_time_sec = 0.0
                        global_first_time_sec = 0.0
                        for period in root.findall('.//mpd:Period', self.ns):
                            for aset in period.findall('mpd:AdaptationSet', self.ns):
                                mime = aset.get('mimeType', '')
                                if not mime:
                                    rep = aset.find('mpd:Representation', self.ns)
                                    if rep is not None:
                                        mime = rep.get('mimeType', '')
                                if 'video' in mime or 'audio' in mime:
                                    template = aset.find('mpd:SegmentTemplate', self.ns)
                                    for r in aset.findall('mpd:Representation', self.ns):
                                        r_template = r.find('mpd:SegmentTemplate', self.ns) or template
                                        if r_template is not None:
                                            r_timescale = int(r_template.get('timescale', '1'))
                                            timeline = r_template.find('mpd:SegmentTimeline', self.ns)
                                            if timeline is not None:
                                                first_t = None
                                                current_t = None
                                                last_end = None
                                                for s in timeline.findall('mpd:S', self.ns):
                                                    t = s.get('t')
                                                    if t:
                                                        current_t = int(t)
                                                    elif current_t is None:
                                                        current_t = 0
                                                    if first_t is None:
                                                        first_t = current_t
                                                    d = int(s.get('d'))
                                                    r_rep = int(s.get('r', '0'))
                                                    last_end = current_t + d * (r_rep + 1)
                                                    current_t = last_end
                                                if first_t is not None:
                                                    first_seg_time_sec = first_t / r_timescale
                                                    if first_seg_time_sec > global_first_time_sec:
                                                        global_first_time_sec = first_seg_time_sec
                                                if last_end is not None:
                                                    last_seg_time_sec = last_end / r_timescale
                                                    if last_seg_time_sec > global_last_time_sec:
                                                        global_last_time_sec = last_seg_time_sec

                        # Fallback if global variables couldn't be calculated
                        if global_last_time_sec == 0.0:
                            global_last_time_sec = all_segments[-1]['time'] / timescale
                        if global_first_time_sec == 0.0:
                            global_first_time_sec = all_segments[0]['time'] / timescale

                        # Force monotonicity for the live edge timestamp to shield against CDN cache jitter.
                        # We use the base URL (without query params) as the unique stream key.
                        stream_key = original_url.split('?')[0]
                        if not hasattr(self.__class__, '_last_times'):
                            self.__class__._last_times = {}
                        
                        previous_max = self.__class__._last_times.get(stream_key, 0.0)
                        if 0.0 < previous_max - global_last_time_sec < 60.0:
                            # Clamp to previous maximum to keep window start monotonic
                            global_last_time_sec = previous_max
                        else:
                            # Update cache (or accept large resets)
                            self.__class__._last_times[stream_key] = global_last_time_sec

                        # Keep only segments starting within the last 12 seconds of the global live edge.
                        # Clamp window start to global_first_time_sec so we never request segments that don't exist in one of the tracks.
                        # Apply a 1.0 second tolerance (half of segment duration) to account for slight float alignment differences.
                        window_start_sec = max(global_last_time_sec - 30.0, global_first_time_sec)
                        segments_to_use = [seg for seg in all_segments if seg['time'] / timescale >= window_start_sec - 1.0]
                        if not segments_to_use:
                            segments_to_use = [all_segments[-1]]

                        first_window_seg = segments_to_use[0]
                        sequence_duration_units = self._nominal_segment_duration_units(
                            all_segments
                        )
                        sequence_time = first_window_seg['time'] - presentation_time_offset
                        sequence_preview = int(round(sequence_time / sequence_duration_units))
                        logger.debug(f"📐 [Window] rep={rep_id} edge={global_last_time_sec:.1f} first={global_first_time_sec:.1f} win={window_start_sec:.1f} segs={len(segments_to_use)} start_ts={segments_to_use[0]['time']/timescale:.1f} seq={sequence_preview}")

                        total_duration = sum(seg['duration'] for seg in segments_to_use)
                        
                        # Calcola TARGETDURATION dal segmento più lungo
                        max_duration = max(seg['duration'] for seg in segments_to_use)
                        
                        # MEDIA-SEQUENCE deve essere basato sul timestamp del primo segmento
                        # per garantire che quando il manifest viene ricaricato, il player
                        # sappia quali segmenti ha già scaricato e quali sono nuovi.
                        # 
                        # Per LIVE stream multi-key, calcoliamo la sequenza dal timestamp:
                        # sequence = first_segment_timestamp / segment_duration (in timescale units)
                        # Questo garantisce che video e audio abbiano lo stesso MEDIA-SEQUENCE
                        # anche se hanno timestamp leggermente diversi, perché usiamo il floor.
                        if len(segments_to_use) > 0:
                            first_seg = segments_to_use[0]
                            first_seg_time_sec = first_seg['time'] / timescale
                            # DASH live manifests may reset startNumber to 1
                            # on every rolling window.  Build a stable HLS
                            # sequence from media time and the nominal segment
                            # duration.  Do not use the first segment duration:
                            # audio timelines legitimately alternate by a few
                            # milliseconds and that made MEDIA-SEQUENCE jump.
                            duration_units = self._nominal_segment_duration_units(
                                all_segments
                            )
                            media_time = first_seg['time'] - presentation_time_offset
                            media_sequence = int(round(media_time / duration_units))

                            # A provider/CDN can briefly return an older rolling
                            # window.  HLS clients treat a backwards sequence as
                            # a playlist reset and may stop playback, so keep the
                            # sequence monotonic for this live representation.
                            sequence_key = f"{original_url.split('?')[0]}::{rep_id}"
                            if not hasattr(self.__class__, '_last_sequences'):
                                self.__class__._last_sequences = {}
                            previous_sequence = self.__class__._last_sequences.get(
                                sequence_key
                            )
                            if (
                                previous_sequence is not None
                                and media_sequence < previous_sequence
                            ):
                                media_sequence = previous_sequence
                            self.__class__._last_sequences[sequence_key] = media_sequence
                            while len(self.__class__._last_sequences) > 512:
                                self.__class__._last_sequences.pop(
                                    next(iter(self.__class__._last_sequences))
                                )
                            
                            lines.append(f'#EXT-X-TARGETDURATION:{int(max_duration) + 1}')
                            lines.append(f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}')
                    else:
                        # VOD: inizia da 0
                        # logger.info(f"🔵 VOD Mode: {len(segments_to_use)} segments")
                        if segments_to_use:
                            max_duration = max(seg['duration'] for seg in segments_to_use)
                            target_dur = int(max_duration) + 1
                        else:
                            target_dur = 10
                            
                        lines.append(f'#EXT-X-TARGETDURATION:{target_dur}')
                        lines.append('#EXT-X-MEDIA-SEQUENCE:0')
                    
                    for seg in segments_to_use:
                        # Costruisci URL segmento
                        seg_name = self._expand_segment_template(
                            media,
                            rep_id,
                            bandwidth,
                            number=seg['number'],
                            timestamp=seg['time'],
                        )
                        
                        full_seg_url = urljoin(base_url, seg_name)
                        encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                        
                        # Estrai solo il nome del file (senza query string) per il path del proxy
                        # Questo evita URL con doppio ? (es: /segment/file.mp4?z32=...?base_url=...)
                        seg_filename = seg_name.split('?')[0] if '?' in seg_name else seg_name
                        
                        lines.append(f'#EXTINF:{seg["duration"]:.3f},')
                        
                        # Estrai solo i parametri header dalla query string originale
                        header_params = self._extract_header_params(params)
                        
                        if server_side_decryption:
                            decrypt_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_seg_url}&init_url={encoded_init_url}&skip_init=1{decryption_params}{media_type_param}{header_params}"
                            lines.append(decrypt_url)
                        else:
                            proxy_seg_url = f"{proxy_base}/segment/{seg_filename}?base_url={encoded_seg_url}{media_type_param}{header_params}"
                            lines.append(proxy_seg_url)
                
                # --- SEGMENT TEMPLATE (DURATION) ---
                else:
                    duration = int(segment_template.get('duration', '0'))
                    total_segments = 100
                    duration_sec = 0
                    if duration > 0:
                        period = root.find('mpd:Period', self.ns)
                        period_duration_str = period.get('duration')
                        if period_duration_str:
                            import re as _re
                            m = _re.match(r'PT(\d+H)?(\d+M)?(\d+(?:\.\d+)?S)?', period_duration_str)
                            if m:
                                hours = int(m.group(1)[:-1]) if m.group(1) else 0
                                minutes = int(m.group(2)[:-1]) if m.group(2) else 0
                                seconds = float(m.group(3)[:-1]) if m.group(3) else 0
                                period_sec = hours * 3600 + minutes * 60 + seconds
                                duration_sec = duration / timescale
                                total_segments = max(1, int(period_sec / duration_sec)) if duration_sec > 0 else 100
                            else:
                                total_segments = 100
                        else:
                            total_segments = 100

                        duration_sec = duration / timescale

                    for i in range(total_segments):
                        seg_num = start_number + i
                        seg_name = self._expand_segment_template(
                            media,
                            rep_id,
                            bandwidth,
                            number=seg_num,
                            timestamp=seg_num,
                        )

                        full_seg_url = urljoin(base_url, seg_name)
                        encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                        header_params = self._extract_header_params(params)
                        orig_ext = os.path.splitext(seg_name.split('?')[0])[1] or '.m4s'
                        if server_side_decryption:
                            decrypt_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_seg_url}&init_url={encoded_init_url}&skip_init=1{decryption_params}{media_type_param}{header_params}"
                            seg_url = decrypt_url
                        else:
                            seg_url = f"{proxy_base}/segment/seg_{seg_num}{orig_ext}?base_url={encoded_seg_url}{media_type_param}{header_params}"

                        lines.append(f'#EXTINF:{duration_sec:.6f},')
                        lines.append(seg_url)

            # Per VOD aggiungi ENDLIST, per LIVE no (indica stream in corso)
            if not is_live:
                lines.append('#EXT-X-ENDLIST')
            
            # Unisci le righe
            playlist_content = '\n'.join(lines)
            # logger.info(f"📜 Generated playlist for rep_id={rep_id} (first 15 lines):\n{chr(10).join(lines[:15])}")
            # logger.info(f"📊 Total lines: {len(lines)}, Total segments: {len([l for l in lines if l.startswith('#EXTINF')])}")
            
            return playlist_content

        except Exception as e:
            logging.error(f"Error converting Media Playlist: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)
