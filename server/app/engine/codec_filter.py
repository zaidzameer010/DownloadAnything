from typing import Any, Dict, List, Optional, Tuple
from app.schemas.formats import FormatSummary
from app.utils.logger import logger

def get_vcodec_family(vcodec: Optional[str]) -> str:
    if not vcodec or vcodec.lower() == "none":
        return "none"
    vcodec = vcodec.lower()
    if vcodec.startswith("av01"):
        return "av1"
    if vcodec.startswith("vp09") or vcodec.startswith("vp9"):
        return "vp9"
    if vcodec.startswith("avc1") or vcodec.startswith("h264") or vcodec.startswith("h.264"):
        return "avc"
    if vcodec.startswith("h265") or vcodec.startswith("hev1") or vcodec.startswith("hevc") or vcodec.startswith("h.265"):
        return "hevc"
    return "other"

def get_acodec_pref(acodec: Optional[str]) -> int:
    if not acodec or acodec.lower() == "none":
        return -1
    acodec = acodec.lower()
    if "opus" in acodec:
        return 4
    if "aac" in acodec or "mp4a" in acodec:
        return 3
    if "mp3" in acodec:
        return 2
    return 1

def format_size(bytes_val: Optional[float]) -> str:
    if not bytes_val:
        return "Unknown size"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"

def is_original_audio(fmt: Dict[str, Any]) -> bool:
    """
    Returns True if the format is the original audio track, False if it is a translated/dubbed track.
    yt-dlp sets language_preference to negative values (typically -1) for translation dubs on YouTube.
    """
    pref = fmt.get("language_preference")
    if pref is not None:
        try:
            if int(pref) < 0:
                return False
        except (ValueError, TypeError):
            pass
    return True

def get_audio_language_preference(fmt: Dict[str, Any]) -> int:
    """
    Returns the language preference score. Higher is more preferred.
    Original/default tracks typically have positive preference (e.g. 10).
    """
    pref = fmt.get("language_preference")
    if pref is not None:
        try:
            return int(pref)
        except (ValueError, TypeError):
            pass
    note = (fmt.get("format_note") or "").lower()
    if "original" in note or "default" in note:
        return 10
    return 0

def is_stream_format(fmt: Dict[str, Any]) -> bool:
    protocol = (fmt.get("protocol") or "").lower()
    return any(p in protocol for p in ["m3u8", "dash", "http_dash_segments", "hls"])

def get_stream_type(fmt: Dict[str, Any]) -> Optional[str]:
    protocol = (fmt.get("protocol") or "").lower()
    if "m3u8" in protocol or "hls" in protocol:
        return "hls"
    if "dash" in protocol:
        return "dash"
    return None

def is_adaptive_only(formats: List[Dict[str, Any]]) -> bool:
    if not formats:
        return False
    non_stream_found = False
    for f in formats:
        fid = f.get("format_id")
        if not fid:
            continue
        if not is_stream_format(f):
            non_stream_found = True
            break
    return not non_stream_found

def _summarize_audio_formats(
    audio_only: List[Dict[str, Any]], 
    duration: Optional[float] = None
) -> List[FormatSummary]:
    if not audio_only:
        return []
    
    # Filter to keep only original audio tracks
    original_audio = [f for f in audio_only if is_original_audio(f)]
    if not original_audio:
        original_audio = audio_only

    audio_only_sorted = sorted(
        original_audio,
        key=lambda x: (
            get_audio_language_preference(x),
            get_acodec_pref(x.get("acodec")),
            x.get("abr") or x.get("tbr") or 0,
            x.get("filesize") or x.get("filesize_approx") or 0
        ),
        reverse=True
    )
    
    summaries = []
    for f in audio_only_sorted:
        fid = f["format_id"]
        acodec = f.get("acodec", "audio")
        ext = f.get("ext", "webm")
        abr = f.get("abr") or f.get("tbr") or 0
        
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and abr and duration:
            size = int(abr * 1000 * duration / 8)
        
        is_stream = is_stream_format(f)
        stream_type = get_stream_type(f)
        stream_suffix = f" · {stream_type.upper()}" if stream_type else ""
        
        size_str = format_size(size) if size else "Unknown size"
        label = f"Audio ({acodec}) · {int(abr)} kbps · ~{size_str}{stream_suffix}" if abr else f"Audio ({acodec}) · ~{size_str}{stream_suffix}"
        
        summaries.append(
            FormatSummary(
                label=label,
                height=0,
                fps=0,
                codecFamily=acodec,
                ext=ext,
                tbr=abr or None,
                estSizeBytes=size,
                formatId=fid,
                isCombined=False,
                hdr=False,
                videoEstSizeBytes=None,
                audioEstSizeBytes=size,
                isStream=is_stream,
                streamType=stream_type
            )
        )
    return summaries

