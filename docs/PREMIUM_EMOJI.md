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
| Success, failure, warning, waiting and reuse | ✅ ❌ ⚠️ ⏳ ⏱ ♻️ ⚡ | EmojiStatus, ApplicationEmoji, EmoticonEmoji |
| Music, video, photos and provider menus | 🎵 🎧 🎬 ▶️ 📸 📌 | ApplicationEmoji, GameEmoji, NewsEmoji |
| Help, information, replies and identity | 📬 📨 💬 👤 ℹ️ 🌐 📖 | NewsEmoji, ApplicationEmoji, EmoticonEmoji |

Selection compares documents with the same Unicode meaning and is independent
of request completion order. The remaining supplied packs provide matching
alternatives; unrelated finance/seasonal symbols are not forced into status copy.
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

Start/help/provider replies, home-menu edits and progress updates use the adapter.
Inline button labels and callback alerts use ordinary Unicode. Existing callback
identities and transfer behavior are preserved.

Telegram requires the displayed emoji to match the document's actual `alt`; see
[the official custom-emoji API](https://core.telegram.org/api/custom-emoji).
Presentation-selector variants are rendered to that exact alt before calculating
UTF-16 entity offsets. Unsupported joined/skin-tone emoji components are not
replaced. Caller-supplied entities remain untouched. Generation is capped at 50
custom entities per message.

If Telegram rejects custom entities or account permission, the reply/edit falls
back to Unicode and custom sends are disabled for that runtime. Other RPC errors
retain existing rate/error handling. Membership in a pack does not grant account
permission.

`logs/telegram.log` records pack load/failure, catalog counts, cooldowns and
permission fallback using safe names/counts. Tests simulate metadata/responses;
they verify registration, semantic selection, concurrency, cancellation, exact-alt
offsets, progress edits and fallback, not live rendering of every pack.
Restart the bot after upgrading to load the catalog.
