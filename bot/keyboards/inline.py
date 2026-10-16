from itertools import groupby

from telethon import Button

from schemas.media import Quality


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
    """Pack provider-ordered choices without changing their stored callback indexes."""
    rows = []
    for _, group in groupby(
        enumerate(qualities, start_index), key=lambda item: item[1].mime_type.split("/", 1)[0]
    ):
        choices = list(group)
        is_video = choices[0][1].mime_type.startswith("video/")
        offset = 0
        while offset < len(choices):
            # Put the first two video qualities together, then pack lower choices in threes.
            count = 2 if (is_video and offset == 0) or len(choices) - offset == 4 else 3
            rows.append(
                [
                    Button.inline(quality.label, data=f"{prefix}:{token}:{index}")
                    for index, quality in choices[offset : offset + count]
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
