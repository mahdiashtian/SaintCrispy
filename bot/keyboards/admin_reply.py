"""Reply-menu hierarchy mirrors the local Couplyo reference."""

from telethon import types

from bot.texts import admin_buttons as b


def keyboard(rows):
    return types.ReplyKeyboardMarkup(
        rows=[
            types.KeyboardButtonRow(
                buttons=[
                    types.KeyboardButton(
                        text=value,
                        type=types.ButtonTypeDefault(),
                        style=types.KeyboardButtonStyle(bg_danger=True)
                        if value in {b.BACK, b.CANCEL}
                        else None,
                    )
                    for value in reversed(row)
                ]
            )
            for row in rows
        ],
        resize=True,
        persistent=False,
    )


MENUS = {
    "ADMIN_PANEL": [
        [b.ADMINS, b.MESSAGING],
        [b.GROUPS, b.SETTINGS],
        [b.CHANNELS, b.DATA],
        [b.CLEANUP],
        [b.CANCEL],
    ],
    "ADMIN_CAT_ADMINS_PANEL": [
        [b.ADD_ADMIN, b.REMOVE_ADMIN],
        [b.LIST_ADMINS],
        [b.DEVELOPER],
        [b.BACK],
    ],
    "ADMIN_CAT_MSG_PANEL": [
        [b.BROADCAST, b.FORWARD],
        [b.GROUP_BROADCAST, b.GROUP_FORWARD],
        [b.DM_USER, b.DM_GROUP],
        [b.FWD_USER, b.FWD_GROUP],
        [b.JOBS],
        [b.BACK],
    ],
    "ADMIN_GROUPS_PANEL": [
        [b.GROUP_LIST, b.GROUP_HEALTH],
        [b.NUDGE_ADMIN, b.NUDGE_OWNERS],
        [b.GROUP_IMPORT, b.GROUP_MEMBERS],
        [b.GROUP_INFO, b.CRAWL_STATUS],
        [b.GROUP_LEAVE],
        [b.BACK],
    ],
    "ADMIN_CAT_CHANNELS_PANEL": [
        [b.JOIN, b.ARCHIVE],
        [b.STORAGE, b.WATCH],
        [b.PROMO, b.WEEKLY],
        [b.AUTO_NUDGE],
        [b.BACK],
    ],
    "ADMIN_CAT_DATA_PANEL": [
        [b.STATS],
        [b.DAILY, b.HISTORY],
        [b.USERS, b.REFRESH_USERS],
        [b.LOOKUP, b.TRACE],
        [b.BACKUP, b.IMPORT],
        [b.EXPORT_USERS, b.EXPORT_GROUPS],
        [b.IMPORT_USERS, b.CACHE],
        [b.BACK],
    ],
    "ADMIN_CAT_CLEANUP_PANEL": [[b.FILTER_CLEANUP], [b.PURGE_USERS], [b.BACK]],
    "ADMIN_JOIN_PANEL": [
        [b.ADD_CHANNEL, b.REMOVE_CHANNEL],
        [b.LIST_CHANNELS],
        [b.JOIN_POINTS],
        [b.BACK],
    ],
    "ADMIN_ARCHIVE_PANEL": [[b.SET_ARCHIVE, b.REMOVE_ARCHIVE], [b.VIEW_ARCHIVE], [b.BACK]],
    "ADMIN_STORAGE_PANEL": [
        [b.SET_STORAGE, b.REMOVE_STORAGE],
        [b.VIEW_STORAGE],
        [b.STORAGE_KINDS],
        [b.BACK],
    ],
    "ADMIN_WATCH_PANEL": [[b.WATCH_ADD, b.WATCH_LIST], [b.BACK]],
}


def menu(state):
    return keyboard(MENUS[state.value if hasattr(state, "value") else state])


BACK_MENU = keyboard([[b.BACK, b.CANCEL]])
EXCLUSIONS = keyboard([[b.SKIP], [b.BACK, b.CANCEL]])


def toggles(labels, enabled):
    values = [f"{'✅' if key in enabled else '⬜'} {label}" for key, label in labels.items()]
    return keyboard(
        [values[index : index + 2] for index in range(0, len(values), 2)]
        + [[b.ALL_ON, b.ALL_OFF], [b.BACK]]
    )


def schedule(enabled, *, text=False):
    hours = [str(value) for value in range(1, 25)]
    return keyboard(
        [[b.DISABLE if enabled else b.ENABLE]]
        + ([[b.SET_TEXT]] if text else [])
        + [hours[index : index + 6] for index in range(0, 24, 6)]
        + [[b.BACK]]
    )
