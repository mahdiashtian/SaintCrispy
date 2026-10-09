import json

from downloader_bot.downloaders.soundcloud.parser import read_qualities, read_track


def variant(preset, protocol="hls", **extra):
    return {
        "preset": preset,
        "quality": "sq",
        "url": f"https://api-v2.soundcloud.com/{preset}",
        "format": {"protocol": protocol},
        **extra,
    }


def test_quality_identity_is_independent_of_protocol_and_previews_are_excluded():
    track = {
        "media": {
            "transcodings": [
                variant("aac_160k"),
                variant("aac_96k"),
                variant("mp3_1_0"),
                variant("mp3_1_0", "progressive"),
                variant("aac_256k", snipped=True),
                variant("abr_sq"),
                variant("aac_256k", "ctr-hls"),
            ]
        }
    }
    qualities = read_qualities(track)
    assert {q.key for q in qualities} == {"aac_160", "aac_96", "mp3_sq"}
    assert next(q for q in qualities if q.key == "mp3_sq").protocol == "progressive"
    assert next(q for q in qualities if q.key == "mp3_sq").fallback_endpoint is not None
    assert qualities[0].key == "aac_160"


def test_track_numeric_id_is_preserved_even_above_signed_32_bit_range():
    page = (
        "window.__sc_hydration = "
        + json.dumps(
            [
                {"hydratable": "sound", "data": {"id": 2373831104, "title": "Mojezeh"}},
            ]
        )
        + ";"
    )
    assert read_track(page)["id"] == 2373831104


def test_legacy_aac_hq_preset_is_ranked_above_standard_aac():
    track = {"media": {"transcodings": [variant("aac_160k"), variant("aac_0_0", quality="hq")]}}
    assert [q.key for q in read_qualities(track)] == ["aac_256", "aac_160"]
