"""Live switches, parent navigation and reference message delivery regressions."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from test_download_settings import admin, event, send

from bot.button_icons import button_label
from bot.handlers import admin as ui
from bot.handlers import admin_reference as panel
from bot.keyboards import admin_reply as keys
from bot.premium_emoji import BotPresentation
from bot.state.storage import MemoryStateStorage
from bot.texts import admin_buttons as b
from bot.texts import promos
from core.enums import ConversationState as CS
from services.conversation import ConversationService


@pytest.mark.parametrize(
    "name,key,label",
    [
        (name, key, label)
        for name, labels in (("join_points", b.POINTS), ("storage_kinds", b.KINDS))
        for key, label in labels.items()
    ],
)
@pytest.mark.parametrize("premium", [False, True])
async def test_forced_join_and_storage_switches_route_their_new_rendered_labels(
    name, key, label, premium
):
    service = admin()
    service.channels = SimpleNamespace(modify=AsyncMock(return_value=[key]))
    conversations = ConversationService(MemoryStateStorage())
    await conversations.set_state(
        999, 999, CS.ADMIN_INPUT, action="toggles", key=name, parent=CS.ADMIN_JOIN_PANEL.value
    )
    actual = event(button_label(label) if premium else "⬜ " + button_label(label))
    await send(actual, service, conversations)
    service.channels.modify.assert_awaited_once()
    owner, field, transform = service.channels.modify.call_args.args
    assert (owner, field) == (999, name)
    assert transform([]) == [key] and transform([key]) == []
    assert "تنظیم ذخیره شد" in actual.respond.call_args.args[0]


@pytest.mark.parametrize("label,field", keys.DOWNLOAD_SWITCHES.items())
@pytest.mark.parametrize("premium", [False, True])
async def test_live_switch_text_is_fixed_and_state_icon_changes_after_each_tap(
    label, field, premium
):
    service = admin()
    values = service.options.return_value
    values[field] = True
    actions = {
        **panel.RATE_TOGGLES,
        b.DOWNLOAD_TOGGLE: "pause",
        b.NOTIFICATIONS_TOGGLE: "notifications",
    }

    async def change(owner, action, *, value):
        assert owner == 999 and action == actions[label]
        values[field] = value

    service.mutate.side_effect = change
    conversations = ConversationService(MemoryStateStorage())
    await conversations.set_state(999, 999, CS.ADMIN_DOWNLOAD_SETTINGS)
    presentation = BotPresentation()
    if premium:
        presentation.button_emojis = {"✅": 101, "⬜": 102}
    for marker, expected in (("✅", False), ("⬜", True)):
        actual = event(f"{marker} {label}" if not premium else label)
        from telethon import events

        with pytest.raises(events.StopPropagation):
            await panel.handle_message(actual, service, conversations, presentation)
        markup = actual.respond.call_args.kwargs["buttons"]
        item = next(item for row in markup.rows for item in row.buttons if label in item.text)
        assert "روشن/خاموش" not in item.text
        assert values[field] is expected
        if premium:
            assert item.text == label
            assert item.style.icon == (101 if expected else 102)
        else:
            assert item.text == f"{'✅' if expected else '⬜'} {label}"


def test_missing_checkbox_pack_preserves_visible_state_instead_of_an_unrelated_premium_icon():
    presentation = BotPresentation()
    presentation.button_emojis = {"⚙️": 123, "📥": 456}
    for enabled in (False, True):
        markup = presentation.buttons(keys.download_settings({"downloads_enabled": enabled}))
        item = next(
            item for row in markup.rows for item in row.buttons if b.DOWNLOAD_TOGGLE in item.text
        )
        assert item.text == f"{'✅' if enabled else '⬜'} {b.DOWNLOAD_TOGGLE}"
        assert not item.style.icon


@pytest.mark.parametrize("state", keys.MENUS)
def test_cancel_exists_only_at_the_root_and_all_other_menus_end_in_back(state):
    labels = [item for row in keys.MENUS[state] for item in row]
    assert (b.CANCEL in labels) is (state == CS.ADMIN_PANEL)
    for markup in (keys.BACK_MENU, keys.EXCLUSIONS):
        assert all(b.CANCEL != item.text for row in markup.rows for item in row.buttons)


async def test_lookup_result_keeps_back_and_never_abandons_input_on_stale_cancel():
    service = admin()
    service.user = AsyncMock(return_value=SimpleNamespace(user_id=7))
    conversations = ConversationService(MemoryStateStorage())
    await panel.ask(event(), service, conversations, "lookup", CS.ADMIN_CAT_DATA_PANEL)
    await send(event(b.CANCEL), service, conversations)
    assert await conversations.get_state(999, 999) is CS.ADMIN_INPUT
    await send(event(b.BACK), service, conversations)
    assert await conversations.get_state(999, 999) is CS.ADMIN_CAT_DATA_PANEL
    await send(event(b.DATA_TOOLS), service, conversations)
    assert await conversations.get_state(999, 999) is CS.ADMIN_DATA_TOOLS_PANEL
    await send(event(b.BACK), service, conversations)
    assert await conversations.get_state(999, 999) is CS.ADMIN_CAT_DATA_PANEL


async def test_read_results_do_not_append_a_management_home_button():
    actual, service = event(), admin()
    actual.data = b"adm:999:stats:0"
    await ui.callback(actual, service, conversations=ConversationService(MemoryStateStorage()))
    assert actual.edit.call_args.kwargs["buttons"] == []


async def test_backup_menu_sends_the_complete_archive_while_its_private_file_is_alive(tmp_path):
    path = tmp_path / "complete.dump"
    path.write_bytes(b"PGDMPcomplete")
    service = admin()
    legacy = AsyncMock()

    @asynccontextmanager
    async def backup(owner):
        assert owner == 999
        try:
            yield path
        finally:
            path.unlink()

    service.maintenance = SimpleNamespace(full_backup=backup, backup=legacy)
    actual = event(b.BACKUP)

    async def sent(text, *, file, **options):
        assert path.is_file() and file == str(path)
        assert "کامل" in text and options["force_document"]

    actual.respond.side_effect = sent
    await panel.menu_action(actual, service, None, CS.ADMIN_CAT_DATA_PANEL, b.BACKUP, None)
    assert not path.exists()
    legacy.assert_not_awaited()


def test_promo_and_admin_reminder_pools_preserve_reference_structure_and_download_domain():
    assert [
        len(pool)
        for pool in (
            promos.GROUP_PROMO,
            promos.GROUP_NEED_ADMIN,
            promos.OWNER_NUDGE,
            promos.GROUP_ADMIN_NUDGE,
            promos.GROUP_ADMIN_DETECTED,
        )
    ] == [40, 25, 16, 10, 8]
    for pool in (
        promos.GROUP_PROMO,
        promos.GROUP_NEED_ADMIN,
        promos.OWNER_NUDGE,
        promos.GROUP_ADMIN_NUDGE,
        promos.GROUP_ADMIN_DETECTED,
    ):
        assert not any("کاپل" in line or "شیپ" in line for line in pool)
    group = SimpleNamespace(owner_id=7, title="<گروه & تست>", chat_id=-10, is_admin=False)
    assert promos.promo(group, 0).startswith("کسی زنده‌ست؟")
    assert "ادمین" not in promos.promo(group, 0)
    assert "tg://user?id=7" in promos.need_admin(group, 0)
    assert "{admins}" not in promos.need_admin(group, 0)
    assert "&lt;گروه &amp; تست&gt;" in promos.removed_nudge(group, 7, 0)
    assert "گروه &" not in promos.removed_nudge(group, 8, 0, with_name=False)


async def test_admin_nudge_uses_reference_pool_html_entities_and_the_same_retry_identity():
    from bot.operations import EffectDelivery
    from schemas.admin import PendingEffect
    from schemas.group import GroupStanding, Standing

    group = SimpleNamespace(owner_id=7, title="Test", chat_id=-10, migrated_to=None, is_active=True)
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChat(10)
    tracker = SimpleNamespace(health=AsyncMock(return_value=GroupStanding(Standing.MEMBER)))
    delivery = EffectDelivery(client, tracker, SimpleNamespace(get=AsyncMock(return_value=group)))
    effect = PendingEffect(1, "nudge_admin", -10, {}, 123, None, 1)
    await delivery.deliver(effect)
    await delivery.deliver(effect)
    first, second = [call.args[0] for call in client.await_args_list]
    assert first.message == second.message
    assert first.random_id == second.random_id == 123
    assert any(isinstance(item, types.MessageEntityTextUrl) for item in first.entities)
    assert "<a" not in first.message
