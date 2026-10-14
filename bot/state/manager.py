"""Telegram FSM facade over the domain conversation service."""

from services.conversation import ConversationService as ConversationManager

__all__ = ["ConversationManager"]
