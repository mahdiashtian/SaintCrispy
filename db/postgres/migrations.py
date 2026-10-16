"""Ordered additive migrations; preserve the existing version ledger."""

from .models import CONTROL_TABLES, FILE_EXPIRY, GROUP_SCHEMA, SCHEMA, WORKFLOW_SCHEMA
from .models.administration import UPGRADE
from .models.group import INVARIANTS_SCHEMA

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
    (
        8,
        "downloaded_file_message_expiry",
        FILE_EXPIRY.schema + "\nCREATE INDEX IF NOT EXISTS media_files_origin ON media_files "
        "(telegram_account_id,origin_peer,message_id);",
    ),
    (9, "group_lifecycle_invariants", INVARIANTS_SCHEMA),
    (
        10,
        "administration_membership_and_durable_operations",
        UPGRADE + "\n".join(t.schema for t in CONTROL_TABLES),
    ),
)
