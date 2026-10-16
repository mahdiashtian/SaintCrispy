"""Spotify publication, expired Telegram anchors and file-only expiry regressions."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors, functions, types, utils
from test_file_expiry import cleanup_fixture
from test_spotify_downloader import PAGE, media

from bot.delivery import TelegramDelivery
from bot.handlers.quality import run_transfer
from bot.progress import TransferProgress
from core.request_context import user_request
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader
from schemas.media import CachedFileUnavailable, DownloadError, Source, TelegramFile
from services.download import DownloadService

PEER = types.InputPeerUser(7, 123)
STALE = TelegramFile(1, 2, b"old", bytes(PEER), 901, 100, verified_complete=True)
FRESH = replace(STALE, document_id=3, file_reference=b"fresh", message_id=902)


@pytest.mark.parametrize("editable", [True, False])
async def test_reported_no_matching_audio_finishes_with_visible_error_and_no_file_send(editable):
    value = media()
    provider_client = SimpleNamespace(
        timeout=8,
        search_timeout=20,
        track=AsyncMock(return_value=value),
        audio=AsyncMock(
            side_effect=DownloadError("No matching audio", code="spotify_no_matching_audio")
        ),
    )
    delivery = SimpleNamespace(new_file=AsyncMock(), resend=AsyncMock())
    service = DownloadService(SpotifyDownloader(provider_client), file_repository(), delivery)
    visible = []

    async def edit(text, **options):
        if not editable or options.get("buttons", "missing") is not None:
            raise errors.ReplyMarkupInvalidError(None)
        visible.append(text)

    async def respond(text, **options):
        assert options["reply_to"] == 42
        visible.append(text)

    event = SimpleNamespace(
        sender_id=7,
        chat_id=7,
        get_input_chat=AsyncMock(return_value=PEER),
        respond=AsyncMock(side_effect=respond),
    )
    status = SimpleNamespace(edit=edit, delete=AsyncMock())
    progress = TransferProgress()
    await run_transfer(
        event, service, SimpleNamespace(media=value, message_id=42), 0, status, progress
    )
    assert progress.phase == "error" and visible == [progress.text()]
    assert "No matching audio" in visible[0]
    assert event.respond.await_count == int(not editable)
    assert status.delete.await_count == int(not editable)
    delivery.new_file.assert_not_awaited()
    delivery.resend.assert_not_awaited()


def file_repository(initial=None):
    state = [initial]

    async def save(site, identity, quality, file):
        state[0] = file

    async def forget(peer, message_id):
        if state[0] and (state[0].origin_peer, state[0].message_id) == (peer, message_id):
            state[0] = None

    return SimpleNamespace(
        key=lambda *args: "spotify-track",
        get=AsyncMock(side_effect=lambda *args: state[0]),
        save=AsyncMock(side_effect=save),
        forget_origin=AsyncMock(side_effect=forget),
        record_delivery=AsyncMock(),
    )


@pytest.mark.parametrize("concurrency", [1, 20])
@pytest.mark.parametrize("origin_present", [False, True])
async def test_missing_expired_cache_anchor_is_rebuilt_once_without_a_failed_request(
    concurrency, origin_present
):
    waiting, arrived = asyncio.Event(), 0

    async def send(peer, file, **options):
        nonlocal arrived
        if file.id.id == STALE.document_id:
            arrived += 1
            if arrived >= concurrency:
                waiting.set()
            await asyncio.wait_for(waiting.wait(), 2)
            raise errors.FileReferenceExpiredError(None)
        return SimpleNamespace(id=903)

    origin = SimpleNamespace(
        document=SimpleNamespace(id=STALE.document_id, access_hash=2, file_reference=b"old")
    )
    client = SimpleNamespace(
        send_file=send, get_messages=AsyncMock(return_value=origin if origin_present else None)
    )
    delivery = TelegramDelivery(client, None, "ffmpeg")
    delivery.new_file = AsyncMock(return_value=FRESH)
    repository = file_repository(STALE)
    provider = SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive")))
    service = DownloadService(provider, repository, delivery, cached_concurrency=concurrency)
    value = media()
    results = await asyncio.gather(
        *(service.deliver(PEER, value, value.qualities[0]) for _ in range(concurrency))
    )
    assert results.count("transferred") == 1
    assert results.count("reused") == concurrency - 1
    provider.resolve.assert_awaited_once()
    delivery.new_file.assert_awaited_once()
    assert await repository.get("spotify", value.content_id, value.qualities[0].key) == FRESH


async def test_repair_is_bounded_when_the_storage_copy_cannot_be_resent_either():
    repository = file_repository(STALE)
    provider = SimpleNamespace(resolve=AsyncMock(return_value=Source("url", "progressive")))
    delivery = SimpleNamespace(
        new_file=AsyncMock(return_value=FRESH),
        resend=AsyncMock(side_effect=CachedFileUnavailable()),
    )
    service = DownloadService(provider, repository, delivery, storage_peer="storage")
    value = media()
    with pytest.raises(CachedFileUnavailable):
        await service.deliver(PEER, value, value.qualities[0])
    assert delivery.resend.await_count == 2
    delivery.new_file.assert_awaited_once()
    repository.forget_origin.assert_awaited_once_with(STALE.origin_peer, STALE.message_id)


@pytest.mark.parametrize("failure", [TimeoutError(), errors.ChatWriteForbiddenError(None)])
async def test_ambiguous_or_permission_send_failure_does_not_redownload_or_resend(failure):
    client = SimpleNamespace(send_file=AsyncMock(side_effect=failure))
    delivery = TelegramDelivery(client, None, "ffmpeg")
    delivery.new_file = AsyncMock()
    repository = file_repository(STALE)
    provider = SimpleNamespace(resolve=AsyncMock())
    service = DownloadService(provider, repository, delivery)
    value = media()
    with pytest.raises(type(failure)):
        await service.deliver(PEER, value, value.qualities[0])
    client.send_file.assert_awaited_once()
    provider.resolve.assert_not_awaited()
    delivery.new_file.assert_not_awaited()
    repository.forget_origin.assert_not_awaited()


@pytest.fixture
async def mp3_origin(tmp_path):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    path = tmp_path / "audio.mp3"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "4",
        "-b:a",
        "128k",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, diagnostic = await process.communicate()
    assert process.returncode == 0, diagnostic.decode(errors="replace")
    data = path.read_bytes()

    async def respond(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        yield ffmpeg, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/audio.mp3", data


@pytest.mark.parametrize("peer", [PEER, types.InputPeerChannel(123, 456)])
@pytest.mark.parametrize("external", ["full", "rejected", "preview"])
async def test_full_spotify_pipeline_publishes_once_and_keeps_each_file_for_thirty_seconds(
    mp3_origin, peer, external
):
    ffmpeg, url, data = mp3_origin
    value = media()
    value = replace(value, duration=4, qualities=(replace(value.qualities[0], duration=4),))
    clock, deadlines, deletion, _, expiry = cleanup_fixture()
    parts, publications = [], []
    document = SimpleNamespace(
        id=10,
        access_hash=20,
        file_reference=b"audio",
        size=len(data),
        attributes=[types.DocumentAttributeAudio(1 if external == "preview" else 4)],
        mime_type="audio/mpeg",
    )

    class Client:
        async def __call__(self, request):
            if isinstance(request, functions.messages.UploadMediaRequest):
                if external == "rejected":
                    raise errors.WebpageCurlFailedError(None)
                return SimpleNamespace(document=document)
            assert isinstance(request, functions.upload.SaveBigFilePartRequest)
            parts.append(request.bytes)
            return True

        async def send_file(self, target, file, **options):
            assert options["reply_to"] == 42
            publications.append((target, file, options))
            return SimpleNamespace(id=900 + len(publications), document=document)

    source = Source(url, "progressive", proxy="", duration=4, size_bytes=len(data), chunk_size=4096)
    provider_client = SimpleNamespace(
        timeout=8,
        search_timeout=20,
        track=AsyncMock(return_value=value),
        audio=AsyncMock(return_value=source),
    )
    provider = SpotifyDownloader(provider_client)
    repository = file_repository()
    async with httpx.AsyncClient(trust_env=False) as http:
        delivery = TelegramDelivery(Client(), http, ffmpeg, file_expiry=expiry)
        service = DownloadService(provider, repository, delivery)
        selected = await provider.inspect(PAGE)
        with user_request(7, utils.get_peer_id(peer), message_id=42):
            assert (
                await service.deliver(peer, selected, selected.qualities[0], TransferProgress())
                == "transferred"
            )
        assert len(publications) == 1
        assert bool(parts) == (external != "full")
        assert set(deadlines.rows) == {(utils.get_peer_id(peer), 901)}
        clock[0] += timedelta(seconds=29)
        await expiry.run_once()
        deletion.delete_messages.assert_not_awaited()
        with user_request(7, utils.get_peer_id(peer), message_id=42):
            assert (
                await service.deliver(peer, selected, selected.qualities[0], TransferProgress())
                == "reused"
            )
        assert len(publications) == 2
        clock[0] += timedelta(seconds=1)
        await expiry.run_once()
        deletion.delete_messages.assert_awaited_once_with(peer, [901], revoke=True)
        assert not deadlines.rows[utils.get_peer_id(peer), 902]["completed"]
        assert repository.record_delivery.await_count == 2
        provider_client.audio.assert_awaited_once()
