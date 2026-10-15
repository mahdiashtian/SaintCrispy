import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors, functions, types
from video_fixture import box
from video_fixture import mp4_header as header_fixture

from bot.delivery import TelegramDelivery
from bot.progress import TransferProgress
from bot.transfers.video import MAX_HEADER_BYTES, boxes, inspect_mp4, mp4_header
from schemas.media import Media, Quality, Source, TelegramFile
from services.download import DownloadService

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
        self.uploaded = []
        self.thumbnails = []
        self.document = None
        self.remote_data = header_fixture()
        self.header_reads = 0

    @asynccontextmanager
    async def iter_download(self, document, **options):
        async def chunks():
            size = options["chunk_size"]
            for start in range(0, min(len(self.remote_data), options["limit"] * size), size):
                self.header_reads += 1
                yield self.remote_data[start : start + size]

        yield chunks()

    async def __call__(self, request):
        if isinstance(request, functions.messages.UploadMediaRequest):
            self.registered.append(request)
            if isinstance(request.media, types.InputMediaUploadedDocument):
                self.uploaded.append(request.media)
                self.document = SimpleNamespace(
                    id=10,
                    access_hash=20,
                    file_reference=b"ref",
                    size=sum(map(len, self.parts.values())),
                    mime_type=request.media.mime_type,
                    attributes=request.media.attributes,
                    thumbs=[types.PhotoSize("m", 80, 48, 1024)] if request.media.thumb else [],
                )
                return SimpleNamespace(document=self.document)
            if self.external is None:
                raise errors.WebpageCurlFailedError(request=None)
            return SimpleNamespace(document=self.external)
        if isinstance(request, functions.upload.SaveFilePartRequest):
            self.thumbnails.append(request.bytes)
            return True
        self.parts[request.file_part] = request.bytes
        return True

    async def send_file(self, peer, media, **options):
        self.messages.append(media)
        registered = self.document or self.external
        attributes = getattr(media, "attributes", registered.attributes if registered else [])
        document = SimpleNamespace(
            id=10,
            access_hash=20,
            file_reference=b"ref",
            size=sum(map(len, self.parts.values())),
            attributes=attributes,
            mime_type="video/mp4",
            thumbs=getattr(registered, "thumbs", []),
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
    assert isinstance(client.messages[0], types.InputMediaDocument)
    media = client.uploaded[0]
    attribute = next(
        item for item in media.attributes if isinstance(item, types.DocumentAttributeVideo)
    )
    assert (attribute.w, attribute.h) == (80, 48) and attribute.supports_streaming
    assert attribute.video_codec == "h264" and attribute.preload_prefix_size > 0
    assert not attribute.nosound and media.nosound_video and not media.force_file
    assert media.thumb and reference.video_preview is True
    assert client.thumbnails[0].startswith(b"\xff\xd8")
    data = b"".join(client.parts[index] for index in sorted(client.parts))
    assert mp4_header(data)[1] is not None
    await decode(origin.ffmpeg, data)
    if path == "/fast.mp4":
        assert data == original  # No remux or quality change for a ready source.
    else:
        assert data != original
        assert mp4_header(data)[1].faststart
        assert b"moof" not in [kind for kind, _, _ in boxes(data)]
    assert origin.requests.count(("GET", path)) == 1


async def test_large_index_finishes_upload_and_thumbnail_without_second_download(
    video_origin, tmp_path
):
    from bot.transfers.video import MAX_PREPARED_HEADER_BYTES

    origin = video_origin
    metadata, original, ready = (
        tmp_path / "metadata.txt",
        tmp_path / "original.mp4",
        tmp_path / "large.mp4",
    )
    metadata.write_text(
        ";FFMETADATA1\ncomment=" + "x" * (MAX_HEADER_BYTES + 8192), encoding="utf-8"
    )
    original.write_bytes(origin.files["/tail.mp4"])
    process = await asyncio.create_subprocess_exec(
        origin.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(original),
        "-f",
        "ffmetadata",
        "-i",
        str(metadata),
        "-map_metadata",
        "1",
        "-map",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(ready),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    data = ready.read_bytes()
    assert mp4_header(data[:MAX_HEADER_BYTES])[1] is None
    assert mp4_header(data, MAX_PREPARED_HEADER_BYTES)[1].prefix_size > MAX_HEADER_BYTES
    origin.files["/large.mp4"] = data
    client, progress = TelegramMock(), TransferProgress()
    async with httpx.AsyncClient(trust_env=False) as http:
        reference = await TelegramDelivery(
            client, http, origin.ffmpeg, max_file_bytes=4 * 1024 * 1024
        ).new_file(
            PEER,
            MEDIA,
            QUALITY,
            Source(origin.url + "/large.mp4", "progressive", require_audio=True, size_probed=True),
            progress,
        )
    assert reference.verified_complete and reference.video_faststart and reference.video_preview
    assert progress.upload_done and progress.uploaded > MAX_HEADER_BYTES
    assert client.thumbnails and len(client.messages) == 1
    assert origin.requests == [("GET", "/large.mp4")]


async def test_video_preparation_slot_is_released_before_slow_upload(video_origin):
    origin = video_origin
    started, release = asyncio.Event(), asyncio.Event()
    progress = TransferProgress()

    async def ack(request):
        started.set()
        await release.wait()
        return True

    async with httpx.AsyncClient(trust_env=False) as http:
        delivery = TelegramDelivery(None, http, origin.ffmpeg, remux_concurrency=1)
        delivery._upload_part = ack
        task = asyncio.create_task(
            delivery._upload(
                Source(origin.url + "/tail.mp4", "progressive"),
                QUALITY,
                "video.mp4",
                progress,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            assert not delivery._remux_slots.locked()
            assert progress.download_done
        finally:
            release.set()
            await task


async def test_external_streamable_video_keeps_the_zero_local_download_path():
    document = SimpleNamespace(
        id=10,
        access_hash=20,
        file_reference=b"ref",
        size=1024,
        attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
        thumbs=[types.PhotoSize("m", 80, 48, 1024)],
    )
    client = TelegramMock(document)
    reference = await TelegramDelivery(client, None, "").new_file(
        PEER,
        MEDIA,
        QUALITY,
        Source("https://example.org/video.mp4", "progressive", size_bytes=1024),
    )
    assert reference.video_streaming is True and not client.parts
    assert reference.video_faststart and client.header_reads == 1
    assert len(client.messages) == len(client.registered) == 1


@pytest.mark.parametrize("problem", ["fragmented", "unavailable"])
async def test_external_streaming_flags_do_not_certify_a_missing_classic_index(
    video_origin, problem
):
    origin = video_origin
    document = SimpleNamespace(
        id=9,
        access_hash=20,
        file_reference=b"old",
        size=len(origin.files["/fast.mp4"]),
        attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
        thumbs=[types.PhotoSize("m", 80, 48, 1024)],
    )
    client = TelegramMock(document)
    if problem == "fragmented":
        client.remote_data = b"".join(
            box(kind, bytes(body) + (box(b"mvex", b"") if kind == b"moov" else b""))
            for kind, body, _ in boxes(header_fixture())
        )
    else:

        @asynccontextmanager
        async def unsupported(*args, **kwargs):
            raise ValueError("Unsupported remote download")
            yield

        client.iter_download = unsupported
    async with httpx.AsyncClient(trust_env=False) as http:
        file = await TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER, MEDIA, QUALITY, Source(origin.url + "/fast.mp4", "progressive")
        )
    assert file.video_faststart and file.document_id == 10
    assert len(client.uploaded) == len(client.messages) == 1


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
    assert len(client.messages) == 1 and isinstance(client.messages[0], types.InputMediaDocument)
    assert len(client.uploaded) == 1 and client.uploaded[0].thumb


async def test_streamable_url_without_a_thumbnail_is_repaired_before_publication(video_origin):
    origin = video_origin
    document = SimpleNamespace(
        id=9,
        access_hash=20,
        file_reference=b"old",
        size=len(origin.files["/fast.mp4"]),
        attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
        thumbs=[],
    )
    client = TelegramMock(document)
    async with httpx.AsyncClient(trust_env=False) as http:
        file = await TelegramDelivery(client, http, origin.ffmpeg).new_file(
            PEER, MEDIA, QUALITY, Source(origin.url + "/fast.mp4", "progressive")
        )
    assert file.video_preview and file.document_id == 10
    assert len(client.uploaded) == len(client.messages) == 1


@pytest.mark.parametrize("thumbnail", [False, True])
async def test_legacy_verified_cache_checks_telegram_preview_once(video_origin, thumbnail):
    origin = video_origin
    old = TelegramFile(1, 2, b"old", bytes(PEER), 3, 1024, True, True)
    stored = old
    refreshes = []
    client = TelegramMock()

    async def get_messages(*args, **kwargs):
        refreshes.append(1)
        return SimpleNamespace(
            document=SimpleNamespace(
                id=1,
                access_hash=2,
                file_reference=b"new",
                size=1024,
                attributes=[types.DocumentAttributeVideo(4, 80, 48, supports_streaming=True)],
                thumbs=[types.PhotoSize("m", 80, 48, 1024)] if thumbnail else [],
            )
        )

    client.get_messages = get_messages

    async def get(*args):
        return stored

    async def save(*args):
        nonlocal stored
        stored = args[-1]

    provider = SimpleNamespace(
        resolve=AsyncMock(return_value=Source(origin.url + "/fast.mp4", "progressive"))
    )
    async with httpx.AsyncClient(trust_env=False) as http:
        service = DownloadService(
            provider,
            SimpleNamespace(key=lambda *args: "id", get=get, save=save),
            TelegramDelivery(client, http, origin.ffmpeg),
        )
        assert await service.deliver(PEER, MEDIA, QUALITY) == (
            "reused" if thumbnail else "transferred"
        )
        assert await service.deliver(PEER, MEDIA, QUALITY) == "reused"
    assert refreshes == [1] and provider.resolve.await_count == (0 if thumbnail else 1)
    assert stored.video_preview is True


@pytest.mark.parametrize("missing", ["video", "preview", "mime"])
async def test_rejected_server_video_registration_never_publishes(video_origin, missing):
    from schemas.media import DownloadError

    origin = video_origin

    class RejectedRegistration(TelegramMock):
        async def __call__(self, request):
            result = await super().__call__(request)
            if isinstance(request, functions.messages.UploadMediaRequest) and self.document:
                if missing == "video":
                    self.document.attributes = []
                elif missing == "preview":
                    self.document.thumbs = []
                else:
                    self.document.mime_type = "application/octet-stream"
            return result

    client = RejectedRegistration()
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(DownloadError) as failure:
            await TelegramDelivery(client, http, origin.ffmpeg).new_file(
                PEER, MEDIA, QUALITY, Source(origin.url + "/fast.mp4", "progressive")
            )
    assert failure.value.code == "video_registration_failed" and not client.messages


async def test_bounded_header_peek_replays_an_oversized_read_without_losing_bytes():
    data = header_fixture() + box(b"mdat", b"x" * (MAX_HEADER_BYTES + 17))

    async def chunks():
        yield data

    async with inspect_mp4(chunks()) as (stream, info):
        assert info and (info.width, info.height) == (32, 32)
        assert b"".join([part async for part in stream]) == data


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
    fresh = replace(
        old,
        document_id=10,
        video_streaming=True,
        verified_complete=True,
        video_preview=True,
        video_faststart=True,
    )
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
    from schemas.media import DownloadError

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
