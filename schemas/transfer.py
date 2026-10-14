"""Framework-independent transfer counters."""

import uuid
from dataclasses import dataclass, field


@dataclass
class TransferState:
    """Small in-memory counters; network requests belong to the separate reporter."""

    phase: str = "starting"
    method: str = ""
    downloaded: int = 0
    uploaded: int = 0
    total: int | None = None
    estimated_total: int | None = None
    duration: int = 0
    seconds: float = 0
    download_done: bool = False
    upload_done: bool = False
    error: str = ""
    transfer_id: str = field(default_factory=lambda: uuid.uuid4().hex)
