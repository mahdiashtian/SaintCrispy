"""Keep live origin diagnostics reproducible without publishing signed URLs."""

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

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
