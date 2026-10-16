"""Bounded Telegram checks and message observers for optional admin settings."""

import asyncio
import logging
import time
from collections import OrderedDict

from telethon import Button, errors, functions, types

from core.locks import KeyedLocks
from schemas.media import DownloadError

log = logging.getLogger("activity")


def message_kind(event):
    message = getattr(event, "message", event)
    media = getattr(message, "media", None)
    if media is None or isinstance(media, types.MessageMediaWebPage):
        text = getattr(event, "raw_text", None) or getattr(message, "message", "")
        return "text" if isinstance(text, str) and text.strip() else None
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if not isinstance(media, types.MessageMediaDocument):
        return None
    if getattr(event, "sticker", None):
        return "sticker"
    if getattr(event, "gif", None):
        return "gif"
    if getattr(event, "voice", None):
        return "voice"
    if getattr(event, "audio", None):
        return "music"
    if getattr(event, "video", None) or getattr(event, "video_note", None):
        return "video"
    return None


class ManagementHooks:
    def __init__(self, client, management, rate=None):
        self.client, self.management, self.rate = client, management, rate
        self.admin = management.admin
        self.slots = asyncio.Semaphore(8)
        self.locks = KeyedLocks()
        self.positive = OrderedDict()

    async def observe(self, event):
        if getattr(event, "out", False) or not getattr(event, "is_group", False):
            return
        sender = getattr(event, "sender", None)
        if not isinstance(sender, types.User) or sender.bot or sender.deleted or sender.is_self:
            return
        chat = getattr(event, "chat", None)
        kind = message_kind(event)
        if kind is None:
            return
        await self.management.enqueue_storage(
            event.chat_id,
            event.id,
            kind,
            sender.id,
            getattr(chat, "title", None),
            sender.username,
            bool(getattr(event, "is_private", False)),
        )

    async def check_download(self, user_id):
        await self.admin.check_download(user_id)
        await self.ensure_joined(user_id, "usage")

    async def ensure_joined(self, user_id, point, *, fresh=False):
        try:
            async with asyncio.timeout(12):
                return await self._ensure_joined(user_id, point, fresh=fresh)
        except TimeoutError as error:
            log.warning(
                {
                    "event": "mandatory_join_timeout",
                    "user_id": user_id,
                    "error_type": type(error).__name__,
                }
            )
            return "unknown"

    async def _ensure_joined(self, user_id, point, *, fresh=False):
        if user_id == self.admin.main_admin_id:
            return "not_required"
        config = await self.admin.channels.get()
        channels = config["forced_channels"] if point in config["join_points"] else []
        if not channels:
            return "not_required"
        fingerprint = tuple(sorted(row["id"] for row in channels))
        key = (user_id, fingerprint)
        async with self.locks.hold(key):
            now = time.monotonic()
            if not fresh and self.positive.get(key, 0) > now:
                self.positive.move_to_end(key)
                return "verified"
            if fresh:
                self.positive.pop(key, None)
            missing, unknown = [], False
            async with self.slots:
                async with asyncio.timeout(10):
                    for channel in channels:
                        try:
                            try:
                                peer = await self.client.get_input_entity(channel["id"])
                            except ValueError:
                                peer = await self.client.get_input_entity(channel["link"])
                            user = await self.client.get_input_entity(user_id)
                            if self.rate:
                                await self.rate.wait()
                            result = await self.client(
                                functions.channels.GetParticipantRequest(peer, user)
                            )
                            participant = result.participant
                            if (
                                isinstance(participant, types.ChannelParticipantLeft)
                                or isinstance(participant, types.ChannelParticipantBanned)
                                and (
                                    participant.left
                                    or getattr(participant.banned_rights, "view_messages", False)
                                )
                            ):
                                missing.append(channel)
                            elif not isinstance(
                                participant,
                                (
                                    types.ChannelParticipant,
                                    types.ChannelParticipantSelf,
                                    types.ChannelParticipantAdmin,
                                    types.ChannelParticipantCreator,
                                    types.ChannelParticipantBanned,
                                ),
                            ):
                                unknown = True
                        except errors.UserNotParticipantError:
                            missing.append(channel)
                        except errors.FloodWaitError as error:
                            if self.rate:
                                await self.rate.pause(error.seconds)
                            unknown = True
                            break
                        except (errors.RPCError, ValueError, OSError, TimeoutError):
                            unknown = True
            if missing:
                self.positive.pop(key, None)
                links = "\n".join(row["link"] for row in missing)
                raise DownloadError(
                    "🔐 برای دریافت فایل، ابتدا عضو کانال‌های زیر شو:\n" + links,
                    code="mandatory_join",
                )
            if unknown:
                # Like Couplyo, an unverifiable channel does not lock every
                # downloader during Telegram outages. Never cache partial PASS.
                self.positive.pop(key, None)
                log.warning({"event": "mandatory_join_unverified", "user_id": user_id})
                return "unknown"
            self.positive[key] = now + 60
            while len(self.positive) > 10000:
                self.positive.popitem(last=False)
            return "verified"

    async def check_start(self, event):
        if not getattr(event, "is_private", False):
            return
        try:
            await self.ensure_joined(event.sender_id, "start")
        except (DownloadError, TimeoutError) as error:
            message = (
                str(error)
                if isinstance(error, DownloadError)
                else "⚠️ بررسی عضویت طول کشید؛ کمی بعد دوباره بررسی کن."
            )
            await event.respond(
                message,
                parse_mode=None,
                buttons=[
                    [Button.inline("✅ عضو شدم؛ بررسی مجدد", data=f"mgjoin:{event.sender_id}")]
                ],
            )
            from telethon import events

            raise events.StopPropagation

    async def recheck(self, event):
        try:
            owner = int(event.data.decode("ascii").split(":")[1])
            if owner != event.sender_id:
                raise DownloadError("این دکمه مربوط به تو نیست.")
            result = await self.ensure_joined(owner, "recheck", fresh=True)
            text = {
                "verified": "✅ عضویت تأیید شد؛ لینک رسانه را بفرست.",
                "unknown": "⚠️ بررسی کامل نشد؛ فعلاً می‌توانی لینک را بفرستی و بعداً دوباره بررسی کنی.",
                "not_required": "✅ در این مرحله عضویت اجباری فعال نیست؛ لینک رسانه را بفرست.",
            }[result]
            await event.answer(text, alert=True)
        except (DownloadError, TimeoutError, ValueError, IndexError, UnicodeError) as error:
            await event.answer(
                str(error)[:190]
                if isinstance(error, DownloadError)
                else "بررسی فعلاً ممکن نیست؛ دوباره امتحان کن.",
                alert=True,
            )
