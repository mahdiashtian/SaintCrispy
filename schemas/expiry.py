"""Transport-independent feedback for a delayed deletion attempt."""


class DeletionRetryAfter(Exception):
    def __init__(self, seconds: float):
        self.seconds = max(1, seconds)
        super().__init__("Deletion temporarily rate limited")
