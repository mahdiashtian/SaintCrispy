"""Curated custom-emoji packs and semantic priorities for downloader messages."""

PREMIUM_EMOJI_PACKS = (
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

APPLICATION_PACKS = ("ApplicationEmoji", "EmojiStatus", "NewsEmoji", "IconsEmoji")
STATUS_PACKS = ("EmojiStatus", "ApplicationEmoji", "EmoticonEmoji", "IconsEmoji")
MEDIA_PACKS = ("ApplicationEmoji", "GameEmoji", "NewsEmoji", "IconsEmoji")
INFORMATION_PACKS = ("NewsEmoji", "ApplicationEmoji", "EmoticonEmoji", "IconsEmoji")

EMOJI_PACK_PREFERENCES = {
    **dict.fromkeys(("📥", "📤", "🔗", "💾", "📋", "⏹", "⚙️", "📎"), APPLICATION_PACKS),
    **dict.fromkeys(("✅", "❌", "⚠️", "⏳", "⏱", "♻️", "⚡"), STATUS_PACKS),
    **dict.fromkeys(("🎵", "🎧", "🎬", "▶️", "📸", "📌"), MEDIA_PACKS),
    **dict.fromkeys(("📬", "📨", "💬", "👤", "ℹ️", "🌐", "📖"), INFORMATION_PACKS),
}
