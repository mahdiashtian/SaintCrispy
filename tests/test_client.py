import asyncio
import json

import httpx
import pytest

from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.schemas.media import DownloadError, SiteHTTPError


async def test_redirect_is_checked_before_fetching_another_host():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/internal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SoundCloudClient(http)
        with pytest.raises(DownloadError):
            await client.page("https://on.soundcloud.com/example")
    assert visited == ["https://on.soundcloud.com/example"]


def client_page(client_id):
    return (
        "window.__sc_hydration = "
        + json.dumps([{"hydratable": "apiClient", "data": {"id": client_id}}])
        + ";"
    )


async def test_hydrated_client_id_does_not_need_any_javascript_download():
    def reject(request):
        pytest.fail("The page already contains the public client ID")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        client = SoundCloudClient(http)
        assert await client.public_client_id(client_page("a" * 32)) == "a" * 32


async def test_client_id_asset_fallback_skips_a_removed_script():
    visited = []

    def respond(request):
        visited.append(request.url.path)
        if request.url.path == "/removed.js":
            return httpx.Response(404)
        return httpx.Response(200, text='client_id:"' + "b" * 32 + '"')

    page = '<script src="https://a-v2.sndcdn.com/current.js"></script><script src="https://a-v2.sndcdn.com/removed.js"></script>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await SoundCloudClient(http).public_client_id(page) == "b" * 32
    assert visited == ["/removed.js", "/current.js"]


async def test_rejected_hydration_and_asset_ids_do_not_hide_a_current_asset_id():
    old_id, new_id = "o" * 32, "n" * 32
    visited = []
    page = (
        client_page(old_id)
        + '<script src="https://a-v2.sndcdn.com/current.js"></script>'
        + '<script src="https://a-v2.sndcdn.com/stale.js"></script>'
    )

    def respond(request):
        visited.append(request.url.path)
        if request.url.host == "soundcloud.com":
            return httpx.Response(200, text=page)
        if request.url.host == "a-v2.sndcdn.com":
            value = old_id if request.url.path == "/stale.js" else new_id
            return httpx.Response(200, text=f'client_id:"{value}"')
        return (
            httpx.Response(401)
            if request.url.params["client_id"] == old_id
            else httpx.Response(200, json={"id": 1})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SoundCloudClient(http)
        client.client_id = old_id
        assert await client.api("https://api-v2.soundcloud.com/tracks/1") == {"id": 1}
        assert client.client_id == new_id
    assert visited == ["/tracks/1", "/", "/stale.js", "/current.js", "/tracks/1"]


async def test_no_replacement_client_id_preserves_the_original_permission_failure():
    visited = []

    def respond(request):
        visited.append(request.url.path)
        return (
            httpx.Response(200, text=client_page("o" * 32))
            if request.url.host == "soundcloud.com"
            else httpx.Response(403)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SoundCloudClient(http)
        client.client_id = "o" * 32
        with pytest.raises(SiteHTTPError) as error:
            await client.api("https://api-v2.soundcloud.com/tracks/1")
    assert error.value.status == 403
    assert visited == ["/tracks/1", "/"]


async def test_concurrent_expired_client_ids_share_one_refresh_and_retry_once():
    home_requests = []

    async def respond(request):
        if request.url.host == "soundcloud.com":
            home_requests.append(request)
            await asyncio.sleep(0.01)
            assert "authorization" not in request.headers
            return httpx.Response(200, text=client_page("n" * 32))
        if request.url.params["client_id"] == "o" * 32:
            return httpx.Response(401)
        assert request.url.params["client_id"] == "n" * 32
        assert request.headers["authorization"] == "OAuth test-token"
        return httpx.Response(200, json={"url": "https://cdn.example/full.mp3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SoundCloudClient(http, "test-token")
        client.client_id = "o" * 32
        results = await asyncio.gather(
            client.api("https://api-v2.soundcloud.com/media/1"),
            client.api("https://api-v2.soundcloud.com/media/2"),
        )
    assert len(home_requests) == 1
    assert all(result["url"] for result in results)


@pytest.mark.parametrize("path,status", [("/tracks/1/download", 401), ("/media/1", 429)])
async def test_account_permission_and_rate_limit_do_not_refresh_the_client_id(path, status):
    visited = []

    def respond(request):
        visited.append(request.url.path)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SoundCloudClient(http)
        client.client_id = "a" * 32
        with pytest.raises(SiteHTTPError) as error:
            await client.api("https://api-v2.soundcloud.com" + path)
    assert error.value.status == status
    assert visited == [path]
