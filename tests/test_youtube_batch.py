"""YouTube queue isolation, actual operation deadlines and bounded discovery work."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from downloader_bot.downloaders.youtube import client as module
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.downloaders.youtube.parser import WatchMetadata, public_renditions
from schemas.media import DownloadError


async def test_recent_native_deadline_does_not_spawn_one_worker_per_failed_public_follower(
    monkeypatch,
):
    client = YouTubeClient(resolver_url="https://cnv.cx")

    async def unavailable(self, identity):
        raise DownloadError("Public failed", code="youtube_public_unavailable")

    monkeypatch.setattr(YouTubeClient, "public_inspect", unavailable)
    provider = YouTubeDownloader(client)
    provider._public_preferred_until = time.monotonic() + 300
    native = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(provider, "_inspect", native)
    for _ in range(1000):
        with pytest.raises(DownloadError) as failure:
            await provider.inspect("https://youtu.be/jNQXAC9IVRw")
        assert failure.value.code == "youtube_public_unavailable"
    assert native.await_count == 1
    provider._native_stalled["jNQXAC9IVRw"] = time.monotonic() - 1
    provider._public_preferred_until = time.monotonic() + 300
    with pytest.raises(DownloadError):
        await provider.inspect("https://youtu.be/jNQXAC9IVRw")
    assert native.await_count == 2
    # A timeout for this video must not suppress a different video's native route.
    provider._public_preferred_until = time.monotonic() + 300
    with pytest.raises(DownloadError):
        await provider.inspect("https://youtu.be/l6zflbTGNFQ")
    assert native.await_count == 3
    for index in range(200):
        provider._defer_native(str(index))
    assert len(provider._native_stalled) == 128


@pytest.mark.parametrize(
    "configuration", [{"player_clients": "web_safari"}, {"pot_base_url": "http://127.0.0.1:4416"}]
)
async def test_explicit_native_configuration_keeps_its_recovery_despite_guest_backoff(
    monkeypatch, configuration
):
    client = YouTubeClient(resolver_url="https://cnv.cx", **configuration)

    async def unavailable(self, identity):
        raise DownloadError(code="youtube_public_unavailable")

    monkeypatch.setattr(YouTubeClient, "public_inspect", unavailable)
    provider = YouTubeDownloader(client)
    provider._public_preferred_until = time.monotonic() + 300
    provider._native_stalled["jNQXAC9IVRw"] = time.monotonic() + 15
    native = AsyncMock(return_value="verified-native-media")
    monkeypatch.setattr(provider, "_inspect", native)
    assert await provider.inspect("https://youtu.be/jNQXAC9IVRw") == "verified-native-media"
    assert native.await_count == 1 and not provider._native_stalled


async def test_public_transport_exposes_only_phase_timings_and_preserves_stream():
    infos = dict(zip(module.PUBLIC_TIMINGS, (0.1, 0.3, 0.6, 2.0), strict=True))

    class Response:
        status_code = 200
        headers = httpx.Headers()

        async def aiter_content(self):
            yield b"{}"

        async def aclose(self):
            pass

    class Browser:
        async def request(self, *args, **options):
            result = Response()
            result.infos = infos
            return result

    async with httpx.AsyncClient(transport=module.PublicTransport(Browser())) as client:
        response = await client.get("https://example.org/private-query")
    assert response.content == b"{}"
    assert response.extensions["youtube_timings"] == {
        "dns_seconds": 0.1,
        "tcp_seconds": 0.2,
        "tls_seconds": 0.3,
        "first_byte_wait_seconds": 1.4,
    }


@pytest.mark.parametrize(
    "code,reason",
    [(6, "dns"), (7, "tcp_connect"), (28, "transport_timeout"), (35, "tls"), (60, "certificate")],
)
def test_transport_classification_uses_codes_without_exposing_exception_text(code, reason):
    cause = RuntimeError("signed-url-and-private-key")
    cause.code = code
    error = httpx.ConnectError("private-url")
    error.__cause__ = cause
    fields = module.public_failure_fields(error)
    assert fields == {
        "error_type": "ConnectError",
        "transport_code": code,
        "network_reason": reason,
    }
    assert "private" not in str(fields)


@pytest.mark.parametrize(
    "height,video_requests", [(240, 1), (360, 1), (480, 2), (720, 2), (1080, 3), (2160, 3)]
)
def test_complete_low_resolution_catalog_omits_duplicate_conversion_ceilings(
    height, video_requests
):
    catalog = WatchMetadata("Title", "Author", 19, True, ((height * 16 // 9, height),), True)
    choices = public_renditions(catalog)
    assert len(choices) == video_requests + 1
    assert choices[-1] == ("mp3", "128")


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        WatchMetadata("T", "A", 19, False),
        WatchMetadata("T", "A", 19, True),
        WatchMetadata("T", "A", 19, True, ((320, 240),), False),
    ],
)
def test_unknown_or_incomplete_catalog_keeps_full_discovery(catalog):
    assert len(public_renditions(catalog)) == 4


@pytest.mark.parametrize("count", [20, 100, 1000])
async def test_distinct_requests_never_serialize_on_an_unrelated_hash_collision(monkeypatch, count):
    # Every identity would have occupied the same old striped lock.
    identities, index = [], 0
    while len(identities) < count:
        identity = f"{index:011d}"
        if hash(identity) % 32 == 0:
            identities.append(identity)
        index += 1
    client = YouTubeClient(resolver_url="https://cnv.cx")
    entered, release = set(), asyncio.Event()

    async def inspect(self, identity):
        entered.add(identity)
        if len(entered) == count:
            release.set()
        await release.wait()
        return identity

    monkeypatch.setattr(YouTubeClient, "_public_inspect", inspect)
    async with asyncio.timeout(3):
        results = await asyncio.gather(
            *(client.public_inspect(identity) for identity in identities)
        )
    assert results == identities and not client._public_locks


async def test_one_thousand_same_identity_followers_coalesce_and_leave_no_lock_entries(monkeypatch):
    client = YouTubeClient(resolver_url="https://cnv.cx")
    calls = 0

    async def inspect(self, identity):
        nonlocal calls
        if identity not in self._public_catalog:
            calls += 1
            await asyncio.sleep(0)
            self._public_catalog[identity] = "complete-menu"
        return self._public_catalog[identity]

    monkeypatch.setattr(YouTubeClient, "_public_inspect", inspect)
    results = await asyncio.gather(*(client.public_inspect("jNQXAC9IVRw") for _ in range(1000)))
    assert results == ["complete-menu"] * 1000 and calls == 1
    assert not client._public_locks


async def test_one_thousand_failed_followers_share_one_failure_until_cooldown_expires(monkeypatch):
    client = YouTubeClient(resolver_url="https://cnv.cx")
    calls = 0

    async def inspect(self, identity):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        raise DownloadError("Safe public failure", code="youtube_public_unavailable")

    monkeypatch.setattr(YouTubeClient, "_public_inspect", inspect)
    results = await asyncio.gather(
        *(client.public_inspect("same") for _ in range(1000)), return_exceptions=True
    )
    assert calls == 1 and all(isinstance(row, DownloadError) for row in results)
    assert all(row.code == "youtube_public_unavailable" for row in results)
    assert not client._public_locks
    _, code, message = client._public_failures["same"]
    client._public_failures["same"] = (time.monotonic() - 1, code, message)
    with pytest.raises(DownloadError):
        await client.public_inspect("same")
    assert calls == 2
    for index in range(200):
        client._remember_public_failure(
            str(index), DownloadError("Safe", code="youtube_public_unavailable")
        )
    assert len(client._public_failures) == 128
    assert all(isinstance(value[2], str) for value in client._public_failures.values())


async def test_cancellation_never_seeds_negative_cache_and_successful_resolution_clears_it(
    monkeypatch,
):
    client = YouTubeClient(resolver_url="https://cnv.cx")

    async def cancelled(self, identity):
        raise asyncio.CancelledError

    monkeypatch.setattr(YouTubeClient, "_public_inspect", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await client.public_inspect("same")
    assert not client._public_failures and not client._public_locks
    client._remember_public_failure(
        "same", DownloadError("Safe", code="youtube_public_unavailable")
    )

    async def resolved(self, media, quality):
        return "verified-source"

    monkeypatch.setattr(YouTubeClient, "_public_resolve", resolved)
    assert (
        await client.public_resolve(SimpleNamespace(content_id="same"), None) == "verified-source"
    )
    assert not client._public_failures


async def test_cancelled_owner_and_waiter_release_identity_entries():
    client = YouTubeClient()
    entered, hold = asyncio.Event(), asyncio.Event()

    async def owner():
        async with client._public_identity_lock("same", "inspect"):
            entered.set()
            await hold.wait()

    first = asyncio.create_task(owner())
    await entered.wait()
    second = asyncio.create_task(owner())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert client._public_locks["same"][1] == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not client._public_locks


async def test_queue_timeout_is_busy_and_does_not_invalidate_another_resolve(monkeypatch):
    monkeypatch.setattr(module, "PUBLIC_QUEUE_TIMEOUT", 0.01)
    client = YouTubeClient(resolver_url="https://cnv.cx")
    client._public_catalog["same"] = "complete-menu"
    async with client._public_identity_lock("same", "inspect"):
        with pytest.raises(DownloadError) as failure:
            await client.public_resolve(SimpleNamespace(content_id="same"), None)
        assert failure.value.code == "youtube_public_busy"
        assert client._public_catalog["same"] == "complete-menu"
        assert "same" not in client._public_probe_required
    assert not client._public_locks


async def test_distinct_pending_identity_bound_is_enforced_and_recovers(monkeypatch):
    monkeypatch.setattr(module, "PUBLIC_MAX_PENDING_IDENTITIES", 1)
    client = YouTubeClient()
    async with client._public_identity_lock("first", "inspect"):
        with pytest.raises(DownloadError) as failure:
            async with client._public_identity_lock("second", "inspect"):
                pytest.fail("Capacity was exceeded")
        assert failure.value.code == "youtube_public_busy"
    async with client._public_identity_lock("second", "inspect"):
        assert len(client._public_locks) == 1
    assert not client._public_locks


@pytest.mark.parametrize("phase", ["response_headers", "response_body"])
async def test_curl_operation_deadline_covers_headers_and_body_and_logs_no_secrets(
    monkeypatch, caplog, phase
):
    monkeypatch.setattr(module, "PUBLIC_OPERATION_TIMEOUT", 0.01)
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"{}"

    class HTTP:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            try:
                if phase == "response_headers":
                    await asyncio.Event().wait()
                yield httpx.Response(
                    200, stream=Body(), request=httpx.Request("GET", "https://example.org/")
                )
            finally:
                closed.append(True)

    caplog.set_level(logging.INFO, logger=module.__name__)
    client = YouTubeClient()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await client._public_json(
            HTTP(),
            "GET",
            "https://cnv.cx/v2/sanity/key?secret=value",
            headers={"key": "secret-key"},
        )
    assert time.monotonic() - started < 0.5 and closed == [True]
    event = next(
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict)
        and record.msg.get("event") == "youtube_public_operation_failed"
    )
    assert event["phase"] == phase and event["error_type"] == "TimeoutError"
    assert "secret" not in caplog.text and "cnv.cx" not in caplog.text
