"""Explicit, credential-free snapshots of immutable media metadata for persistence."""

from schemas.media import Media

QUALITY_FIELDS = (
    "key",
    "label",
    "codec",
    "bitrate",
    "extension",
    "mime_type",
    "protocol",
    "original",
    "width",
    "height",
    "duration",
)


def public_media(media: Media) -> dict:
    # Values are immutable scalars; recursive dataclass copies are unnecessary.
    return {
        "site": media.site,
        "content_id": media.content_id,
        "title": media.title,
        "artist": media.artist,
        "duration": media.duration,
        "page_url": media.page_url,
        "thumbnail": None,
        "account_id": media.account_id,
        "requires_refresh": True,
        "qualities": [
            {
                **{name: getattr(quality, name) for name in QUALITY_FIELDS},
                "endpoint": "",
                "fallback_endpoint": None,
            }
            for quality in media.qualities
        ],
    }
