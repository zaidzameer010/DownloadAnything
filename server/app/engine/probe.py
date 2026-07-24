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
    ) -> Dict[str, Any]:
        return probe_video(
            job_id,
            url,
            referer=referer,
            page_title=page_title,
            mime_hint=mime_hint,
            settings=settings,
        )


def probe_video(
    job_id: str,
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
    mime_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates probing by routing to the appropriate specialized extractor.

    Uses media_classify as the single source of truth. When the URL alone is
    unknown (no stream path tokens, no dedicated yt-dlp site), a cheap HEAD
    request supplies Content-Type so classification stays MIME-driven without
    extension allowlists.
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

    # yt-dlp supported sites, stream manifests, and unknown pages.
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
                f"probe_stream failed for direct file URL {redact_url(url)}: {exc}; "
                f"falling back to probe_direct_media."
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
