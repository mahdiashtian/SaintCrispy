"""Curated custom-emoji packs and semantic priorities for downloader messages."""

import re

# Match complete decorations, including joined emoji, flags and keycap sequences.
_EMOJI_BASE = (
    r"[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9-\u21aa"
    r"\u231a-\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2"
    r"\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u27bf\u2934-\u2935"
    r"\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299"
    r"\U0001f000-\U0001faff]"
)
_EMOJI_PART = _EMOJI_BASE + r"[\ufe0e\ufe0f\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]*"
BUTTON_EMOJI = re.compile(
    r"[0-9#*]\ufe0f?\u20e3|" + _EMOJI_PART + r"(?:\u200d" + _EMOJI_PART + r")*"
)


def button_label(text: str) -> str:
    """Use the dedicated icon slot without duplicating Unicode decorations in the label."""
    label = BUTTON_EMOJI.sub("", text).strip()
    # Telegram requires text even when its dedicated premium icon slot is used.
    return label or EMOJI_BUTTON_LABELS.get(text.replace("\ufe0f", "").replace("\ufe0e", ""), text)


EMOJI_BUTTON_LABELS = {
    "⬅": "بازگشت",
    "➡": "بعدی",
    "◀": "قبلی",
    "▶": "بعدی",
    "⬆": "بالا",
    "⬇": "پایین",
    "🔙": "بازگشت",
    "🔄": "تازه‌سازی",
    "✅": "تأیید",
    "❌": "لغو",
    "✖": "بستن",
    "➕": "افزودن",
    "➖": "حذف",
    "⏹": "توقف",
    "⏩": "ادامه",
}


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
    **dict.fromkeys(
        (
            "📥",
            "📤",
            "🔗",
            "💾",
            "📋",
            "⏹",
            "⚙️",
            "📎",
            "↩️",
            "↪️",
            "⌨️",
            "⏩",
            "◀️",
            "➡️",
            "⬅️",
            "⬆️",
            "⬇️",
            "🔙",
            "🔄",
            "🔁",
            "🛠",
            "📁",
            "📄",
            "📜",
            "🗂️",
            "🗃️",
            "🗄",
            "🗑️",
            "🧹",
            "🧾",
            "📦",
            "📊",
            "📈",
            "📅",
            "🗓️",
            "🩺",
            "📢",
            "📣",
            "🔎",
            "📡",
            "🔐",
            "🔒",
            "🔢",
            "🆔",
            "🏷️",
            "💻",
            "💼",
            "👑",
            "🛡️",
            "👨‍💻",
            "🕵️",
            "🕷",
            "0️⃣",
        ),
        APPLICATION_PACKS,
    ),
    **dict.fromkeys(
        (
            "✅",
            "❌",
            "⚠️",
            "⏳",
            "⏱",
            "♻️",
            "⚡",
            "🟢",
            "🔴",
            "⚪️",
            "☐",
            "⬜",
            "▫️",
            "⏸",
            "⛔️",
            "🚫",
            "❗️",
            "✖️",
            "➕",
            "➖",
            "🕐",
            "🕒",
            "🐢",
        ),
        STATUS_PACKS,
    ),
    **dict.fromkeys(
        (
            "🎵",
            "🎧",
            "🎬",
            "▶️",
            "📸",
            "📌",
            "📽️",
            "🎞️",
            "🎙️",
            "🖼️",
            "📷",
        ),
        MEDIA_PACKS,
    ),
    **dict.fromkeys(
        (
            "📬",
            "📨",
            "💬",
            "👤",
            "ℹ️",
            "🌐",
            "📖",
            "✉️",
            "✍️",
            "📩",
            "📭",
            "📍",
            "👥",
            "👋",
            "👇",
            "👉",
            "👀",
            "👁️",
            "👌",
            "💡",
            "🎯",
            "🚀",
            "🚪",
            "😄",
            "😀",
            "🙂",
            "😕",
            "🙃",
            "🙈",
            "🙋",
            "🙌",
            "🙏",
            "🤔",
            "🤖",
            "🤷",
            "🏠",
            "🔔",
            "🔹",
        ),
        INFORMATION_PACKS,
    ),
}

# Use an available related icon when no document has the requested Unicode alt.
# The rendered text must use that document's real alt, never a mismatched entity.
EMOJI_FALLBACKS = {
    "↩️": ("🔙",),
    "⌨️": ("💻",),
    "⏩": ("➡️",),
    "⏹": ("❌",),
    "◀️": ("🔙",),
    "☐": ("⬜",),
    "⚪️": ("⬜",),
    "✖️": ("❌",),
    "⬅️": ("🔙",),
    "🆔": ("🏷️",),
    "🎞️": ("🎬",),
    "🐢": ("⏳",),
    "👇": ("👉",),
    "👤": ("👥",),
    "💾": ("📦",),
    "📁": ("💼",),
    "📄": ("📋",),
    "📜": ("📖",),
    "📡": ("🌐",),
    "📢": ("📣",),
    "📨": ("✉️",),
    "📩": ("✉️",),
    "📬": ("✉️",),
    "📭": ("✉️",),
    "📸": ("📷", "🖼️"),
    "📽️": ("🎬",),
    "🔁": ("🔄",),
    "🔐": ("🔒",),
    "🔢": ("0️⃣",),
    "🔹": ("▫️",),
    "🕐": ("🕒",),
    "🕵️": ("🔎",),
    "🕷": ("🔎",),
    "🗂️": ("💼",),
    "🗃️": ("📦",),
    "🗄": ("📦",),
    "😄": ("😀",),
    "🙂": ("😀",),
    "🙋": ("👋",),
    "🙌": ("👌", "👋"),
    "🙏": ("👋", "👌"),
    "🚪": ("↪️",),
    "🤷": ("🤔",),
    "🧹": ("🗑️",),
    "🩺": ("🛠",),
}
