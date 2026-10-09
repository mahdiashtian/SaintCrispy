from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass

from downloader_bot.schemas.media import DownloadError
from downloader_bot.services.limits import check_file_size

MAX_HEADER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    codec: str | None
    has_audio: bool
    prefix_size: int


def boxes(data):
    """Read complete ISO BMFF boxes; malformed lengths never cause an unbounded loop."""
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = bytes(data[offset + 4 : offset + 8])
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                return
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header = 16
        if size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            return
        yield kind, data[offset + header : offset + size], offset + size
        offset += size


def child(data, name):
    return next((body for kind, body, _ in boxes(data) if kind == name), b"")


def video_info(moov, prefix_size: int) -> VideoInfo | None:
    tracks = [body for kind, body, _ in boxes(moov) if kind == b"trak"]
    has_audio = any(child(child(track, b"mdia"), b"hdlr")[8:12] == b"soun" for track in tracks)
    for track in tracks:
        mdia = child(track, b"mdia")
        if child(mdia, b"hdlr")[8:12] != b"vide":
            continue
        tkhd = child(track, b"tkhd")
        if len(tkhd) < 84:
            continue
        width = int.from_bytes(tkhd[-8:-4], "big") >> 16
        height = int.from_bytes(tkhd[-4:], "big") >> 16
        if not width or not height:
            continue
        # Quarter-turn display matrices swap the displayed dimensions.
        if tkhd[-44:-40] == b"\0" * 4 and tkhd[-28:-24] == b"\0" * 4:
            width, height = height, width
        duration = 0.0
        mdhd = child(mdia, b"mdhd")
        if len(mdhd) >= 24:
            start = 20 if mdhd[0] == 1 else 12
            length = 8 if mdhd[0] == 1 else 4
            if len(mdhd) < start + 4 + length:
                return None
            timescale = int.from_bytes(mdhd[start : start + 4], "big")
            ticks = int.from_bytes(mdhd[start + 4 : start + 4 + length], "big")
            if timescale and ticks != (1 << (length * 8)) - 1:
                duration = ticks / timescale
        stsd = child(child(child(mdia, b"minf"), b"stbl"), b"stsd")
        sample = next(boxes(stsd[8:]), None)
        codecs = {
            b"avc1": "h264",
            b"avc3": "h264",
            b"hvc1": "h265",
            b"hev1": "h265",
            b"av01": "av1",
        }
        codec = codecs.get(sample[0]) if sample else None
        return VideoInfo(width, height, duration, codec, has_audio, prefix_size)
    return None


def mp4_header(data) -> tuple[bool, VideoInfo | None]:
    for kind, body, end in boxes(data):
        if kind in {b"mdat", b"moof"}:
            return True, None  # Media before its index needs remuxing.
        if kind == b"moov":
            return True, video_info(body, end)
    # Detect an incomplete large media box without downloading it in full.
    offset = 0
    for _, _, end in boxes(data):
        offset = end
    if bytes(data[offset + 4 : offset + 8]) in {b"mdat", b"moof"}:
        return True, None
    return len(data) >= MAX_HEADER_BYTES, None


@asynccontextmanager
async def inspect_mp4(chunks, max_file_bytes: int = 0):
    """Peek at a bounded prefix and replay it; closing also closes HTTP or FFmpeg."""
    async with aclosing(chunks):
        prefix = bytearray()
        info = None
        async for chunk in chunks:
            check_file_size(len(prefix) + len(chunk), max_file_bytes)
            if len(prefix) + len(chunk) > MAX_HEADER_BYTES:
                break
            prefix.extend(chunk)
            complete, info = mp4_header(prefix)
            if complete:
                break

        async def replay():
            if prefix:
                yield bytes(prefix)
            async for chunk in chunks:
                yield chunk

        async with aclosing(replay()) as stream:
            yield stream, info


def require_video_info(info: VideoInfo | None) -> VideoInfo:
    if info is None:
        raise DownloadError(
            "آماده‌سازی ویدیو برای پخش هنگام دانلود کامل نشد؛ فایل ناقص ارسال نمی‌شود."
        )
    return info
