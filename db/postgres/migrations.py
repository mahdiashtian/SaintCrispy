"""Ordered additive migrations; preserve the existing version ledger."""

from .models import GROUP_SCHEMA, SCHEMA, WORKFLOW_SCHEMA

MIGRATIONS = (
    (1, "existing_media_schema", SCHEMA),
    (2, "durable_bot_workflow", WORKFLOW_SCHEMA),
    (
        3,
        "streamable_video_references",
        "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS video_streaming BOOLEAN;",
    ),
    (
        4,
        "verified_complete_media",
        "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS verified_complete "
        "BOOLEAN NOT NULL DEFAULT FALSE;",
    ),
    (
        5,
        "video_preview_references",
        "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS video_preview BOOLEAN;",
    ),
    (
        6,
        "indexed_video_references",
        "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS video_faststart BOOLEAN;",
    ),
    (7, "group_lifecycle_and_admission", GROUP_SCHEMA),
)
