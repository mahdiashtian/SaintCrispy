import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "video_access_diagnostics", ROOT / "tools/diagnose_video_access.py"
)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


async def test_configuration_reports_health_without_disclosing_credentials(monkeypatch, tmp_path):
    cookies = tmp_path / "SECRET.cookies"
    cookies.write_text("# Netscape HTTP Cookie File\nSECRET", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    monkeypatch.setenv("YOUTUBE_PROXY", "http://SECRET:password@example.org")
    monkeypatch.setenv("INSTAGRAM_COOKIE", "sessionid=SECRET")
    monkeypatch.setenv("INSTAGRAM_PROXY", "http://SECRET:password@example.org")
    monkeypatch.setenv("YOUTUBE_POT_BASE_URL", "http://SECRET:password@example.org")

    async def runtime(spec):
        return {"name": "node", "available": True, "version": "24.0.0"}

    async def helper(base_url):
        return {"configured": True, "reachable": False}

    monkeypatch.setattr(diagnostics, "runtime_status", runtime)
    monkeypatch.setattr(diagnostics, "helper_status", helper)
    result = await diagnostics.configuration()
    assert result["youtube"]["cookies"]["readable"]
    assert result["youtube"]["cookies"]["netscape"]
    assert result["instagram"]["cookies_configured"]
    encoded = json.dumps(result)
    assert "SECRET" not in encoded and "password" not in encoded and "example.org" not in encoded


async def test_helper_invalid_payload_and_network_error_do_not_leak_response(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(200, json={"version": "SECRET"})

    monkeypatch.setattr(
        diagnostics.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await diagnostics.helper_status("http://localhost:4416")
    assert not result["reachable"] and "SECRET" not in json.dumps(result)

    def broken(request):
        raise httpx.ConnectError("http://SECRET:password@example.org")

    monkeypatch.setattr(
        diagnostics.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(broken), **kwargs),
    )
    result = await diagnostics.helper_status("http://localhost:4416")
    assert not result["reachable"] and "SECRET" not in json.dumps(result)
