from typing import Any, Dict, Optional

from app.engine.media_classify import classify_url, is_direct_file_url
from app.engine.stream_extractor import probe_stream
from app.engine.direct_media import fetch_file_headers, probe_direct_media
from app.engine.probe_validation import ProbeFailure
from app.schemas.settings import AppSettings
from app.services.interfaces import IProbeEngine
from app.utils.logger import get_logger, redact_url

logger = get_logger(__name__)


class ProbeOrchestrator(IProbeEngine):
    """Routes a URL to the correct probe strategy."""

    def probe(
        self,
        job_id: str,
        url: str,
        settings: AppSettings,
        referer: Optional[str] = None,
        page_title: Optional[str] = None,
        mime_hint: Optional[str] = None,
        stream_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        return probe_video(
            job_id,
            url,
            referer=referer,
            page_title=page_title,
            mime_hint=mime_hint,
            settings=settings,
            stream_url=stream_url,
        )


def probe_video(
    job_id: str,
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
    mime_hint: Optional[str] = None,
    stream_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates probing by routing to the appropriate specialized extractor.

    Uses media_classify as the single source of truth. When the URL alone is
    unknown (no stream path tokens, no dedicated yt-dlp site), a cheap HEAD
    request supplies Content-Type so classification stays MIME-driven without
    extension allowlists.

    Fallback order:
      1. yt-dlp native probe (probe_stream).
      2. Stream extraction: a captured stream/manifest URL from the caller
         (e.g. a browser extension's m3u8 intercept).
      3. Generic extraction: direct file/media probe for URLs that look like
         blobs yt-dlp could not handle.
    """
    bucket = classify_url(url, mime=mime_hint)
    resolved_mime = mime_hint

    if bucket == "unknown":
        # MIME HEAD fallback — only when URL routing cannot decide.
        try:
            _size, mime, _ranges, final_url = fetch_file_headers(url, referer=referer)
            if mime:
                resolved_mime = mime
                bucket = classify_url(final_url or url, mime=mime)
                logger.debug(
                    f"Probe MIME HEAD for {redact_url(url)} → {mime} → {bucket}"
                )
        except Exception as exc:
            logger.debug(f"Probe MIME HEAD failed for {redact_url(url)}: {exc}")

    if bucket == "direct":
        try:
            return probe_direct_media(
                job_id=job_id,
                url=url,
                referer=referer,
                page_title=page_title,
                mime_hint=resolved_mime,
                settings=settings,
            )
        except ProbeFailure as pf:
            # Some URLs look like direct files but are actually site download
            # gateways that serve HTML. Give yt-dlp generic a chance before
            # bailing out.
            if pf.category == "no_media_found":
                logger.info(
                    f"Direct probe found no media for {redact_url(url)}; "
                    f"falling back to generic yt-dlp extractor."
                )
                try:
                    return probe_stream(
                        job_id=job_id,
                        url=url,
                        referer=referer,
                        page_title=page_title,
                        settings=settings,
                    )
                except Exception as exc:
                    if is_direct_file_url(url):
                        logger.info(
                            f"Fallback probe_stream failed for direct file URL {redact_url(url)}: {exc}; "
                            f"using probe_direct_media html fallback."
                        )
                        return probe_direct_media(
                            job_id=job_id,
                            url=url,
                            referer=referer,
                            page_title=page_title,
                            mime_hint=resolved_mime,
                            allow_html_fallback=True,
                            settings=settings,
                        )
                    raise
            raise

    # 1. yt-dlp native probe for supported sites, stream manifests, and pages.
    native_err: Optional[BaseException] = None
    try:
        return probe_stream(
            job_id=job_id,
            url=url,
            referer=referer,
            page_title=page_title,
            settings=settings,
        )
    except Exception as exc:
        native_err = exc
        logger.warning(
            f"yt-dlp native probe failed for {redact_url(url)}: {exc}"
        )

    # 2. Stream extraction fallback: the caller may have supplied a captured
    #    stream/manifest URL (e.g. from a browser extension's network observer).
    if stream_url and stream_url != url:
        try:
            info = probe_stream(
                job_id=job_id,
                url=stream_url,
                referer=referer,
                page_title=page_title,
                settings=settings,
            )
            # Preserve the original page URL for refresh/referer logic.
            if not info.get("page_url"):
                info["page_url"] = url
            return info
        except Exception as stream_err:
            logger.warning(
                f"Stream extraction fallback failed for {redact_url(stream_url)}: {stream_err}"
            )

    # 3. Generic extraction fallback: direct file/media probe. This catches
    #    blobs yt-dlp could not handle (e.g. unlabeled direct files) without
    #    misclassifying ordinary HTML pages as media.
    try:
        return probe_direct_media(
            job_id=job_id,
            url=url,
            referer=referer,
            page_title=page_title,
            mime_hint=resolved_mime,
            allow_html_fallback=False,
            settings=settings,
        )
    except Exception as generic_err:
        logger.warning(
            f"Generic extraction fallback failed for {redact_url(url)}: {generic_err}"
        )

    # Propagate the original yt-dlp failure; it is the most informative error.
    if native_err is not None:
        raise native_err
