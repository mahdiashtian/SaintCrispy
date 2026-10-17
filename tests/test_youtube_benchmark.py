"""Batch tool accounting and stop conditions, without external network access."""

from types import SimpleNamespace

import pytest

from schemas.media import Media, Quality, SiteHTTPError, Source
from tools import benchmark_youtube as tool


@pytest.fixture
def batch(monkeypatch, tmp_path):
    client = SimpleNamespace(_public_cache={}, _public_catalog={})
    calls = []
    quality = Quality("audio", "MP3", "mp3", 128, "mp3", "audio/mpeg", "progressive", "")

    class Provider:
        async def inspect(self, url):
            calls.append(url)
            return Media("youtube", tool.video_id(url), "T", "A", 19, url, None, (quality,))

        async def resolve(self, media, quality):
            return Source("https://example.org/private-signature", "progressive", duration=19)

    async def chunks(http, source, quality, ffmpeg, progress, **options):
        progress.seconds = 19
        yield bytes(1024)

    monkeypatch.setattr(tool.YouTubeClient, "from_environment", lambda: client)
    monkeypatch.setattr(tool, "YouTubeDownloader", lambda client: Provider())
    monkeypatch.setattr(tool, "media_chunks", chunks)
    args = SimpleNamespace(
        url=["https://youtu.be/jNQXAC9IVRw"],
        stages=[20, 100],
        concurrency=4,
        cold=True,
        case_timeout=1,
        max_file_mib=1,
        max_failures=3,
        ffmpeg="unused",
        output=tmp_path / "report.json",
    )
    return args, calls


async def test_twenty_then_one_hundred_samples_record_full_bytes_and_separate_stages(batch):
    args, calls = batch
    report = await tool.run(args)
    assert len(calls) == 120 and report["unique_inputs"] == 1
    assert [stage["completed"] for stage in report["stages"]] == [20, 100]
    assert [stage["concurrency"] for stage in report["stages"]] == [1, 4]
    assert sum(stage["received_bytes"] for stage in report["stages"]) == 120 * 1024
    text = args.output.read_text()
    assert "private-signature" not in text


async def test_rate_limit_stops_both_stages_without_rotating_or_retrying(batch, monkeypatch):
    args, calls = batch

    class Rejected:
        async def inspect(self, url):
            calls.append(url)
            raise SiteHTTPError(429, "Safe message")

    monkeypatch.setattr(tool, "YouTubeDownloader", lambda client: Rejected())
    report = await tool.run(args)
    assert len(calls) == 1
    assert [stage["attempted"] for stage in report["stages"]] == [1, 0]
    assert [stage["not_attempted"] for stage in report["stages"]] == [19, 100]
    assert report["stages"][0]["rows"][0]["http_status"] == 429