def filter_and_summarize_formats(
    formats: List[Dict[str, Any]], 
    duration: Optional[float] = None
) -> List[FormatSummary]:
    if not formats:
        return []

    # 1. Partition formats
    video_only: List[Dict[str, Any]] = []
    audio_only: List[Dict[str, Any]] = []
    combined: List[Dict[str, Any]] = []

    for f in formats:
        fid = f.get("format_id")
        if not fid:
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        height = f.get("height") or 0
        is_stream = is_stream_format(f)
        
        has_video = (vcodec != "none" and vcodec is not None) or height > 0
        has_audio = (acodec != "none" and acodec is not None) or (height > 0 and not is_stream and acodec != "none")

        if has_video and not has_audio:
            video_only.append(f)
        elif has_audio and not has_video:
            audio_only.append(f)
        elif has_video and has_audio:
            combined.append(f)

    logger.debug(f"Partitioned: {len(video_only)} video-only, {len(audio_only)} audio-only, {len(combined)} combined")

    # 2. Determine dominant family across all video-bearing formats
    video_bearing = video_only + combined
    families_present = set()
    for f in video_bearing:
        families_present.add(get_vcodec_family(f.get("vcodec")))

    dominant_family = "none"
    if "av1" in families_present:
        dominant_family = "av1"
    elif "vp9" in families_present:
        dominant_family = "vp9"
    elif "avc" in families_present:
        dominant_family = "avc"
    elif "hevc" in families_present:
        dominant_family = "hevc"
    elif families_present:
        dominant_family = "other"

    logger.debug(f"Dominant video codec family determined: {dominant_family}")

    # If media is audio-only
    if dominant_family == "none":
        return _summarize_audio_formats(audio_only, duration)

    # 3. Filter video-bearing formats to keep only dominant family (or unspecified codec)
    filtered_video = [
        f for f in video_bearing 
        if get_vcodec_family(f.get("vcodec")) == dominant_family or get_vcodec_family(f.get("vcodec")) == "none"
    ]

    # Find the best audio stream to pair with video-only streams
    best_audio = None
    if audio_only:
        # Filter to keep only original audio tracks
        original_audio = [f for f in audio_only if is_original_audio(f)]
        if not original_audio:
            original_audio = audio_only

        # Sort audio-only descending by quality/language preference
        audio_only_sorted = sorted(
            original_audio,
            key=lambda x: (
                get_audio_language_preference(x),
                get_acodec_pref(x.get("acodec")),
                x.get("abr") or x.get("tbr") or 0,
                x.get("filesize") or x.get("filesize_approx") or 0
            ),
            reverse=True
        )
        best_audio = audio_only_sorted[0]

    # Group video formats by height only
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for f in filtered_video:
        height = f.get("height") or 0
        buckets.setdefault(height, []).append(f)

    summaries = []
    
    # 4. For each bucket, pick the single best format
    for height, formats_in_bucket in buckets.items():
        # Separate video-only and combined in the bucket
        bucket_video_only = [f for f in formats_in_bucket if f in video_only]
        bucket_combined = [f for f in formats_in_bucket if f in combined]

        chosen_format = None
        is_combined_format = False
        audio_pair = None

        if bucket_video_only and best_audio:
            # Sort by progressive preference, then total/video bitrate descending
            bucket_video_only.sort(
                key=lambda x: (
                    not is_stream_format(x),
                    x.get("vbr") or x.get("tbr") or 0,
                    x.get("filesize") or x.get("filesize_approx") or 0
                ),
                reverse=True
            )
            chosen_format = bucket_video_only[0]
            audio_pair = best_audio
            is_combined_format = False
        elif bucket_combined:
            bucket_combined.sort(
                key=lambda x: (
                    not is_stream_format(x),
                    x.get("tbr") or x.get("vbr") or 0,
                    x.get("filesize") or x.get("filesize_approx") or 0
                ),
                reverse=True
            )
            chosen_format = bucket_combined[0]
            is_combined_format = True
        elif bucket_video_only:
            # No audio available, pick video only
            bucket_video_only.sort(
                key=lambda x: (
                    not is_stream_format(x),
                    x.get("vbr") or x.get("tbr") or 0,
                    x.get("filesize") or x.get("filesize_approx") or 0
                ),
                reverse=True
            )
            chosen_format = bucket_video_only[0]
            is_combined_format = False

        if not chosen_format:
            continue

        # Determine fps to show in the label
        fps = chosen_format.get("fps") or 30
        try:
            fps = int(round(float(fps)))
            if fps > 240 or fps <= 0:
                fps = 30
        except (ValueError, TypeError):
            fps = 30

        # Build ID
        if audio_pair and not is_combined_format:
            format_id = f"{chosen_format['format_id']}+{audio_pair['format_id']}"
        else:
            format_id = chosen_format["format_id"]

        # Calculate estimated size
        size = None
        video_size = chosen_format.get("filesize") or chosen_format.get("filesize_approx")
        if not video_size and duration:
            vbr = chosen_format.get("vbr") or chosen_format.get("tbr") or 0
            if vbr > 0:
                video_size = int(vbr * 1000 * duration / 8)

        audio_size = None
        if audio_pair and not is_combined_format:
            audio_size = audio_pair.get("filesize") or audio_pair.get("filesize_approx")
            if not audio_size and duration:
                abr = audio_pair.get("abr") or audio_pair.get("tbr") or 0
                if abr > 0:
                    audio_size = int(abr * 1000 * duration / 8)
            
            if video_size and audio_size:
                size = video_size + audio_size
            elif video_size:
                size = video_size
        else:
            size = video_size

        is_stream = is_stream_format(chosen_format) or (audio_pair is not None and is_stream_format(audio_pair))
        stream_type = get_stream_type(chosen_format) or (audio_pair and get_stream_type(audio_pair) or None)
        stream_suffix = f" · {stream_type.upper()}" if stream_type else ""

        size_str = f" · ~{format_size(size)}" if size else ""

        # Flags: HDR, etc.
        format_note = (chosen_format.get("format_note") or "").upper()
        hdr = "HDR" in format_note or "PQ" in format_note or "HLG" in format_note
        hdr_str = " · HDR" if hdr else ""

        codec_family_label = dominant_family.upper()
        ext = chosen_format.get("ext", "mp4") if is_combined_format else "mkv"  # paired usually merges to mkv

        label = f"{height}p{fps} · {codec_family_label}{hdr_str}{size_str}{stream_suffix}"
        
        summaries.append(
            FormatSummary(
                label=label,
                height=height,
                fps=fps,
                codecFamily=dominant_family,
                ext=ext,
                tbr=chosen_format.get("tbr") or None,
                estSizeBytes=size,
                formatId=format_id,
                isCombined=is_combined_format,
                hdr=hdr,
                videoEstSizeBytes=video_size,
                audioEstSizeBytes=audio_size if (audio_pair and not is_combined_format) else None,
                isStream=is_stream,
                streamType=stream_type
            )
        )
    # Sort final summaries descending by height
    summaries.sort(key=lambda x: x.height, reverse=True)
    return summaries
