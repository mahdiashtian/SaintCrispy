import secrets
import time
from dataclasses import dataclass

from downloader_bot.schemas.media import DownloadError, Media


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
        self._menus: dict[str, Menu] = {}

    def add(self, owner_id: int, chat_id: int, media: Media) -> str:
        now = time.monotonic()
        self._menus = {key: menu for key, menu in self._menus.items() if menu.expires_at > now}
        if len(self._menus) >= self.limit:
            raise DownloadError("درخواست‌های فعال زیاد است؛ کمی بعد دوباره لینک را بفرست.")
        token = secrets.token_hex(8)
        self._menus[token] = Menu(owner_id, chat_id, media, now + self.ttl)
        return token

    def get(self, token: str, owner_id: int, chat_id: int) -> Menu:
        menu = self._menus.get(token)
        if menu is None or menu.expires_at <= time.monotonic():
            raise DownloadError("این انتخاب منقضی شده است؛ لینک را دوباره بفرست.")
        if menu.owner_id != owner_id or menu.chat_id != chat_id:
            raise DownloadError("این دکمه مربوط به درخواست تو نیست.")
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
            self._menus.pop(next(iter(self._menus)))
        token = await super().create(owner_id, chat_id, media, message_id)
        try:
            await self.repository.save_menu(token, owner_id, chat_id, media, message_id)
            if self.conversations:
                from .states import ConversationState

                await self.conversations.set(
                    owner_id, chat_id, ConversationState.CHOOSING_QUALITY, menu_token=token
                )
        except BaseException:
            self._menus.pop(token, None)
            raise
        return token

    async def fetch(self, token, owner_id, chat_id) -> Menu:
        from downloader_bot.repositories.postgres.workflow import read_media

        try:
            return self.get(token, owner_id, chat_id)
        except DownloadError:
            row = await self.repository.get_menu(token, owner_id, chat_id)
            if row is None:
                raise DownloadError(
                    "⚠️ این دکمه مربوط به درخواست تو نیست یا دیگر موجود نیست."
                ) from None
            return Menu(
                owner_id, chat_id, read_media(row["metadata"]), float("inf"), row["message_id"]
            )
