"""Source group reminders, migration and Telegram document effects."""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types
from test_admin_reference_database import app as app

from bot.operations import EffectDelivery
from repositories.postgres.group import GroupRepository
from repositories.postgres.outbox import OutboxRepository
from schemas.admin import PendingEffect, PermanentEffect
from schemas.group import GroupChange, GroupStanding, Standing
from services.group import GroupService
from services.operations import OperationsService


@pytest.mark.parametrize("mode,expected", [("owner", {7}), ("admins", {8}), ("both", {7, 8})])
async def test_removed_group_reminders_target_distinct_started_contacts_only(app, mode, expected):
    for identity in (7, 8, 9):
        await app.workflow.touch_user(identity, started=identity != 9)
    await app.groups.metadata(-10, 7, [7, 8, 8, 9], 10, None)
    await app.groups.record(
        GroupChange(
            -10, "removed", "removed", app.now + timedelta(seconds=1), "group", active=False
        )
    )
    token = await app.admin.prepare(999, "nudge_owners", {"target": mode, "with_name": False})
    job_id = await app.admin.confirm(999, token)
    service = OperationsService(OutboxRepository(app.pool, app.bot), app.factory, app.admin, None)
    await service.enumerate()
    effects = await app.pool.fetch(
        "SELECT target_id,payload FROM outbox_events WHERE job_id=$1", job_id
    )
    assert {row["target_id"] for row in effects} == expected
    assert all(json.loads(row["payload"])["group_id"] == -10 for row in effects)
    assert await app.pool.fetchval("SELECT total_count FROM admin_jobs WHERE id=$1", job_id) == len(
        expected
    )


@pytest.mark.parametrize("standing", [Standing.UNKNOWN, Standing.ADMIN, Standing.GONE])
async def test_admin_nudge_never_sends_for_unknown_admin_or_gone_standing(standing):
    client = AsyncMock()
    groups = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(migrated_to=None, is_active=True))
    )
    tracker = SimpleNamespace(health=AsyncMock(return_value=GroupStanding(standing)))
    with pytest.raises(PermanentEffect):
        await EffectDelivery(client, tracker, groups).deliver(
            PendingEffect(1, "nudge_admin", -10, {}, 123, None, 1)
        )
    client.assert_not_awaited()


async def test_archive_document_uses_saved_reference_after_source_message_expiry():
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChannel(77, 1234)
    effect = PendingEffect(
        1,
        "archive_document",
        -(10**12 + 77),
        {
            "document": {
                "document_id": 10,
                "access_hash": 20,
                "file_reference": base64.b64encode(b"reference").decode(),
            },
            "caption": "Completed download",
        },
        123456,
        None,
        1,
    )
    await EffectDelivery(client, None, None).deliver(effect)
    request = client.call_args.args[0]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaDocument)
    assert request.random_id == 123456 and request.media.id.file_reference == b"reference"
    assert bytes(request)
    client.get_messages.assert_not_awaited()


@pytest.mark.parametrize("new_known_removed", [False, True])
async def test_migration_preserves_watch_and_does_not_revive_known_removed_target(
    app, new_known_removed
):
    new_id = -(10**12 + 77)
    old_watch = {
        "chat_id": -10,
        "channel": {"id": -(10**12 + 88), "title": "Watch", "link": "https://t.me/watch_channel"},
        "kinds": ["text"],
    }
    await app.admin.channels.update(999, "watches", [old_watch])
    await app.admin.channels.cached()
    await app.groups.record(
        GroupChange(-10, "promoted", "promote", app.now + timedelta(seconds=1), "group", admin=True)
    )
    if new_known_removed:
        await app.groups.record(
            GroupChange(
                new_id,
                "removed",
                "known-removed",
                app.now + timedelta(seconds=2),
                "supergroup",
                active=False,
                admin=False,
            )
        )
    groups = GroupService(
        GroupRepository(app.pool, app.bot),
        app.factory,
        roster_enabled=True,
        management_enabled=True,
    )
    groups.management_changed = app.admin.channels.invalidate
    await groups.migrate(-10, new_id, app.now + timedelta(seconds=3))
    current = await groups.get(new_id)
    assert current.is_active is (not new_known_removed)
    assert current.is_admin is (not new_known_removed)
    assert (await app.admin.channels.cached())["watches"] == [{**old_watch, "chat_id": new_id}]
    assert (await groups.get(-10)).migrated_to == new_id
