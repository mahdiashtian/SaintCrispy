import json
from dataclasses import fields, replace

from repositories.metadata import public_media
from repositories.postgres.workflow import read_media
from schemas.media import Media, Quality


def test_public_snapshot_preserves_quality_identity_without_credentials_or_sources():
    quality = Quality(
        "full",
        "Full",
        "h264",
        160,
        "mp4",
        "video/mp4",
        "progressive",
        "SIGNED-SOURCE",
        original=True,
        fallback_endpoint="SIGNED-FALLBACK",
        width=720,
        height=1280,
        duration=12,
    )
    media = Media(
        "instagram",
        "identity",
        "Title",
        "Artist",
        12,
        "https://www.instagram.com/reel/identity/",
        "SIGNED-THUMBNAIL",
        (quality,),
        authorization="PRIVATE-CREDENTIAL",
        account_id="local-label",
    )
    snapshot = public_media(media)
    encoded = json.dumps(snapshot)
    assert "SIGNED" not in encoded and "PRIVATE-CREDENTIAL" not in encoded
    assert set(snapshot) == {field.name for field in fields(Media)} - {"authorization"}
    assert set(snapshot["qualities"][0]) == {field.name for field in fields(Quality)}
    restored = read_media(encoded)
    assert restored.qualities == (replace(quality, endpoint="", fallback_endpoint=None),)
    assert restored.account_id == "local-label" and restored.requires_refresh
    snapshot["qualities"][0]["key"] = "different"
    assert media.qualities[0].key == "full"


def test_empty_quality_snapshot_round_trips_without_inventing_sources():
    media = Media("youtube", "identity", "Title", "", 0, "https://youtu.be/identity", None, ())
    assert read_media(public_media(media)) == replace(media, requires_refresh=True)
