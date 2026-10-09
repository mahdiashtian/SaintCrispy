import os

import pytest
from telethon import TelegramClient, types, utils
from telethon.sessions import MemorySession


async def test_typed_menu_upload_and_reuse_media_bypass_telethon_disk_and_metadata_readers(
    monkeypatch,
):
    client = TelegramClient(MemorySession(), 123, "0" * 32)
    media = (
        types.InputMediaPhotoExternal("https://cdn.example/thumbnail.jpg"),
        types.InputMediaDocumentExternal("https://cdn.example/video.mp4"),
        types.InputMediaUploadedDocument(
            types.InputFileBig(1, 1, "video.mp4"),
            "video/mp4",
            [types.DocumentAttributeFilename("video.mp4")],
        ),
        types.InputMediaDocument(types.InputDocument(1, 2, b"reference")),
    )

    def reject(*args, **kwargs):
        pytest.fail("Typed XVideos media must not reach a synchronous disk/metadata reader")

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "isfile", reject)
        patch.setattr(utils, "get_attributes", reject)
        for value in media:
            handle, result, _ = await client._file_to_media(value)
            assert handle is None and result is value
