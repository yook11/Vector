"""URL decoder service — resolves Google News redirect URLs to real article URLs."""

import asyncio
from urllib.parse import urlparse

import structlog
from googlenewsdecoder import new_decoderv1

logger = structlog.get_logger(__name__)

# Default delay between decode requests (seconds) to avoid Google rate limiting.
# Each decode makes 2 HTTP requests to Google, so 0.5s ≈ max ~120 decodes/min.
DEFAULT_INTERVAL = 0.5


def is_google_news_url(url: str) -> bool:
    """Check if URL is a Google News redirect URL that needs decoding."""
    return urlparse(url).netloc == "news.google.com"


def _decode_single(url: str, interval: float | None) -> dict:
    """Synchronous decode wrapper (run via asyncio.to_thread)."""
    return new_decoderv1(url, interval=interval)


async def decode_urls(
    urls: list[str],
    interval: float = DEFAULT_INTERVAL,
) -> dict[str, str]:
    """Decode Google News URLs to their real article URLs.

    Returns a mapping of original_url -> decoded_url.
    Non-Google-News URLs are passed through unchanged.
    Failed decodings fall back to the original URL.

    Args:
        urls: List of URLs to decode.
        interval: Seconds to wait between decode requests (rate limiting).

    Returns:
        Dict mapping each input URL to its decoded (or original) URL.
    """
    result: dict[str, str] = {}
    google_urls: list[str] = []

    for url in urls:
        if is_google_news_url(url):
            google_urls.append(url)
        else:
            result[url] = url

    if not google_urls:
        return result

    decoded_count = 0
    failed_count = 0

    for url in google_urls:
        resp = await asyncio.to_thread(_decode_single, url, interval)
        if resp.get("status"):
            result[url] = resp["decoded_url"]
            decoded_count += 1
        else:
            logger.warning(
                "url_decode_failed",
                url=url,
                message=resp.get("message", "unknown"),
            )
            result[url] = url  # fallback to original
            failed_count += 1

    logger.info(
        "url_decode_completed",
        total=len(google_urls),
        decoded=decoded_count,
        failed=failed_count,
    )
    return result
