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


async def test_diagnostic_uses_bot_environment_precedence_and_literal_secret_values(
    monkeypatch, tmp_path, capsys
):
    (tmp_path / ".env").write_text(
        "\ufeffINSTAGRAM_COOKIE='sessionid=SECRET${OTHER}'\n", encoding="utf-8"
    )
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    monkeypatch.setenv("INSTAGRAM_COOKIE", "stale")
    monkeypatch.setenv("OTHER", "expanded")
    monkeypatch.setattr(diagnostics.sys, "argv", ["diagnose_video_access.py"])

    async def configuration():
        assert diagnostics.os.environ["INSTAGRAM_COOKIE"] == "sessionid=SECRET${OTHER}"
        return {"configuration_loaded": True}

    monkeypatch.setattr(diagnostics, "configuration", configuration)
    assert await diagnostics.main() == 0
    assert "SECRET" not in capsys.readouterr().out


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


async def test_helper_connection_error_does_not_leak_proxy(monkeypatch):
    original = httpx.AsyncClient

    def broken(request):
        raise httpx.ConnectError("http://SECRET:password@example.org")

    monkeypatch.setattr(
        diagnostics.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(broken), **kwargs),
    )
    result = await diagnostics.helper_status("http://localhost:4416")
    assert not result["reachable"] and "SECRET" not in json.dumps(result)


async def test_configuration_flags_stale_extractor_and_missing_node(monkeypatch):
    async def runtime(spec):
        return {"name": "node", "available": False}

    async def helper(base_url):
        return {"configured": False, "reachable": False}

    monkeypatch.setattr(diagnostics, "runtime_status", runtime)
    monkeypatch.setattr(diagnostics, "helper_status", helper)
    monkeypatch.setattr(diagnostics, "package_version", lambda name: "2026.8.19")
    report = await diagnostics.configuration()
    assert {"code": "youtube_js_runtime_unavailable"} in report["deployment_issues"]
    assert any(
        row.get("package") == "yt-dlp"
        and row["expected"] == diagnostics.dependency_pins()["yt-dlp"]
        for row in report["deployment_issues"]
    )


async def test_connectivity_probe_reports_http_rejection_without_consuming_body():
    class NoBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Connectivity probe must not read the body")
            yield

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, stream=NoBody()))
    ) as http:
        result = await diagnostics.connection_probe(http, "https://www.youtube.com/")
    assert result["http_response_received"] and result["http_status"] == 403


async def test_connectivity_probe_distinguishes_tcp_failure_and_redacts_causes():
    async def broken(request):
        await request.extensions["trace"]("connection.connect_tcp.started", {})
        try:
            raise OSError(101, "SECRET network address")
        except OSError as cause:
            raise httpx.ConnectError("SECRET proxy password") from cause

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
        result = await diagnostics.connection_probe(http, "https://www.youtube.com/")
    assert not result["http_response_received"] and result["failed_phase"] == "tcp_connect"
    assert result["failure_chain"][-1]["errno"] == 101
    assert "SECRET" not in json.dumps(result)


async def test_bad_provider_transport_does_not_hide_other_connection_report(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PROXY", "SECRET://invalid")
    monkeypatch.setenv("INSTAGRAM_PROXY", "")

    async def connected(http, url):
        return {"http_response_received": True, "http_status": 200}

    monkeypatch.setattr(diagnostics, "connection_probe", connected)
    report = await diagnostics.network_status()
    assert report["youtube"]["failed_phase"] == "transport_setup"
    assert report["instagram"]["http_response_received"]
    assert "SECRET" not in json.dumps(report)


async def test_completed_tls_is_not_misreported_as_a_tls_failure():
    async def stalled_headers(request):
        for event in (
            "connect_tcp.started",
            "connect_tcp.complete",
            "start_tls.started",
            "start_tls.complete",
        ):
            await request.extensions["trace"]("connection." + event, {})
        raise httpx.ReadTimeout("SECRET")

    async with httpx.AsyncClient(transport=httpx.MockTransport(stalled_headers)) as http:
        report = await diagnostics.connection_probe(http, "https://www.youtube.com/")
    assert report["failed_phase"] == "http_headers"
