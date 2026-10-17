# Premium emoji catalog

The bot includes all thirteen packs supplied by the operator:

- [Emoji_fan103_by_TgEmodziBot](https://t.me/addemoji/Emoji_fan103_by_TgEmodziBot)
- [idton](https://t.me/addemoji/idton)
- [ApplicationEmoji](https://t.me/addemoji/ApplicationEmoji)
- [Gem_Pay3](https://t.me/addemoji/Gem_Pay3)
- [FinanceEmoji](https://t.me/addemoji/FinanceEmoji)
- [nutchnll_by_TgEmojis_bot](https://t.me/addemoji/nutchnll_by_TgEmojis_bot)
- [EmoticonEmoji](https://t.me/addemoji/EmoticonEmoji)
- [pk_2018495_by_EmojiRuBot](https://t.me/addemoji/pk_2018495_by_EmojiRuBot)
- [EmojiStatus](https://t.me/addemoji/EmojiStatus)
- [GameEmoji](https://t.me/addemoji/GameEmoji)
- [NewsEmoji](https://t.me/addemoji/NewsEmoji)
- [ChristmasIcons](https://t.me/addemoji/ChristmasIcons)
- [LoveIcons](https://t.me/addemoji/LoveIcons)

`bot/button_icons.py` owns the catalog and semantic priorities. After
authentication the adapter queries `messages.getStickerSet` and selects actual
`DocumentAttributeCustomEmoji` IDs. IDs are not invented or copied from unverified
previews. Public addemoji pages alone cannot prove every document is available to
the deployment account.

| Meaning | Symbols | Preferred matching packs |
| --- | --- | --- |
| Download, upload, links, storage and controls | 📥 📤 🔗 💾 📋 ⏹ ⚙️ 📎 | ApplicationEmoji, EmojiStatus, NewsEmoji |
| Success, failure, warning, waiting, Spotify and reuse | ✅ ❌ ⚠️ ⏳ ⏱ ♻️ ⚡ 🟢 | EmojiStatus, ApplicationEmoji, EmoticonEmoji |
| Music, video, photos and provider menus | 🎵 🎧 🎬 ▶️ 📸 📌 📽️ 🎞️ | ApplicationEmoji, GameEmoji, NewsEmoji |
| Help, information, replies and identity | 📬 📨 💬 👤 ℹ️ 🌐 📖 | NewsEmoji, ApplicationEmoji, EmoticonEmoji |

Selection first compares documents with the same Unicode meaning and is independent
of request completion order. When no exact document exists, a curated related icon
is selected: for example, a camera can become a picture, a person becomes a group,
and cleanup becomes a trash can. The displayed Unicode changes to the selected
document's actual alt. An exact matching icon always takes precedence. The remaining
supplied packs provide matching alternatives; unrelated finance/seasonal symbols
are not forced into status copy.
`IconsEmoji` remains the legacy fallback. This is a semantic policy, not a claim
that all artwork has been visually inspected.

`CUSTOM_EMOJI_SET=IconsEmoji` automatically includes the thirteen packs. The setting
can contain comma-separated names or addemoji URLs; explicit operator packs take
priority. Names are validated and deduplicated case-insensitively. Empty disables
all metadata requests and uses normal Unicode.

Loading is a managed background task: at most three requests concurrently and
four seconds per request. It does not block readiness. A failed/malformed pack is
isolated; FloodWait pauses remaining catalog requests without sleeping the receiver.
Shutdown cancels and awaits the loader. Replies and progress edits use in-memory
selection and cached regexes, with no per-message pack queries.

Start/help/provider replies, home-menu edits, progress updates and delivered-file
captions use the same adapter and loaded catalog. New external/streamed files and
cached resends receive custom entities through the common publication path.
Buttons use their dedicated custom-emoji icon slot and remove the ordinary emoji
from the label, so there is only one icon. Emoji-only navigation gets a readable
label such as “بازگشت” or “بعدی”. Existing callback identities, ownership checks,
colors and transfer behavior are preserved. Callback alerts use ordinary Unicode
because that Telegram method has no formatting-entity parameter.

Telegram requires the displayed emoji to match the document's actual `alt`; see
[the official custom-emoji API](https://core.telegram.org/api/custom-emoji).
Presentation-selector variants are rendered to that exact alt before calculating
UTF-16 entity offsets. Unsupported joined/skin-tone emoji components are not
replaced. Caller-supplied entities remain untouched by the generic adapter. The
admin HTML renderer inherits the wrapped event's presentation, prepares icon
substitutions before parsing HTML, then calculates both HTML and custom-emoji
offsets on the resulting text. Generation is capped at 50 custom entities per
message.

If Telegram rejects custom entities or account permission, the reply/edit/caption falls
back to Unicode and custom sends are disabled for that runtime. Other RPC errors
retain existing rate/error handling. Membership in a pack does not grant account
permission.

`logs/telegram.log` records pack load/failure, catalog counts, cooldowns and
permission fallback using safe names/counts. Tests simulate metadata/responses;
they verify registration, semantic selection, concurrency, cancellation, exact-alt
offsets, progress edits and fallback, not live rendering of every pack.
Restart the bot after upgrading to load the catalog.

## Site-specific file captions

`bot/texts/downloads.py` owns the eight presentation profiles, including each site's
icon, download tagline, creator label and quality-menu prompt. Provider handlers
retain their routing and extraction ownership. Instagram does not repeat the
source post's advertising/caption text; it shows the page and selected quality.
Other sites retain a labeled title and artist/channel/publisher. File captions
include the authenticated bot's actual username, not a hardcoded promotion handle.

The composition root injects the same caption builder for cached resends and uses
the same presentation instance for menus, progress and delivered files. Business
services do not import the Telegram/presentation layer. Caption fields are bounded
by UTF-16 units, whitespace is normalized, and text is sent with parsing disabled
so titles remain literal. Existing file references get the current caption on the
next resend without downloading the file again. Previously published messages are
not edited retroactively.

October 15 workstation tests simulate both fresh publication and cached resend for
all six sites, checking matching captions/custom entities, correct UTF-16 offsets,
caption size bounds and permission rejection with one Unicode retry. They do not
claim live Telegram artwork rendering or account permission.

Validation for this caption batch: **898 passed, 18 skipped** across the complete
workstation suite; Ruff lint/format and whitespace checks passed. Skipped cases
require external services or dedicated opt-in configuration.

## October 17 coverage verification

Spotify's green icon and numerous admin/help/group symbols were missing from the
body catalog allowlist even when matching pack metadata had loaded. The expanded
semantic catalog covers all static interface symbols, and admin replies now inherit
presentation when it is carried by their event wrapper.

On the workstation, a read-only authenticated Telegram metadata audit loaded all
14 packs (the thirteen operator packs plus IconsEmoji). No chat messages were sent.
Replaying those actual public document IDs locally verified 118 static UI symbols,
143 message samples across all eight sites and the home menu, and 118 button
samples. Spotify's green icon resolved from EmojiStatus. This verifies valid
metadata and exact-alt/UTF-16 construction, not live rendering or production
account permission. Regression tests also exercise sparse packs, HTML formatting,
single-icon navigation, native-alt precedence and Unicode permission fallback.

Workstation validation: the full suite passed **2264 tests**, with 282 skipped
because external services or opt-in configuration were absent. After adding one
additional button coverage test, the affected suite passed **125 tests**, with
4 SQL-dependent cases skipped. Ruff lint/format and whitespace checks passed.
