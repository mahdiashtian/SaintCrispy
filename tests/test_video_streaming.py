import asyncio
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors, functions, types
from video_fixture import box
from video_fixture import mp4_header as header_fixture

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.video import MAX_HEADER_BYTES, boxes, inspect_mp4, mp4_header
from downloader_bot.schemas.media import Media, Quality, Source, TelegramFile
from downloader_bot.services.download import DownloadService

QUALITY = Quality("original", "Original", "video", None, "mp4", "video/mp4", "progressive", "")
MEDIA = Media("sample", "id", "Public color test", "", 4, "page", None, (QUALITY,))
PEER = types.InputPeerUser(1, 2)


@pytest.fixture
async def video_origin(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    tail, fast = tmp_path / "tail.mp4", tmp_path / "fast.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=80x48:r=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "4",
        "-c:v",
        "libx264",
        "-g",
        "10",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(tail),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(tail),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(fast),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    files = {"/tail.mp4": tail.read_bytes(), "/fast.mp4": fast.read_bytes()}
    requests = []

    async def respond(reader, writer):
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
            method, path, _ = request.splitlines()[0].split()
            requests.append((method, path))
            data = files[path]
            start, stop, status = 0, len(data) - 1, "200 OK"
            match = re.search(r"(?im)^range: bytes=(\d+)-(\d*)", request)
            if match:
                start = int(match[1])
                stop = min(stop, int(match[2])) if match[2] else stop
                status = "206 Partial Content"
            headers = (
                f"HTTP/1.1 {status}\r\nConnection: close\r\nAccept-Ranges: bytes\r\n"
                f"Content-Type: video/mp4\r\nContent-Length: {stop - start + 1}\r\n"
            )
            if match:
                headers += f"Content-Range: bytes {start}-{stop}/{len(data)}\r\n"
            writer.write(headers.encode() + b"\r\n")
            if method != "HEAD":
                writer.write(data[start : stop + 1])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{port}", files=files, ffmpeg=ffmpeg, requests=requests
        )


class TelegramMock:
    def __init__(self, external=None):
        self.external = external
        self.parts = {}
        self.messages = []
        self.registered = []

    async def __call__(self, request):
        if isinstance(request, functions.messages.UploadMediaRequest):
            self.registered.append(request)
            if self.external is None:
                raise errors.WebpageCurlFailedError(request=None)
            return SimpleNamespace(document=self.external)
        self.parts[request.file_part] = request.bytes
        return True

    async def send_file(self, peer, media, **options):
        self.messages.append(media)
        attributes = getattr(media, "attributes", self.external.attributes if self.external else [])
        document = SimpleNamespace(
            id=10,
            access_hash=20,
            file_reference=b"ref",
            size=sum(map(len, self.parts.values())),
            attributes=attributes,
        )
        return SimpleNamespace(id=30, document=document)


async def decode(ffmpeg, data, *, one_frame=False):
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        *(["-frames:v", "1"] if one_frame else ["-map", "0:a:0"]),
        "-f",
        "null",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate(data)
    assert process.returncode == 0, diagnostic.decode(errors="replace")


@pytest.mark.parametrize("path", ["/fast.mp4", "/tail.mp4"])
async def test_real_video_with_unknown_dimensions_is_playable_before_full_download(
    video_origin, path
):
    origin = video_origin
    original = origin.files[path]
    complete, info = mp4_header(original)
    assert complete and (info is not None) == (path == "/fast.mp4")
    client = TelegramMock()
    progress = TransferProgress()
    async with httpx.AsyncClient(trust_env=False) as http:
        reference = await TelegramDelivery(
            client,
            http,
            origin.ffmpeg,
            max_file_bytes=1024 * 1024,
        ).new_file(
            PEER,
            MEDIA,
            QUALITY,
            Source(origin.url + path, "progressive", size_bytes=len(original)),
            progress,
        )
    assert len(client.messages) == 1 and reference.video_streaming is True
    media = client.messages[0]
    attribute = next(
        item for item in media.attributes if isinstance(item, types.DocumentAttributeVideo)
    )
    assert (attribute.w, attribute.h) == (80, 48) and attribute.supports_streaming
    assert attribute.video_codec == "h264" and attribute.preload_prefix_size > 0
    assert not attribute.nosound and media.nosound_video and not media.force_file
    data = b"".join(client.parts[index] for index in sorted(client.parts))
    assert mp4_header(data)[1] is not None
    await decode(origin.ffmpeg, data)
    if path == "/fast.mp4":
        assert data == original  # No remux or quality change for a ready source.
    else:
        assert data != original
        first_media_end = next(end for kind, _, end in boxes(data) if kind == b"mdat")
        assert first_media_end < len(data)
        await decode(origin.ffmpeg, data[:first_media_end], one_frame=True)


async def test_external_streamable_video_keeps_the_zero_local_download_path():
    document = SimpleNamespace(
        id=10,
        access_hash=20,
        file_reference=b"ref",
        size=1024,
        attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
    )
    client = TelegramMock(document)
    reference = await TelegramDelivery(client, None, "").new_file(
        PEER,
        MEDIA,
        QUALITY,
        Source("https://example.org/video.mp4", "progressive", size_bytes=1024),
    )
    assert reference.video_streaming is True and not client.parts
    assert len(client.messages) == len(client.registered) == 1


