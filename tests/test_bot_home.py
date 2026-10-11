import re
from types import SimpleNamespace

from downloader_bot.bot.handlers.home import handle_home
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.presentation import quality_buttons
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.schemas.media import Quality


def test_video_choices_pack_top_two_then_three_without_changing_callback_indexes():
    qualities = tuple(
        Quality(str(i), f"MP4 {height}p", "video", None, "mp4", "video/mp4", "progressive", "url")
        for i, height in enumerate((2160, 1440, 1080, 720, 480))
    )
    rows = quality_buttons(qualities, "yt", "a" * 16, start_index=50)
    assert [len(row) for row in rows] == [2, 3]
    buttons = [button for row in rows for button in row]
    assert [button.text for button in buttons] == [q.label for q in qualities]
    assert [button.type.data.decode().split(":")[-1] for button in buttons] == [
        str(i) for i in range(50, 55)
    ]


async def test_active_menu_excludes_another_user_and_another_chat():
    jobs = TransferJobs()
    own = jobs.reserve(10, 20, TransferProgress())
    other_user = jobs.reserve(11, 20, TransferProgress())
    other_chat = jobs.reserve(10, 21, TransferProgress())
    edits = []

    async def edit(text, **options):
        edits.append((text, options))

    async def answer(*args, **options):
        pass

    event = SimpleNamespace(
        sender_id=10,
        chat_id=20,
        answer=answer,
        edit=edit,
        pattern_match=re.match(rb"home:(\d+):(\w+)", b"home:10:active"),
    )
    await handle_home(event, jobs)
    data = [button.type.data for row in edits[0][1]["buttons"] for button in row]
    assert f"stop:{own.token}".encode() in data
    assert all(
        other_user.token.encode() not in item and other_chat.token.encode() not in item
        for item in data
    )
    assert jobs.count == 3


async def test_another_user_cannot_open_the_start_menu():
    answers = []

    async def answer(text, **options):
        answers.append(options)

    async def edit(*args, **options):
        raise AssertionError("Menu should not be edited")

    event = SimpleNamespace(
        sender_id=11,
        chat_id=20,
        answer=answer,
        edit=edit,
        pattern_match=re.match(rb"home:(\d+):(\w+)", b"home:10:help"),
    )
    await handle_home(event)
    assert answers == [{"alert": True}]
