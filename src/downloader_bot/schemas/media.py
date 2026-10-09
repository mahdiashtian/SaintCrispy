from dataclasses import dataclass, field


@dataclass(frozen=True)
class Quality:
    key: str
    label: str
    codec: str
    bitrate: int | None
    extension: str
    mime_type: str
    protocol: str
    endpoint: str
    original: bool = False
    fallback_endpoint: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None


@dataclass(frozen=True)
class Media:
    site: str
    content_id: str
    title: str
    artist: str
    duration: int
    page_url: str
    thumbnail: str | None
    qualities: tuple[Quality, ...]
    authorization: str | None = field(default=None, repr=False)
    account_id: str = "guest"
    requires_refresh: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True)
class Source:
    url: str = field(repr=False)
    protocol: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    audio_url: str | None = field(default=None, repr=False)
    audio_headers: dict[str, str] = field(default_factory=dict, repr=False)
    proxy: str | None = field(default=None, repr=False)
    size_bytes: int | None = None
    chunk_size: int | None = None
    require_audio: bool = False
    duration: float | None = None
    input_protocol: str | None = None
    audio_protocol: str | None = None


@dataclass(frozen=True)
class TelegramFile:
    document_id: int
    access_hash: int
    file_reference: bytes
    origin_peer: bytes
    message_id: int
    size_bytes: int | None = None
    video_streaming: bool | None = None


def streamable_video(quality: Quality) -> bool:
    return quality.mime_type.startswith("video/") and quality.extension.lower() == "mp4"


class DownloadError(Exception):
    """A recoverable failure with a safe, user-visible explanation."""

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message)
        self.code = code


class SiteHTTPError(DownloadError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