async def test_nonstreamable_external_document_is_not_published_and_is_repaired(video_origin):
    origin = video_origin
    document = SimpleNamespace(
        id=9,
        access_hash=20,
        file_reference=b"old",
        size=len(origin.files["/fast.mp4"]),
        attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=False)],
    )
    client = TelegramMock(document)
    async with httpx.AsyncClient(trust_env=False) as http:
        reference = await TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER,
            MEDIA,
            QUALITY,
            Source(origin.url + "/fast.mp4", "progressive"),
        )
    assert reference.document_id == 10 and reference.video_streaming is True
    assert len(client.messages) == 1 and isinstance(
        client.messages[0], types.InputMediaUploadedDocument
    )


async def test_old_nonstreamable_cache_is_replaced_once_then_reused(video_origin):
    origin = video_origin
    state = {"file": TelegramFile(1, 2, b"old", bytes(PEER), 3, 1024, False), "resolved": 0}
    client = TelegramMock()

    async def get(*args):
        return state["file"]

    async def save(*args):
        state["file"] = args[-1]

    async def resolve(*args):
        state["resolved"] += 1
        return Source(origin.url + "/fast.mp4", "progressive")

    async with httpx.AsyncClient(trust_env=False) as http:
        repository = SimpleNamespace(get=get, save=save, key=lambda *args: "id")
        service = DownloadService(
            SimpleNamespace(resolve=resolve),
            repository,
            TelegramDelivery(client, http, origin.ffmpeg),
        )
        assert await service.deliver(PEER, MEDIA, QUALITY) == "transferred"
        client.parts.clear()
        assert await service.deliver(PEER, MEDIA, QUALITY) == "reused"
    assert state["resolved"] == 1 and state["file"].video_streaming is True
    assert len(client.messages) == 2 and isinstance(client.messages[-1], types.InputMediaDocument)


async def test_legacy_ready_reference_is_not_trusted_from_its_duration_attribute():
    file = TelegramFile(1, 2, b"old", bytes(PEER), 3)

    async def get_messages(*args, **kwargs):
        return SimpleNamespace(
            document=SimpleNamespace(
                id=1,
                access_hash=2,
                file_reference=b"new",
                size=1024,
                attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
            )
        )

    delivery = TelegramDelivery(SimpleNamespace(get_messages=get_messages), None, "")
    assert await delivery.prepare_cached(file, QUALITY) is None


async def test_thousand_requests_share_one_legacy_video_repair_and_publish_concurrently():
    old = TelegramFile(1, 2, b"old", bytes(PEER), 3, 1024, False)
    fresh = replace(old, document_id=10, video_streaming=True, verified_complete=True)
    stored = old
    all_followers = asyncio.Event()
    active = peak = 0

    async def get(*args):
        return stored

    async def save(*args):
        nonlocal stored
        stored = args[-1]

    async def resend(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 999:
            all_followers.set()
        try:
            await asyncio.wait_for(all_followers.wait(), 3)
            return fresh
        finally:
            active -= 1

    delivery = SimpleNamespace(
        prepare_cached=AsyncMock(return_value=None),
        new_file=AsyncMock(return_value=fresh),
        resend=resend,
    )
    service = DownloadService(
        SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive"))),
        SimpleNamespace(key=lambda *args: "same", get=get, save=save),
        delivery,
        concurrency=1000,
        cached_concurrency=1000,
    )
    results = await asyncio.gather(*(service.deliver(PEER, MEDIA, QUALITY) for _ in range(1000)))
    assert results.count("transferred") == 1 and results.count("reused") == 999
    delivery.prepare_cached.assert_awaited_once_with(old, QUALITY)
    delivery.new_file.assert_awaited_once()
    assert stored == fresh and peak == 999 and not service._locks._entries


async def test_failed_legacy_repair_keeps_the_previous_file_and_quality():
    from downloader_bot.schemas.media import DownloadError

    old = TelegramFile(1, 2, b"old", bytes(PEER), 3, 1024, False)
    repository = SimpleNamespace(
        key=lambda *args: "same",
        get=AsyncMock(return_value=old),
        save=AsyncMock(),
    )
    delivery = SimpleNamespace(
        prepare_cached=AsyncMock(return_value=None),
        new_file=AsyncMock(side_effect=DownloadError("transfer incomplete")),
        resend=AsyncMock(),
    )
    downloader = SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive")))
    service = DownloadService(downloader, repository, delivery)
    with pytest.raises(DownloadError, match="transfer incomplete"):
        await service.deliver(PEER, MEDIA, QUALITY)
    downloader.resolve.assert_awaited_once_with(MEDIA, QUALITY)
    repository.save.assert_not_awaited()
    delivery.resend.assert_not_awaited()


async def test_header_inspection_cancellation_closes_source():
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def chunks():
        try:
            yield header_fixture()[:24]
            waiting.set()
            await asyncio.Future()
        finally:
            closed.set()

    async def inspect():
        async with inspect_mp4(chunks()):
            pytest.fail("Incomplete header must not be accepted")

    task = asyncio.create_task(inspect())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_header_prefix_is_bounded_and_reads_modern_codecs():
    for codec, expected in (
        (b"avc1", "h264"),
        (b"hvc1", "h265"),
        (b"av01", "av1"),
        (b"vp09", "vp9"),
    ):
        complete, info = mp4_header(header_fixture(80, 48, 4.25, codec))
        assert complete and info.codec == expected and info.duration == 4.25
    assert mp4_header(box(b"free", b"x" * MAX_HEADER_BYTES)) == (True, None)
