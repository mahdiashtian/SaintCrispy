"""Semantic pack selection, bounded loading and safe premium presentation."""

import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors, types

from bot.button_icons import PREMIUM_EMOJI_PACKS
from bot.premium_emoji import BotPresentation, pack_names
from bot.progress import ProgressReporter, TransferProgress


def pack(*items):
    return SimpleNamespace(
        documents=[
            SimpleNamespace(
                id=identity,
                attributes=[
                    types.DocumentAttributeCustomEmoji(
                        alt=alt,
                        stickerset=types.InputStickerSetEmpty(),
                        free=True,
                    )
                ],
            )
            for alt, identity in items
        ]
    )


def test_catalog_contains_all_thirteen_requested_pack_names():
    assert PREMIUM_EMOJI_PACKS == (
        "Emoji_fan103_by_TgEmodziBot",
        "idton",
        "ApplicationEmoji",
        "Gem_Pay3",
        "FinanceEmoji",
        "nutchnll_by_TgEmojis_bot",
        "EmoticonEmoji",
        "pk_2018495_by_EmojiRuBot",
        "EmojiStatus",
        "GameEmoji",
        "NewsEmoji",
        "ChristmasIcons",
        "LoveIcons",
    )
    assert len(pack_names("IconsEmoji")) == 14


def test_pack_urls_are_normalized_and_case_insensitive_duplicates_are_removed():
    names = pack_names("https://t.me/addemoji/ApplicationEmoji,applicationemoji, idton")
    assert names[:2] == ("ApplicationEmoji", "idton")
    assert len(names) == 14
    assert "example.org" not in pack_names("https://example.org/addemoji/private")
    assert pack_names("  ") == ()


async def test_actual_matching_alts_choose_semantic_packs_independently_of_network_order():
    async def client(request):
        name = request.stickerset.short_name
        await asyncio.sleep(0.001 if name == "EmojiStatus" else 0)
        if name == "ApplicationEmoji":
            return pack(("📥", 101), ("✅", 102))
        if name == "EmojiStatus":
            return pack(("✅", 201), ("📥", 202))
        if name == "ChristmasIcons":
            return pack(("📥", 301), ("✅", 302), ("🎄", 303))
        return pack()

    presentation = BotPresentation()
    await presentation.load(client, "IconsEmoji")
    assert presentation.emojis == {"📥": 101, "✅": 201}
    assert presentation.sources == {"📥": "ApplicationEmoji", "✅": "EmojiStatus"}
    assert "ChristmasIcons" in presentation.pack_emojis


async def test_operator_pack_has_priority_and_missing_packs_do_not_hide_working_choices():
    async def client(request):
        name = request.stickerset.short_name
        if name == "Preferred":
            return pack(("📥", 999))
        if name == "ApplicationEmoji":
            return pack(("📥", 101), ("🔗", 102))
        raise errors.StickersetInvalidError(request=None)

    presentation = BotPresentation()
    await presentation.load(client, "Preferred")
    assert presentation.emojis == {"📥": 999, "🔗": 102}


async def test_pack_network_work_is_bounded_and_every_pack_is_requested_once():
    active, peak, requested = 0, 0, []

    async def client(request):
        nonlocal active, peak
        requested.append(request.stickerset.short_name)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return pack(("✅", len(requested)))

    presentation = BotPresentation()
    await presentation.load(client, "IconsEmoji")
    assert peak == 3 and len(requested) == len(set(requested)) == 14
    assert active == 0


async def test_empty_configuration_disables_all_pack_requests():
    async def client(request):
        pytest.fail("Disabled emojis must not use Telegram metadata requests")

    await BotPresentation().load(client, "")


async def test_background_loading_does_not_block_startup_and_cancels_on_shutdown():
    entered, cancelled = asyncio.Event(), []

    async def client(request):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.append(request.stickerset.short_name)

    presentation = BotPresentation()
    async with presentation.loading(client, "IconsEmoji"):
        await asyncio.wait_for(entered.wait(), 1)
        assert not presentation.emojis
    assert len(cancelled) == 3


async def test_flood_wait_pauses_remaining_pack_calls_without_sleeping_the_bot():
    calls = []

    async def client(request):
        calls.append(request)
        raise errors.FloodWaitError(request=None, capture=600)

    await asyncio.wait_for(BotPresentation().load(client, "IconsEmoji"), 1)
    assert len(calls) == 1


def test_variation_selectors_render_exact_document_alt_and_preserve_utf16_positions():
    presentation = BotPresentation()
    presentation.emojis = {"⚠": 101, "▶️": 102}
    text, entities = presentation.prepare("📬 ⚠️ خطا ▶ ویدیو")
    assert text == "📬 ⚠ خطا ▶️ ویدیو"
    assert [entity.document_id for entity in entities] == [101, 102]
    for entity, glyph in zip(entities, ("⚠", "▶️"), strict=True):
        assert entity.offset == len(text[: text.index(glyph)].encode("utf-16-le")) // 2
        assert entity.length == len(glyph.encode("utf-16-le")) // 2


def test_partial_skin_tone_or_joined_emoji_is_not_replaced():
    presentation = BotPresentation()
    presentation.emojis = {"👍": 101, "👩": 102, "💻": 103}
    text = "👍🏽 👩‍💻"
    assert presentation.prepare(text) == (text, [])


async def test_progress_edits_keep_premium_entities_and_do_not_repeat_pack_network_calls():
    calls = []

    async def edit(text, **options):
        calls.append((text, options))

    async def respond(text, **options):
        return SimpleNamespace(edit=edit, id=77)

    presentation = BotPresentation()
    presentation.emojis = {"📥": 101, "📤": 102, "✅": 103}
    progress = TransferProgress()
    message = await presentation.respond(SimpleNamespace(respond=respond), progress.text())
    reporter = ProgressReporter(message, progress)
    progress.phase, progress.method = "done", "progressive"
    await reporter._edit()
    assert [entity.document_id for entity in calls[0][1]["formatting_entities"]] == [103]


async def test_edit_privilege_failure_falls_back_once_and_keeps_status_working():
    calls = []

    async def edit(text, **options):
        calls.append(options)
        if "formatting_entities" in options:
            raise errors.PremiumAccountRequiredError(request=None)

    presentation = BotPresentation()
    presentation.emojis = {"✅": 101}
    message = SimpleNamespace(edit=edit)
    await presentation.edit(message, "✅ ارسال شد")
    await presentation.edit(message, "✅ کامل شد")
    assert len(calls) == 3 and presentation.disabled
    assert "formatting_entities" not in calls[-1]


async def test_explicit_caller_entities_are_preserved_without_offset_changes():
    calls = []
    original = [types.MessageEntityBold(0, 4)]

    async def respond(text, **options):
        calls.append((text, options))

    presentation = BotPresentation()
    presentation.emojis = {"⚠": 101}
    await presentation.respond(
        SimpleNamespace(respond=respond), "⚠️ تست", formatting_entities=original
    )
    assert calls[0][0] == "⚠️ تست" and calls[0][1]["formatting_entities"] is original
