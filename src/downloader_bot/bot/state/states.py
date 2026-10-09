"""Stable names persisted across deployments; no onboarding gate."""

from enum import StrEnum


class ConversationState(StrEnum):
    MAIN = "MAIN"
    INSPECTING = "INSPECTING"
    CHOOSING_QUALITY = "CHOOSING_QUALITY"
    TRANSFERRING = "TRANSFERRING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
