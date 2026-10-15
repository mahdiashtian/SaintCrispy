"""Provider availability policy, independent of chat roles and cached media."""

from dataclasses import dataclass

from schemas.media import DownloadError


@dataclass(frozen=True)
class ProviderAccess:
    main_admin_id: int | None = None

    def __post_init__(self):
        if self.main_admin_id is not None and (
            type(self.main_admin_id) is not int or self.main_admin_id <= 0
        ):
            raise ValueError("Main admin ID must be a positive integer")

    def allows(self, site: str | None, user_id: int | None) -> bool:
        return site != "youtube" or (
            type(user_id) is int
            and self.main_admin_id is not None
            and user_id == self.main_admin_id
        )

    def require(self, site: str | None, user_id: int | None) -> None:
        if not self.allows(site, user_id):
            raise DownloadError(
                "دانلود از یوتیوب فعلاً برای کاربران غیرفعال است.",
                code="provider_access_denied",
            )
