import asyncio
from types import SimpleNamespace

import pytest
from telethon import types

from downloader_bot.bot.handlers.quality import handle_quality
from downloader_bot.bot.handlers.stop import handle_stop
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.state.menus import MenuStore
from downloader_bot.schemas.media import Media, Quality

QUALITY = Quality("aac_160", "AAC 160", "aac", 160, "m4a", "audio/mp4", "hls", "endpoint")
MEDIA = Media("soundcloud", "track", "Title", "Artist", 30, "url", None, (QUALITY,))


def stop_event(job, owner=1, chat=2):
    answers, edits = [], []

    async def answer(text, **kwargs):
        answers.append((text, kwargs))

    async def edit(text, **kwargs):
        edits.append((text, kwargs))

    return (
        SimpleNamespace(
            sender_id=owner,
            chat_id=chat,
            pattern_match=[None, job.token.encode()],
            answer=answer,
            edit=edit,
        ),
        answers,
        edits,
    )


async def test_stop_callback_cancels_queued_work_and_removes_the_button():
    jobs = TransferJobs()
    job = jobs.reserve(1, 2, TransferProgress())
    jobs.start(job, lambda: asyncio.sleep(0))
    event, answers, edits = stop_event(job)
    await handle_stop(event, jobs)
    await jobs.join()
    assert job.cancelled and jobs.count == 0
    assert "پذیرفته" in answers[0][0]
    assert "لغو" in edits[0][0]
    assert isinstance(edits[0][1]["buttons"], types.ReplyInlineMarkup)
    assert edits[0][1]["buttons"].rows == []


@pytest.mark.parametrize(("owner", "chat"), [(3, 2), (1, 4)])
async def test_stop_callback_rejects_another_owner_or_chat(owner, chat):
    jobs = TransferJobs()
    job = jobs.reserve(1, 2, TransferProgress())
    event, answers, edits = stop_event(job, owner, chat)
    await handle_stop(event, jobs)
    assert answers[0][1]["alert"] and not edits
    assert not job.cancelled and jobs.count == 1


async def test_stop_callback_does_not_interrupt_cache_persistence():
    jobs = TransferJobs()
    job = jobs.reserve(1, 2, TransferProgress(phase="saving"))
    event, answers, edits = stop_event(job)
    await handle_stop(event, jobs)
    assert answers[0][1]["alert"] and not edits and not job.cancelled


async def test_quality_handler_returns_while_transfer_runs_and_stop_cleans_it_up():
    ready, closed = asyncio.Event(), asyncio.Event()
    statuses, answers, edits = [], [], []
    menus = MenuStore()
    token = menus.add(1, 2, MEDIA)

    async def answer(text, **kwargs):
        answers.append(text)

    async def edit(text, **kwargs):
        edits.append((text, kwargs))

    async def respond(text, **kwargs):
        statuses.append((text, kwargs))
        return SimpleNamespace(edit=edit)

    async def get_peer():
        return types.InputPeerUser(1, 2)

    async def deliver(peer, media, quality, progress):
        progress.phase = "streaming"
        progress.method = "hls"
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    event = SimpleNamespace(
        sender_id=1,
        chat_id=2,
        pattern_match=[None, token.encode(), b"0"],
        answer=answer,
        respond=respond,
        get_input_chat=get_peer,
    )
    async with TransferJobs() as jobs:
        await asyncio.wait_for(
            handle_quality(event, SimpleNamespace(deliver=deliver), menus, jobs),
            1,
        )
        assert statuses[0][1]["buttons"][0][0].type.data.startswith(b"stop:")
        stop_token = statuses[0][1]["buttons"][0][0].type.data.split(b":")[1].decode()
        job = jobs.get(stop_token, 1, 2)
        await asyncio.wait_for(ready.wait(), 1)
        stop, _, _ = stop_event(job)
        await handle_stop(stop, jobs)
        async with asyncio.timeout(1):
            await jobs.join()
        assert closed.is_set()
    assert "لغو" in edits[-1][0]
    assert edits[-1][1]["buttons"].rows == []


async def test_stop_between_message_creation_and_enqueue_never_starts_the_transfer():
    menus, jobs = MenuStore(), TransferJobs()
    token = menus.add(1, 2, MEDIA)
    edits = []

    async def answer(*args, **kwargs):
        pass

    async def edit(text, **kwargs):
        edits.append(text)

    async def respond(text, **kwargs):
        stop_token = kwargs["buttons"][0][0].type.data.split(b":")[1].decode()
        jobs.cancel(stop_token, 1, 2)
        return SimpleNamespace(edit=edit)

    async def deliver(*args):
        pytest.fail("A cancelled job must not begin resolving or delivering media")

    event = SimpleNamespace(
        sender_id=1,
        chat_id=2,
        pattern_match=[None, token.encode(), b"0"],
        answer=answer,
        respond=respond,
    )
    await handle_quality(event, SimpleNamespace(deliver=deliver), menus, jobs)
    assert jobs.count == 0 and "لغو" in edits[-1]
