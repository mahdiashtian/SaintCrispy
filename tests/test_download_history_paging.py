"""Download paging changes only its section, with private fresh authorization."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.extensions import html
from test_admin_reference_database import app as app

from bot.admin_keyboard import AdminReplyEvent
from bot.handlers.download_history import handle_download_page, navigation, show_trace
from bot.premium_emoji import BotPresentation
from bot.texts.couplyo_admin import ADMIN_LOOKUP_AGAIN_HINT, download_history
from repositories.postgres.admin import AdminRepository
from schemas.media import DownloadError


def page(index=0, next_page=False):
    return {
        "download_page": index,
        "has_next": next_page,
        "downloads": [
            {
                "site": "youtube",
                "content_id": f"video-{index}-{n}",
                "quality": "720p",
                "delivery_count": 1,
            }
            for n in range(10)
        ],
    }


@pytest.mark.parametrize(
    "index,has_next,labels",
    [(0, False, []), (0, True, ["➡️"]), (1, True, ["⬅️", "➡️"]), (2, False, ["⬅️"])],
)
def test_navigation_hides_nonexistent_directions(index, has_next, labels):
    assert [
        button.text for row in navigation(7, 8, page(index, has_next)) for button in row
    ] == labels


@pytest.mark.parametrize("index,has_next", [(0, True), (1, True), (2, False)])
async def test_callback_preserves_profile_groups_hint_and_entities(index, has_next):
    prefix, prefix_entities = html.parse(
        '<b>👤 پروفایل ثابت</b>\n<a href="tg://user?id=8">کاربر</a>\nگروه‌ها: قدیمی\n'
    )
    old, old_entities = html.parse(download_history(page()))
    offset = len(prefix.encode("utf-16-le")) // 2
    for entity in old_entities:
        entity.offset += offset
    message = SimpleNamespace(
        raw_text=prefix + old + ADMIN_LOOKUP_AGAIN_HINT, entities=prefix_entities + old_entities
    )
    event = SimpleNamespace(
        sender_id=7,
        chat_id=7,
        is_group=False,
        data=f"dls:7:8:{index}".encode(),
        get_message=AsyncMock(return_value=message),
        answer=AsyncMock(),
        edit=AsyncMock(),
    )
    admin = SimpleNamespace(download_page=AsyncMock(return_value=page(index, has_next)))
    await handle_download_page(event, admin=admin, presentation=BotPresentation())
    admin.download_page.assert_awaited_once_with(7, 8, index)
    edited = event.edit.call_args.args[0]
    assert edited.startswith(prefix)
    assert edited.endswith(ADMIN_LOOKUP_AGAIN_HINT)
    assert f"video-{index}-9" in edited and edited.count("video-") == 10
    formatting = event.edit.call_args.kwargs["formatting_entities"]
    for entity in prefix_entities:
        assert entity.to_dict() in [item.to_dict() for item in formatting]
    assert len(event.edit.call_args_list) == 1


@pytest.mark.parametrize("sender,chat,denied", [(9, 9, False), (7, -100123, False), (7, 7, True)])
async def test_other_owners_groups_and_revoked_admins_cannot_read_or_edit(sender, chat, denied):
    event = SimpleNamespace(
        sender_id=sender,
        chat_id=chat,
        is_group=chat < 0,
        data=b"dls:7:8:1",
        answer=AsyncMock(),
        edit=AsyncMock(),
    )
    admin = SimpleNamespace(download_page=AsyncMock(side_effect=DownloadError("دسترسی نداری")))
    await handle_download_page(event, admin=admin)
    assert admin.download_page.await_count == int(denied)
    event.edit.assert_not_awaited()
    assert event.answer.call_args.kwargs["alert"]


async def test_empty_forged_page_does_not_destroy_visible_history():
    event = SimpleNamespace(
        sender_id=7,
        chat_id=7,
        is_group=False,
        data=b"dls:7:8:99",
        answer=AsyncMock(),
        edit=AsyncMock(),
    )
    admin = SimpleNamespace(download_page=AsyncMock(return_value={"downloads": []}))
    await handle_download_page(event, admin=admin)
    event.edit.assert_not_awaited()


async def test_database_uses_one_extra_row_and_stable_bot_scoped_order():
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[{"id": n} for n in range(11)]))
    result = await AdminRepository(connection, 55).download_page(8, 2)
    assert len(result["downloads"]) == 10 and result["has_next"]
    sql, *args = connection.fetch.call_args.args
    assert args == [55, 8, 11, 20]
    assert "ORDER BY last_delivered_at DESC,site,content_id,quality" in sql


async def test_real_database_twenty_five_downloads_have_three_disjoint_pages(app):
    await app.pool.execute(
        """INSERT INTO user_media_history
        (telegram_account_id,user_id,site,content_id,quality,last_method)
        SELECT $1,8,'youtube','video-'||lpad(n::text,2,'0'),'720p','reused' FROM generate_series(0,24) n""",
        app.bot,
    )
    await app.pool.execute(
        """INSERT INTO user_media_history
        (telegram_account_id,user_id,site,content_id,quality,last_method)
        VALUES ($1,9,'youtube','other-user','720p','reused'),($2,8,'youtube','other-bot','720p','reused')""",
        app.bot,
        app.bot + 1,
    )
    pages = [await app.admin.download_page(999, 8, index) for index in range(3)]
    assert [len(item["downloads"]) for item in pages] == [10, 10, 5]
    assert [item["has_next"] for item in pages] == [True, True, False]
    ids = [row["content_id"] for item in pages for row in item["downloads"]]
    assert ids == [f"video-{n:02d}" for n in range(25)]


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("invocation", [False, True])
async def test_initial_reply_admin_trace_keeps_history_in_one_inline_message(long, invocation):
    now = datetime.now(UTC)
    profile = dict(
        user_id=8,
        first_name="User",
        username=None,
        is_started=True,
        is_blocked=False,
        is_admin=False,
        is_banned=False,
        first_seen_at=now,
        started_at=now,
        last_seen_at=now,
        last_active_at=now,
    )
    groups = (
        [
            dict(
                title="گروه" * 50,
                chat_id=-n - 1,
                is_active=True,
                last_seen_at=now,
                total_groups=25,
                active_groups=25,
            )
            for n in range(25)
        ]
        if long
        else []
    )
    data = {**page(0, True), "profile": profile, "groups": groups}
    actual = SimpleNamespace(sender_id=7, respond=AsyncMock())
    conversations = SimpleNamespace(update_data=AsyncMock())
    adapted = AdminReplyEvent(actual, conversations)
    if invocation:
        from bot.handlers.admin_reference import Invocation

        adapted = Invocation(adapted, "trace", "8")
    await show_trace(
        adapted,
        data,
        presentation=BotPresentation(),
        hint=ADMIN_LOOKUP_AGAIN_HINT,
    )
    text = actual.respond.call_args.args[0]
    assert text.count("video-") == 10
    assert len(text.encode("utf-16-le")) // 2 <= 4000
    assert actual.respond.call_args.kwargs["buttons"][0][0].type.data == b"dls:7:8:1"
    conversations.update_data.assert_not_awaited()
