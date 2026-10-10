"""Bounded Telegram header reads for tests with structural media."""

from contextlib import asynccontextmanager

from video_fixture import mp4_header


class HeaderClient:
    @asynccontextmanager
    async def iter_download(self, document, **options):
        async def chunks():
            yield mp4_header()

        yield chunks()
