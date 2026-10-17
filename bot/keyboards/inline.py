from itertools import groupby

from telethon import Button

from bot.deeplinks import normalize_bot_username
from schemas.media import Quality


def add_to_group_buttons(bot_username):
    username = normalize_bot_username(bot_username)
    return (
        [[Button.url("➕ منو ببر تو گروهت", f"https://t.me/{username}?startgroup=start")]]
        if username
        else None
    )


def start_buttons(owner_id: int):
    return [
        [
            Button.inline("📖 راهنما", f"home:{owner_id}:help"),
            Button.inline("🌐 سایت‌ها", f"home:{owner_id}:sites"),
        ],
        [Button.inline("📥 درخواست‌های من", f"home:{owner_id}:active")],
    ]


def quality_buttons(
    qualities: tuple[Quality, ...], prefix: str, token: str, *, start_index: int = 0
) -> list[list[Button]]:
    """Feature the first choice and mirror pairs while retaining callback indexes."""
    rows = []
    for _, group in groupby(
        enumerate(qualities, start_index), key=lambda item: item[1].mime_type.split("/", 1)[0]
    ):
        choices = list(group)
        offset = 0
        while offset < len(choices):
            count = 1 if offset == 0 and len(choices) > 2 else 2
            rows.append(
                [
                    Button.inline(quality.label, data=f"{prefix}:{token}:{index}")
                    for index, quality in reversed(choices[offset : offset + count])
                ]
            )
            offset += count
    return rows


def help_buttons(owner_id: int, *, topic="home"):
    from bot.texts.guide import BACK, TOPICS

    if topic != "home":
        return [[Button.inline(BACK, f"help:{owner_id}:home")]]
    buttons = [Button.inline(label, f"help:{owner_id}:{key}") for key, label in TOPICS]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
