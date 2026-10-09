"""Keep live origin diagnostics reproducible without publishing signed URLs."""

import argparse
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telethon import errors, functions

from downloader_bot.schemas.media import DownloadError, Media, Quality, Source

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("provider_audit", ROOT / "tools/audit_providers.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
probe_spec = importlib.util.spec_from_file_location(
    "soundcloud_probe", ROOT / "tools/probe_soundcloud.py"
)
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)


def test_multiple_reported_links_keep_separate_cases_for_the_same_provider():
    first = "https://soundcloud.com/quavoofficial/away"
    second = "https://soundcloud.com/octobersveryown/quebec"
    args = SimpleNamespace(
        sites=["soundcloud", "youtube"], url=[("soundcloud", first), ("soundcloud", second)]
    )
    assert audit.requested_cases(args) == [
        ("soundcloud", "soundcloud", first),
        ("soundcloud", "soundcloud:2", second),
        ("youtube", "youtube", audit.SAMPLES["youtube"]),
    ]


@pytest.mark.parametrize(
    "value", ["unknown=https://example.com", "soundcloud=file:///tmp/private", "bad"]
)
def test_invalid_audit_case_is_rejected_before_origin_requests(value):
    with pytest.raises(argparse.ArgumentTypeError):
        audit.url_argument(value)


async def test_legacy_diagnostics_preserve_statuses_without_fetching_or_printing_signed_media():
    visited = []
    private_id = "private-client-id"
    signed_url = "https://cdn.example/full.mp3?token=private-signature"

    def respond(request):
        visited.append((request.url.host, request.url.path))
        assert request.url.host in {"api.soundcloud.com", "api-v2.soundcloud.com"}
        if request.url.host == "api-v2.soundcloud.com":
            raise httpx.ConnectTimeout("private failure detail", request=request)
        if request.url.path.endswith("/streams"):
            return httpx.Response(200, json={"http_mp3_128_url": signed_url})
        return httpx.Response(302, headers={"location": signed_url})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        report = await probe.inspect_legacy_streams(http, "123", {"client_id": private_id})
    assert len(visited) == 3
    assert report[0]["error_type"] == "ConnectTimeout"
    assert report[1]["media_hosts"] == {"http_mp3_128_url": "cdn.example"}
    assert report[2]["status"] == 302
    assert report[2]["redirect_host"] == "cdn.example"
    serialized = json.dumps(report)
    assert private_id not in serialized
    assert "private-signature" not in serialized
    assert "private failure detail" not in serialized


def stream_args(**overrides):
    return SimpleNamespace(
        **{
            "sites": ["soundcloud"],
            "url": [],
            "all_qualities": True,
            "telegram": False,
            "stream_mismatches": False,
            "stream": True,
            "case_timeout": 2,
            "transfer_timeout": 1,
            "max_stream_mib": 1,
            "ffmpeg": "unused",
            **overrides,
        }
    )


async def test_audit_checks_later_qualities_after_a_resolution_failure(monkeypatch):
    quality = Quality("broken", "Audio", "mp3", None, "mp3", "audio/mpeg", "progressive", "api")
    media = Media(
        "soundcloud",
        "123",
        "Track",
        "Artist",
        1,
        audit.SAMPLES["soundcloud"],
        None,
        tuple(replace(quality, key=key) for key in ("broken", "first", "second")),
    )

    async def inspect(url):
        return media

    async def resolve(_, quality):
        if quality.key == "broken":
            raise DownloadError("Unavailable rendition", code="test_unavailable")
        return Source(
            "https://cdn.example/audio?token=PRIVATE", "progressive", proxy="", size_bytes=12
        )

    monkeypatch.setattr(
        audit, "SoundCloudDownloader", lambda _: SimpleNamespace(inspect=inspect, resolve=resolve)
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        audit.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ID3" + b"a" * 9)),
            **kwargs,
        ),
    )
    report = await audit.audit(stream_args())
    rows = report["providers"]["soundcloud"]["resolved"]
    assert not report["passed"]
    assert [row["verified"] for row in rows] == [False, True, True]
    assert all(row["stream_complete"] and row["stream_bytes"] == 12 for row in rows[1:])
    assert "PRIVATE" not in json.dumps(report)


async def test_full_stream_failure_is_not_reported_as_a_successful_cdn_sample(monkeypatch):
    quality = Quality("audio", "Audio", "mp3", None, "mp3", "audio/mpeg", "progressive", "api")
    source = Source("https://cdn.example/audio", "progressive", size_bytes=100)
    media = Media("soundcloud", "123", "Track", "Artist", 1, "page", None, (quality,))

    async def resolve(*args):
        return source

    original = httpx.AsyncClient
    monkeypatch.setattr(
        audit.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ID3" + b"a" * 9)),
            **kwargs,
        ),
    )
    row, _ = await audit.check_quality(
        SimpleNamespace(resolve=resolve), media, quality, stream_args()
    )
    assert row["cdn_responds"] and row["sample_bytes"] == 12
    assert not row["verified"] and not row["stream_complete"]
    assert row["error_type"] == "DownloadError"


async def test_telegram_external_failure_uses_verified_stream_without_sending_a_message(
    monkeypatch,
):
    payload = b"ID3" + b"a" * 9
    quality = Quality("audio", "Audio", "mp3", None, "mp3", "audio/mpeg", "progressive", "api")
    source = Source("https://cdn.example/audio", "progressive", size_bytes=len(payload))
    calls = []

    async def telegram(request):
        calls.append(request)
        if isinstance(request, functions.upload.SaveBigFilePartRequest):
            assert request.bytes == payload
            return True
        assert isinstance(request, functions.messages.UploadMediaRequest)
        if len(calls) == 1:
            raise errors.WebpageCurlFailedError(request=None)
        return SimpleNamespace(
            document=SimpleNamespace(size=len(payload), file_reference=b"reference")
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        audit.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)), **kwargs
        ),
    )
    row = {}
    await audit.register_quality(telegram, quality, source, stream_args(), row)
    assert not row["telegram_external_accepted"]
    assert row["telegram_method"] == "stream" and row["telegram_verified"]
    assert row["telegram_uploaded_bytes"] == row["telegram_bytes"] == len(payload)


async def test_telegram_size_mismatch_cannot_pass_verification():
    quality = Quality("audio", "Audio", "mp3", None, "mp3", "audio/mpeg", "progressive", "api")

    async def telegram(request):
        return SimpleNamespace(document=SimpleNamespace(size=5, file_reference=b"reference"))

    row = {}
    await audit.register_quality(
        telegram,
        quality,
        Source("https://cdn.example/audio", "progressive", size_bytes=100),
        stream_args(stream=False),
        row,
    )
    assert row["telegram_external_accepted"] and not row["size_matches_origin"]
    assert not row["telegram_verified"]
