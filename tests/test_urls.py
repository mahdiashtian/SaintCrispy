import pytest

from downloader_bot.downloaders.router import (
    extract_soundcloud_url,
    extract_xnxx_url,
    site_link_pattern,
)


@pytest.mark.parametrize(
    "text",
    [
        "soundcloud.com/gdaal/mojezeh",
        "www.soundcloud.com/gdaal/mojezeh",
        "https://soundcloud.com/gdaal/mojezeh",
        "http://soundcloud.com/gdaal/mojezeh",
        "این آهنگ: https://soundcloud.com/gdaal/mojezeh",
        "(https://soundcloud.com/gdaal/mojezeh)",
        "HTTPS://SOUNDCLOUD.COM/gdaal/mojezeh",
        "on.soundcloud.com/abc123",
        "m.soundcloud.com/gdaal/mojezeh",
    ],
)
def test_domain_links_are_routed(text):
    assert extract_soundcloud_url(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "https://soundcloud.com.evil.org/track",
        "https://evil.soundcloud.com/track",
        "https://notsoundcloud.com/track",
        "https://evil.org/soundcloud.com/track",
        "me@soundcloud.com",
        "https://soundcloud.com@evil.org/track",
        "https://soundcloud.com:9999/track",
        "ftp://soundcloud.com/track",
    ],
)
def test_similar_or_embedded_domains_are_rejected(text):
    assert extract_soundcloud_url(text) is None


def test_query_is_preserved_and_text_punctuation_removed():
    assert extract_soundcloud_url("آهنگ (soundcloud.com/a/b?secret_token=s-test).") == (
        "https://soundcloud.com/a/b?secret_token=s-test"
    )


def test_pattern_can_be_reused_for_another_site():
    pattern = site_link_pattern("example.com")
    assert pattern.search("این لینک www.example.com/a")
    assert not pattern.search("https://example.com.evil.org/a")


@pytest.mark.parametrize(
    "text",
    [
        "xnxx.com/video-demo/example",
        "ویدیو: www.xnxx.com/video-demo/example",
        "https://xnxx.com/video-demo/example",
        "http://m.xnxx.com/video-demo/example",
        "(HTTPS://WWW.XNXX.COM/video-demo/example).",
        "video.xnxx.com/video12345/title",
        "www.xnxx3.com/video-demo/title",
        "xnxx3.com/video-demo/title",
        "m.xnxx3.com/video-demo/title",
    ],
)
def test_xnxx_links_are_normalized(text):
    assert extract_xnxx_url(text).lower().startswith("https://")


@pytest.mark.parametrize(
    "text",
    [
        "https://xnxx.com.evil.org/video-demo",
        "https://notxnxx.com/video-demo",
        "https://evil.xnxx.com/video-demo",
        "https://evil.org/xnxx.com/video-demo",
        "https://xnxx.com@evil.org/video-demo",
        "https://xnxx.com:9999/video-demo",
        "me@xnxx.com",
        "https://video.xnxx.com.evil.org/video-demo",
        "https://xnxx3.com@evil.org/video-demo",
        "https://www.xnxx3.com.evil.org/video-demo",
    ],
)
def test_xnxx_lookalike_domains_are_rejected(text):
    assert extract_xnxx_url(text) is None
