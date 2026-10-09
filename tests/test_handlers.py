from types import SimpleNamespace

import pytest
from telethon import errors

from downloader_bot.bot.handlers import register_handlers
from downloader_bot.bot.state.menus import MenuStore
from downloader_bot.downloaders.pinterest.handler import handle_pinterest_link
from downloader_bot.downloaders.soundcloud.handler import handle_soundcloud_link
from downloader_bot.downloaders.xnxx.handler import handle_xnxx_link
from downloader_bot.downloaders.xvideos.handler import handle_xvideos_link
from downloader_bot.schemas.media import DownloadError, Media, Quality

QUALITY = Quality("aac_160", "AAC 160", "aac", 160, "m4a", "audio/mp4", "hls", "endpoint")


@pytest.mark.parametrize("thumbnail", [None, "https://i1.sndcdn.com/artwork.jpg"])
async def test_menu_has_quality_buttons_even_without_usable_thumbnail(thumbnail):
    responses = []
    media = Media("soundcloud", "track", "Title", "Artist", 30, "url", thumbnail, (QUALITY,))

    async def inspect(url):
        assert url == "https://www.soundcloud.com/gdaal/mojezeh"
        return media

    async def respond(text, **kwargs):
        if kwargs.get("file"):
            raise errors.WebpageCurlFailedError(request=None)
        responses.append((text, kwargs))

    event = SimpleNamespace(
        raw_text="این آهنگ www.soundcloud.com/gdaal/mojezeh",
        sender_id=1,
        chat_id=2,
        respond=respond,
    )
    await handle_soundcloud_link(event, SimpleNamespace(inspect=inspect), MenuStore())
    assert len(responses) == 1
    button = responses[0][1]["buttons"][0][0]
    assert button.text == "AAC 160"
    assert len(button.type.data) <= 64


def test_handlers_are_registered_separately_with_specific_patterns():
    registrations = []
    client = SimpleNamespace(
        add_event_handler=lambda callback, event: registrations.append((callback, event))
    )
    register_handlers(client, None, None)
    # Find handlers by callback so registering another site cannot change these assertions.
    messages = {
        getattr(callback, "func", callback).__name__: event
        for callback, event in registrations
        if hasattr(event, "pattern")
    }
    assert messages["handle_start"].pattern("/start")
    assert not messages["handle_start"].pattern("soundcloud.com/a/b")
    assert messages["handle_soundcloud_link"].pattern("لینک: www.soundcloud.com/a/b")
    assert not messages["handle_soundcloud_link"].pattern("https://notsoundcloud.com/a/b")
    assert messages["handle_xnxx_link"].pattern("ویدیو: www.xnxx.com/video-demo/example")
    assert not messages["handle_xnxx_link"].pattern("https://xnxx.com.evil.org/video-demo/example")
    assert messages["handle_xvideos_link"].pattern("ویدیو: www.xvideos.com/video.demo/example")
    assert not messages["handle_xvideos_link"].pattern("https://xvideos.com.evil.org/video.demo")
    callbacks = next(
        event
        for callback, event in registrations
        if getattr(callback, "func", callback).__name__ == "handle_quality"
    )
    assert callbacks.match(b"xnxx:0123456789abcdef:0").groups() == (
        b"0123456789abcdef",
        b"0",
    )
    assert callbacks.match(b"sc:0123456789abcdef:0")
    assert callbacks.match(b"xv:0123456789abcdef:0")
    assert callbacks.match(b"pin:0123456789abcdef:0")
    assert messages["handle_pinterest_link"].pattern("پین: www.pinterest.com/pin/123456789/")
    assert not messages["handle_pinterest_link"].pattern("https://pinterest.com.evil.org/pin/1")


async def test_pinterest_menu_uses_its_own_callback_prefix():
    responses = []
    quality = Quality("image_orig", "اصلی", "jpg", None, "jpg", "image/jpeg", "progressive", "url")
    media = Media("pinterest", "123", "Demo", "Author", 0, "url", None, (quality,))
    menus = MenuStore()

    async def inspect(url):
        assert url == "https://pin.it/demo"
        return media

    async def respond(text, **kwargs):
        responses.append((text, kwargs))

    event = SimpleNamespace(raw_text="پین pin.it/demo", sender_id=1, chat_id=2, respond=respond)
    await handle_pinterest_link(event, SimpleNamespace(inspect=inspect), menus)
    prefix, token, index = responses[0][1]["buttons"][0][0].type.data.decode().split(":")
    assert prefix == "pin" and index == "0"
    assert menus.get(token, 1, 2).media.site == "pinterest"


@pytest.mark.parametrize(
    ("site", "callback_prefix", "handler", "path"),
    [
        ("xnxx", "xnxx", handle_xnxx_link, "video-demo"),
        ("xvideos", "xv", handle_xvideos_link, "video.demo"),
    ],
)
async def test_video_menu_uses_its_own_callback_prefix(site, callback_prefix, handler, path):
    responses = []
    quality = Quality(
        "high", "MP4 کیفیت بالا", "video", None, "mp4", "video/mp4", "progressive", "url"
    )
    media = Media(site, "demo", "Demo", "", 30, "url", None, (quality,))
    menus = MenuStore()

    async def inspect(url):
        assert url == f"https://www.{site}.com/{path}/example"
        return media

    async def respond(text, **kwargs):
        responses.append((text, kwargs))

    event = SimpleNamespace(
        raw_text=f"ویدیو www.{site}.com/{path}/example",
        sender_id=1,
        chat_id=2,
        respond=respond,
    )
    await handler(event, SimpleNamespace(inspect=inspect), menus)
    button = responses[0][1]["buttons"][0][0]
    prefix, token, index = button.type.data.decode().split(":")
    assert prefix == callback_prefix and index == "0"
    assert len(button.type.data) <= 64
    assert menus.get(token, 1, 2).media.site == site


def test_other_users_cannot_select_the_menu_owners_quality():
    store = MenuStore()
    media = Media("soundcloud", "track", "Title", "Artist", 30, "url", None, (QUALITY,))
    token = store.add(1, 2, media)
    with pytest.raises(DownloadError):
        store.get(token, 3, 2)
