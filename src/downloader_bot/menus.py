import secrets
import time
from dataclasses import dataclass

from downloader_bot.models import DownloadError, Media


@dataclass(frozen=True)
class Menu:
    owner_id: int
    chat_id: int
    media: Media
    expires_at: float


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
