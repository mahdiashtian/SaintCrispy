"""Body and button coverage with sparse Telegram-style pack metadata."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import Button, types
from test_premium_emoji_catalog import pack

from bot.admin_keyboard import reply_event
from bot.button_icons import BUTTON_EMOJI
from bot.handlers.admin_render import source_reply
from bot.premium_emoji import BotPresentation
from bot.state.storage import MemoryStateStorage
from bot.texts.downloads import DOWNLOAD_COPY, download_caption, inspection_text, quality_menu_text
from bot.texts.guide import TOPICS, topic_text
from bot.texts.messages import help_text, sites_text, start_text
from schemas.media import Media, Quality
from services.conversation import ConversationService

# Representative available alts from the operator packs, deliberately omitting
# photo/person/cleanup/folder/etc. alts so semantic fallback is exercised.
AVAILABLE = (
    "ℹ️ ↪️ ⏱ ⏳ ▶️ ♻️ ⚙️ ⚠️ ⚡️ ⛔️ ✅ ✉️ ✍️ ❌ ❗️ ➕ ➖ ➡️ ⬆️ ⬇️ ⬜️ ▫️ "
    "🌐 🎙 🎧 🎬 🎯 🎵 🏠 🏷 👀 👁 👉 👋 👌 👑 👥 👨‍💻 💡 💬 💼 💻 📅 📈 📊 📋 📌 📍 "
    "📎 📖 📣 📤 📥 📦 🔄 🔎 🔒 🔔 🔗 🔙 🔴 🕒 🖼 🗑 🗓 😀 😕 🙃 🙈 🚀 🚫 🛠 🛡 🟢 🤔 🤖 🧾 0️⃣"
).split()
QUALITY = Quality("original", "Original", "mp4", None, "mp4", "video/mp4", "progressive", "")


@pytest.fixture
async def presentation():
    async def client(request):
        return (
            pack(*((alt, 1000 + index) for index, alt in enumerate(AVAILABLE)))
            if request.stickerset.short_name == "NewsEmoji"
            else pack()
        )

    result = BotPresentation()
    await result.load(client, "IconsEmoji")
    return result


def assert_all_premium(presentation, text):
    rendered, entities = presentation.prepare(text)
    encoded = rendered.encode("utf-16-le")
    actual = {(entity.offset, entity.length) for entity in entities}
    expected = {
        (
            len(rendered[: match.start()].encode("utf-16-le")) // 2,
            len(match.group().encode("utf-16-le")) // 2,
        )
        for match in BUTTON_EMOJI.finditer(rendered)
    }
    assert actual == expected, (text, rendered)
    for entity in entities:
        glyph = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
        assert presentation.emojis[glyph] == entity.document_id
    return rendered, entities


@pytest.mark.parametrize("site", tuple(DOWNLOAD_COPY))
async def test_every_site_menu_inspection_and_caption_has_only_premium_icons(presentation, site):
    media = Media(site, "id", "Title", "Creator", 3, "page", None, (QUALITY,))
    for text in (
        inspection_text(site),
        quality_menu_text(media),
        download_caption(media, QUALITY, "ExampleBot"),
    ):
        assert_all_premium(presentation, text)


async def test_spotify_green_icon_is_a_custom_entity_in_start_help_and_file_copy(presentation):
    texts = (start_text("ExampleBot"), sites_text(), inspection_text("spotify"))
    for text in texts:
        rendered, entities = assert_all_premium(presentation, text)
        offset = len(rendered[: rendered.index("🟢 Spotify")].encode("utf-16-le")) // 2
        assert any(
            entity.offset == offset and entity.document_id == presentation.emojis["🟢"]
            for entity in entities
        )


@pytest.fixture(scope="module")
def ui_glyphs():
    root = Path(__file__).resolve().parents[1] / "bot"
    glyphs = set()
    for path in root.rglob("*.py"):
        if path.name in {"button_icons.py", "premium_emoji.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                glyphs.update(BUTTON_EMOJI.findall(node.value))
    return sorted(glyphs)


async def test_help_topics_and_all_static_interface_symbols_have_premium_coverage(
    presentation, ui_glyphs
):
    for text in (help_text(), *(topic_text(topic) for topic, _ in TOPICS)):
        assert_all_premium(presentation, text)
    # Respect the Telegram entity cap while proving all static sections are covered.
    for index in range(0, len(ui_glyphs), 20):
        assert_all_premium(presentation, " ".join(ui_glyphs[index : index + 20]))


async def test_every_static_button_icon_has_one_premium_slot_and_keeps_its_action(
    presentation, ui_glyphs
):
    for glyph in ui_glyphs:
        original = Button.inline(f"{glyph} Section", b"same-action")
        button = presentation.buttons([[original]])[0][0]
        assert button.style.icon in presentation.button_emojis.values(), glyph
        assert button.text == "Section" and button.type.data == b"same-action"
        assert original.text == f"{glyph} Section" and original.style.icon is None


async def test_html_admin_copy_inherits_wrapped_presentation_and_recalculates_bold_offsets(
    presentation,
):
    actual = SimpleNamespace(sender_id=999, chat_id=999, data=None, respond=AsyncMock())
    adapted = reply_event(actual, ConversationService(MemoryStateStorage()), presentation)
    await source_reply(
        adapted, "<b>📬 Spotify</b>\n🧹 پاکسازی", buttons=[[Button.inline("⬅️", b"same")]]
    )
    text, options = actual.respond.call_args.args[0], actual.respond.call_args.kwargs
    assert "📬" not in text and "🧹" not in text
    assert "Spotify" in text and "پاکسازی" in text
    custom = [
        entity
        for entity in options["formatting_entities"]
        if isinstance(entity, types.MessageEntityCustomEmoji)
    ]
    assert len(custom) == 2
    bold = next(
        entity
        for entity in options["formatting_entities"]
        if isinstance(entity, types.MessageEntityBold)
    )
    assert (
        text.encode("utf-16-le")[bold.offset * 2 : (bold.offset + bold.length) * 2].decode(
            "utf-16-le"
        )
        == "✉️ Spotify"
    )
    button = options["buttons"][0][0]
    assert button.text == "بازگشت" and button.style.icon == presentation.button_emojis["🔙"]
    assert button.type.data == b"same"


def test_native_matching_alt_wins_and_unsupported_content_is_not_substituted():
    presentation = BotPresentation()
    presentation.emojis = {"📸": 101, "🖼": 102, "⚙️": 103}
    text, entities = presentation.prepare("📸 Instagram 🎄")
    assert text == "📸 Instagram 🎄" and [entity.document_id for entity in entities] == [101]


async def test_disabled_custom_emoji_keeps_unicode_text_and_navigation_fallback(presentation):
    presentation.disabled = True
    assert presentation.prepare("🟢 Spotify 📸 Instagram") == ("🟢 Spotify 📸 Instagram", [])
    original = Button.inline("⬅️", b"back")
    button = presentation.buttons([[original]])[0][0]
    assert button.text == "⬅️" and button.style.icon is None and button.type.data == b"back"
