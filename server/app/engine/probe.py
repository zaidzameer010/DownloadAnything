from typing import Any, Dict, List, Optional
import yt_dlp
from app.api.settings import load_settings
from app.utils.logger import logger

def probe_video(
    job_id: str,
    url: str
) -> Dict[str, Any]:
    """
    Synchronously extracts video info using yt-dlp.
    Natively pulls cookies from the user's selected browser profile.
    Should be called inside a threadpool.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "verbose": False,
        "ignoreconfig": True,
        "js_runtimes": {"node": {}, "bun": {}},
        "allow_unplayable_formats": True,
        "check_formats": "cached",
        "extractor_args": {
            "youtube": {
                "skip": ["translated_subs"],
            }
        }
    }
    
    settings_data = load_settings()
    browser = settings_data.cookiesFromBrowser
    
    if browser and browser.lower() != "none" and browser.lower() != "":
        logger.info(f"Using native cookies from browser: {browser}")
        # yt-dlp expects cookiesfrombrowser to be a tuple
        opts["cookiesfrombrowser"] = (browser.lower(),)

    logger.info(f"Probing URL: {url} for job: {job_id}")
    
    def determine_media_type(u: str, info_dict: Dict[str, Any]) -> str:
        lower_u = u.lower().split('?')[0]
        is_stream_u = any(lower_u.endswith(ext) for ext in [".m3u8", ".mpd"]) or "/manifest" in lower_u
        
        if is_stream_u:
            return "stream"
            
        formats_list = info_dict.get("formats", [])
        if formats_list and all(f.get("isStream", False) for f in formats_list):
            return "stream"
            
        has_vid = any((f.get("height") or 0) > 0 for f in formats_list)
        if not has_vid and formats_list:
            return "audio"
        return "video"

    try:
        # Mandatory with block to release YoutubeDL resources
        with yt_dlp.YoutubeDL(opts) as ydl:
            # extract_info is synchronous and blocking
            info = ydl.extract_info(url, download=False)
            # sanitize_info returns a json-serializable dict
            sanitized = ydl.sanitize_info(info)
            sanitized["mediaType"] = determine_media_type(url, sanitized)
            return sanitized
    except Exception as e:
        # If we failed with cookies, retry without cookies
        if "cookiesfrombrowser" in opts:
            logger.warning(f"Probe failed with native cookies ({browser}): {e}. Retrying without cookies...")
            clean_opts = opts.copy()
            clean_opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(clean_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    sanitized = ydl.sanitize_info(info)
                    sanitized["mediaType"] = determine_media_type(url, sanitized)
                    return sanitized
            except Exception as retry_err:
                logger.error(f"Retry probe without cookies also failed: {retry_err}")
                raise retry_err
        
        logger.error(f"Error during probe for URL {url}: {e}", exc_info=True)
        raise e
            
def determine_probe_error_suggestion(error_str: str) -> Optional[str]:
    """
    Analyzes yt-dlp error output to recommend actions.
    """
    err = error_str.lower()
    if "confirm you're not a bot" in err or "sign in" in err or "login" in err:
        return "cookies_required"
    if "po token" in err or "pot" in err or "token" in err:
        return "po_token_required"
    if "geo-restricted" in err or "geo blocked" in err or "not available in your country" in err:
        return "geo_blocked"
    return None

def is_natively_supported(url: str) -> bool:
    """
    Checks if yt-dlp has a native information extractor for the URL.
    This runs entirely locally and synchronously via regex matching.
    """
    try:
        with yt_dlp.YoutubeDL() as ydl:
            for ie in ydl._ies.values():
                if ie.__name__ != 'GenericIE' and ie.suitable(url):
                    return True
    except Exception:
        pass
    return False
