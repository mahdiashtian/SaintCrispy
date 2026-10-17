"""Independent rate controls persisted as bot-scoped application settings."""

from dataclasses import asdict, dataclass

NUMERIC_CONTROLS = {
    "user_quota": "user_links_per_minute",
    "group_quota": "group_links_per_minute",
    "user_gap": "user_gap_seconds",
    "group_gap": "group_gap_seconds",
}
TOGGLE_CONTROLS = {
    "user_quota_toggle": "user_quota_enabled",
    "group_quota_toggle": "group_quota_enabled",
    "user_gap_toggle": "user_gap_enabled",
    "group_gap_toggle": "group_gap_enabled",
}


@dataclass(frozen=True)
class DownloadLimits:
    user_links_per_minute: int = 1
    group_links_per_minute: int = 5
    user_quota_enabled: bool = True
    group_quota_enabled: bool = True
    user_gap_seconds: int = 5
    group_gap_seconds: int = 5
    user_gap_enabled: bool = False
    group_gap_enabled: bool = False

    def __post_init__(self):
        for key in NUMERIC_CONTROLS.values():
            value = getattr(self, key)
            maximum = 3600 if key.endswith("_seconds") else 1000
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Invalid request limit: {key}")
        for key in TOGGLE_CONTROLS.values():
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"Invalid request toggle: {key}")

    @classmethod
    def from_settings(cls, values, *, group_default=5):
        defaults = cls(group_links_per_minute=group_default).settings()
        return cls(**{key: values.get(key, default) for key, default in defaults.items()})

    def settings(self):
        return asdict(self)

    def public(self):
        return {
            **self.settings(),
            "user_limit": self.user_links_per_minute,
            "group_limit": self.group_links_per_minute,
        }
