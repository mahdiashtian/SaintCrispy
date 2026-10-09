import copy

import pytest

from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.instagram.parser import item_identity, read_media
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.pinterest.parser import read_pin
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.soundcloud.parser import read_qualities
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.schemas.media import DownloadError


@pytest.mark.parametrize(
    "downloader,aliases",
    [
        (
            YouTubeDownloader(None),
            ["https://youtu.be/jNQXAC9IVRw?si=one", "https://www.youtube.com/shorts/jNQXAC9IVRw"],
        ),
        (
            XVideosDownloader(None),
            [
                "https://www.xvideos.com/video.ABC/title?x=one",
                "https://www.xvideos2.com/embedframe/abc",
            ],
        ),
        (
            XNXXDownloader(None),
            [
                "https://www.xnxx.com/video-ABC/title?x=1",
                "https://m.xnxx3.com/video-abc/another_title",
            ],
        ),
        (
            PinterestDownloader(None),
            [
                "https://www.pinterest.com/pin/123456/",
                "https://www.pinterest.co.uk/pin/title--123456/sent/?x=1",
            ],
        ),
        (
            InstagramDownloader(None),
            [
                "https://www.instagram.com/p/Chunk8-jurw/?igsh=x",
                "https://instagram.com/reel/Chunk8-jurw/",
            ],
        ),
        (
            SoundCloudDownloader(None),
            [
                "https://m.soundcloud.com/artist/track/?utm_source=one",
                "https://www.soundcloud.com/artist/track?si=two",
            ],
        ),
    ],
)
def test_provider_cache_identity_ignores_tracking_host_and_display_slug(downloader, aliases):
    assert downloader.cache_key(aliases[0]) == downloader.cache_key(aliases[1])


def test_soundcloud_private_access_tokens_and_case_sensitive_youtube_ids_remain_distinct():
    sc = SoundCloudDownloader(None)
    assert sc.cache_key("https://soundcloud.com/a/b?secret_token=one") != sc.cache_key(
        "https://soundcloud.com/a/b?secret_token=two"
    )
    yt = YouTubeDownloader(None)
    assert yt.cache_key("https://youtu.be/jNQXAC9IVRw") != yt.cache_key(
        "https://youtu.be/jNQXAC9IVRW"
    )


def test_soundcloud_mutable_slugs_are_never_permanent_track_id_aliases():
    sc = SoundCloudDownloader(None)
    assert sc.catalog_key("https://soundcloud.com/artist/track") is None
    assert sc.catalog_key("https://on.soundcloud.com/example") is None


def test_instagram_mobile_relay_and_shortcode_child_ids_identify_the_same_asset():
    assert (
        item_identity({"pk": "2913440072144448240"}, "", child=True)
        == item_identity(
            {"id": "POLARIS_2913440072144448240_123"},
            "",
            child=True,
        )
        == item_identity({"code": "Chunk8-jurw"}, "", child=True)
    )
    with pytest.raises(DownloadError, match="شناسه پایدار"):
        item_identity({}, "parent", child=True)


def test_instagram_carousel_reordering_never_relabels_an_existing_file():
    first = {
        "code": "Chunk8-jurw",
        "display_url": "https://scontent.cdninstagram.com/a.jpg",
        "dimensions": {"width": 640, "height": 640},
    }
    second = {
        **first,
        "code": "BQ0eAlwhDrw",
        "display_url": "https://scontent.cdninstagram.com/b.jpg",
    }
    node = {"carousel_media": [first, second]}
    _, original = read_media(node, "https://www.instagram.com/p/parent/", "parent")
    node["carousel_media"].reverse()
    _, reordered = read_media(node, "https://www.instagram.com/p/parent/", "parent")
    assert {f.quality.key: f.source.url for f in original} == {
        f.quality.key: f.source.url for f in reordered
    }


def test_pinterest_carousel_without_slot_ids_uses_media_identity_not_order_or_signed_url():
    def images(signature, query):
        return {
            "orig": {
                "url": f"https://i.pinimg.com/originals/{signature}.jpg?token={query}",
                "width": 640,
                "height": 640,
            }
        }

    data = {
        "id": "123",
        "carousel_data": {
            "carousel_slots": [
                {"images": images("a" * 32, "old")},
                {"images": images("b" * 32, "old")},
            ]
        },
    }
    original = read_pin(data, "123")
    changed = copy.deepcopy(data)
    changed["carousel_data"]["carousel_slots"].reverse()
    for slot in changed["carousel_data"]["carousel_slots"]:
        slot["images"]["orig"]["url"] = slot["images"]["orig"]["url"].replace("old", "new")
    reordered = read_pin(changed, "123")
    assert {q.key: q.endpoint.split("?")[0] for q in original.qualities} == {
        q.key: q.endpoint.split("?")[0] for q in reordered.qualities
    }
    replacement = copy.deepcopy(data)
    replacement["carousel_data"]["carousel_slots"][0]["images"] = images("c" * 32, "new")
    assert original.qualities[0].key != read_pin(replacement, "123").qualities[0].key


def test_soundcloud_opus_keeps_native_container_and_prefers_a_complete_direct_url():
    item = {
        "preset": "opus_64k",
        "quality": "sq",
        "url": "https://api-v2.soundcloud.com/stream",
        "format": {"protocol": "hls"},
    }
    qualities = read_qualities(
        {
            "media": {
                "transcodings": [
                    item,
                    {
                        **item,
                        "url": "https://api-v2.soundcloud.com/direct",
                        "format": {"protocol": "progressive"},
                    },
                ]
            }
        }
    )
    assert len(qualities) == 1
    assert (
        qualities[0].key,
        qualities[0].protocol,
        qualities[0].extension,
        qualities[0].mime_type,
    ) == (
        "opus_64",
        "progressive",
        "opus",
        "audio/ogg",
    )
    assert qualities[0].fallback_endpoint == item["url"]
