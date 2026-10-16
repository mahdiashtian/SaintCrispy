"""Bounded read-only Telegram checks; never receive updates or prompt for login."""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def probe(values, chat_id=None, *, public_group=None, factory=TelegramClient):
    result = {
        "scope": "read_only_bot_session",
        "authenticated": False,
        "group_checked": False,
        "lifecycle_test": "not_run_requires_authorized_human_and_controlled_group",
        "admin_panel_test": "not_run_requires_human_private_interaction",
    }
    if not all(values.get(key) for key in ("API_ID", "API_HASH", "BOT_TOKEN")):
        return {**result, "error_type": "MissingCredentials"}
    client = None
    stage = "construct"
    try:
        client = factory(
            MemorySession(),
            int(values["API_ID"]),
            values["API_HASH"],
            receive_updates=False,
            catch_up=False,
            request_retries=2,
            connection_retries=1,
            auto_reconnect=False,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            timeout=5,
        )
        async with asyncio.timeout(30):
            stage = "connect"
            await client.connect()
            stage = "sign_in"
            await client.sign_in(bot_token=values["BOT_TOKEN"])
            stage = "get_me"
            me = await client.get_me()
            if not me or not me.bot or str(me.id) != values["BOT_TOKEN"].split(":", 1)[0]:
                raise ValueError("Wrong bot identity")
            result["authenticated"] = True
            if chat_id is not None or public_group is not None:
                if chat_id is not None and (type(chat_id) is not int or chat_id >= 0):
                    raise ValueError("A controlled test group must have a negative ID")
                stage = "resolve_test_group"
                entity = await client.get_entity(public_group or chat_id)
                if chat_id is not None:
                    from telethon import utils

                    if utils.get_peer_id(entity) != chat_id:
                        raise ValueError("Test group identity mismatch")
                if isinstance(entity, types.Channel) and entity.megagroup and not entity.broadcast:
                    peer = await client.get_input_entity(entity)
                    stage = "self_membership"
                    own = await client(
                        functions.channels.GetParticipantRequest(peer, types.InputPeerSelf())
                    )
                    result["administrator"] = isinstance(
                        own.participant,
                        (types.ChannelParticipantAdmin, types.ChannelParticipantCreator),
                    )
                    stage = "roster_page"
                    page = await client(
                        functions.channels.GetParticipantsRequest(
                            peer, types.ChannelParticipantsRecent(), 0, 200, 0
                        )
                    )
                    result.update(group_checked=True, roster_page_count=len(page.participants))
                elif isinstance(entity, types.Chat):
                    stage = "basic_roster"
                    full = await client(functions.messages.GetFullChatRequest(entity.id))
                    participants = full.full_chat.participants
                    if (
                        isinstance(participants, types.ChatParticipants)
                        and participants.chat_id == entity.id
                    ):
                        result.update(
                            group_checked=True, roster_page_count=len(participants.participants)
                        )
                else:
                    raise ValueError("Peer is not a verified group")
    except Exception as error:
        result.update(stage=stage, error_type=type(error).__name__)
        if hasattr(error, "seconds"):
            result["wait_seconds"] = error.seconds
    finally:
        if client:
            try:
                async with asyncio.timeout(5):
                    await client.disconnect()
            except (OSError, TimeoutError):
                pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat-id", type=int, help="An explicitly controlled existing test group only"
    )
    parser.add_argument(
        "--public-group", help="Public handle of the controlled test group for a cold session"
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=True, interpolate=False, encoding="utf-8-sig")
    logging.disable(logging.CRITICAL)
    result = asyncio.run(probe(os.environ, args.chat_id, public_group=args.public_group))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
