"""Register a user on any request; /start is only a presentation action."""


class UserService:
    def __init__(self, repository):
        self.repository = repository

    async def touch_user(self, user_id: int, *, started: bool = False) -> None:
        await self.repository.touch_user(user_id, started=started)
