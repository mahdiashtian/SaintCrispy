import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from core.request_context import current_inspection_work, request_id
from schemas.media import DownloadError, Media


@dataclass(frozen=True)
class Menu:
    owner_id: int
    chat_id: int
    media: Media
    expires_at: float
    message_id: int | None = None


class MenuStore:
    def __init__(self, ttl: int = 900, limit: int = 2048):
        self.ttl = ttl
        self.limit = limit
        self._menus: OrderedDict[str, Menu] = OrderedDict()

    def add(self, owner_id: int, chat_id: int, media: Media) -> str:
        now = time.monotonic()
        # Fixed TTL keeps insertion and expiry order aligned; prune only expired heads.
        while self._menus and next(iter(self._menus.values())).expires_at <= now:
            self._menus.popitem(last=False)
        if len(self._menus) >= self.limit:
            raise DownloadError("درخواست‌های فعال زیاد است؛ کمی بعد دوباره لینک را بفرست.")
        token = secrets.token_hex(8)
        self._menus[token] = Menu(owner_id, chat_id, media, now + self.ttl)
        return token

    def get(self, token: str, owner_id: int, chat_id: int) -> Menu:
        menu = self._menus.get(token)
        if menu is None:
            raise DownloadError("این انتخاب منقضی شده است؛ لینک را دوباره بفرست.")
        if menu.owner_id != owner_id or menu.chat_id != chat_id:
            raise DownloadError(
                "⛔️ این دکمه فقط برای فرستندهٔ لینک است؛ لینک را خودت بفرست.",
                code="menu_not_owned",
            )
        if menu.expires_at <= time.monotonic():
            raise DownloadError("این انتخاب منقضی شده است؛ لینک را دوباره بفرست.")
        return menu

    async def create(self, owner_id, chat_id, media, message_id=None) -> str:
        token = self.add(owner_id, chat_id, media)
        menu = self._menus[token]
        self._menus[token] = Menu(owner_id, chat_id, media, menu.expires_at, message_id)
        return token

    async def fetch(self, token, owner_id, chat_id) -> Menu:
        return self.get(token, owner_id, chat_id)


class PersistentMenuStore(MenuStore):
    """Keep a bounded hot cache; durable menus have no conversation expiry."""

    def __init__(self, repository, *, limit=2048, conversations=None):
        super().__init__(limit=limit)
        self.repository = repository
        self.conversations = conversations

    async def create(self, owner_id, chat_id, media, message_id=None) -> str:
        if len(self._menus) >= self.limit:
            self._menus.popitem(last=False)
        token = await super().create(owner_id, chat_id, media, message_id)
        try:
            combined = getattr(self.repository, "save_menu_and_choose", None)
            if self.conversations and combined is not None:
                work = current_inspection_work.get()
                await combined(
                    token,
                    owner_id,
                    chat_id,
                    media,
                    message_id,
                    link_identity=work.link_identity if work else None,
                    request_id=request_id.get() if work else None,
                )
                if work is not None:
                    work.completed = True
                logging.getLogger("activity").info(
                    {"event": "conversation_changed", "state": "CHOOSING_QUALITY"}
                )
            else:
                await self.repository.save_menu(token, owner_id, chat_id, media, message_id)
            if self.conversations and combined is None:
                from .states import ConversationState

                await self.conversations.set(
                    owner_id, chat_id, ConversationState.CHOOSING_QUALITY, menu_token=token
                )
        except BaseException:
            self._menus.pop(token, None)
            raise
        return token

    async def fetch(self, token, owner_id, chat_id) -> Menu:
        from repositories.postgres.workflow import read_media

        try:
            return self.get(token, owner_id, chat_id)
        except DownloadError as error:
            if error.code == "menu_not_owned":
                raise
            row = await self.repository.get_menu(token, owner_id, chat_id)
            if row is None:
                raise DownloadError(
                    "⚠️ این دکمه مربوط به درخواست تو نیست یا دیگر موجود نیست."
                ) from None
            return Menu(
                owner_id, chat_id, read_media(row["metadata"]), float("inf"), row["message_id"]
            )
