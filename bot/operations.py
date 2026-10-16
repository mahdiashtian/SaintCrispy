"""Telegram adapters and owned operational worker lifecycle."""

import asyncio
import logging
from datetime import UTC, datetime

from telethon import errors, functions, types

from bot.checks.group import saved_input_peer
from core.diagnostics import error_fields
from schemas.admin import PermanentEffect, RetryEffect
from schemas.group import GroupChange, Standing

log = logging.getLogger("telegram")


class EffectDelivery:
    def __init__(self, client, tracker, groups, *, presentation=None, rate=None, admin=None):
        self.client, self.tracker, self.groups = client, tracker, groups
        self.presentation, self.rate = presentation, rate
        self.admin = admin

    async def deliver(self, effect):
        try:
            if effect.kind == "health":
                report = await self.tracker.health(effect.target_id)
                if report.standing is Standing.UNKNOWN:
                    if report.reason == "persist_deferred":
                        raise RetryEffect(5, "health_persist_deferred")
                    raise PermanentEffect("health_unknown_" + (report.reason or "unresolved"))
                return
            try:
                peer = await self.client.get_input_entity(effect.target_id)
            except ValueError:
                saved = await self.groups.peer(effect.target_id) if effect.target_id < 0 else None
                if saved is None:
                    raise
                peer = saved_input_peer(saved)
            if effect.kind == "leave":
                if isinstance(peer, types.InputPeerChannel):
                    await self.client(functions.channels.LeaveChannelRequest(peer))
                elif isinstance(peer, types.InputPeerChat):
                    await self.client(
                        functions.messages.DeleteChatUserRequest(
                            peer.chat_id, types.InputUserSelf()
                        )
                    )
                else:
                    raise PermanentEffect("invalid_group_peer")
                await self.mark_left(effect)
                return
            if self.rate:
                delay = await self.rate.reserve()
                if delay > 1:
                    raise RetryEffect(delay, "flood_wait")
                if delay:
                    await asyncio.sleep(delay)
                    await self.rate.wait()
            if effect.kind == "message":
                text = effect.payload["text"]

                async def send(text, **options):
                    return await self.client(
                        functions.messages.SendMessageRequest(
                            peer,
                            text,
                            random_id=effect.random_id,
                            no_webpage=True,
                            entities=options.get("formatting_entities", []),
                        )
                    )

                if self.presentation:
                    await self.presentation._send(send, text, {"parse_mode": None})
                else:
                    await send(text)
            elif effect.kind == "forward":
                source = await self.client.get_input_entity(effect.payload["source_peer"])
                await self.client(
                    functions.messages.ForwardMessagesRequest(
                        from_peer=source,
                        id=[effect.payload["source_id"]],
                        random_id=[effect.random_id],
                        to_peer=peer,
                    )
                )
            else:
                raise PermanentEffect("unknown_effect_kind")
        except (errors.UserNotParticipantError, errors.ChatForbiddenError) as error:
            if effect.kind == "leave":
                await self.mark_left(effect)
                return
            if effect.target_id < 0:
                await self.tracker.send_failure(effect.target_id, error)
            raise PermanentEffect(type(error).__name__) from error
        except errors.FloodWaitError as error:
            if self.rate:
                await self.rate.pause(error.seconds)
            raise RetryEffect(error.seconds, "flood_wait") from error
        except (
            errors.UserIsBlockedError,
            errors.InputUserDeactivatedError,
            errors.ChatWriteForbiddenError,
        ) as error:
            if effect.target_id < 0:
                await self.tracker.send_failure(effect.target_id, error)
            elif self.admin and isinstance(
                error, (errors.UserIsBlockedError, errors.InputUserDeactivatedError)
            ):
                await self.admin.recipient_unavailable(effect.target_id, type(error).__name__)
            raise PermanentEffect(type(error).__name__) from error
        except errors.RPCError as error:
            if effect.target_id < 0:
                await self.tracker.send_failure(effect.target_id, error)
            if isinstance(error, (errors.BadRequestError, errors.ForbiddenError)):
                raise PermanentEffect(type(error).__name__) from error
            raise RetryEffect(5, type(error).__name__) from error

    async def mark_left(self, effect):
        group = await self.groups.get(effect.target_id, fresh=True)
        if group and group.is_active is not False:
            result = await self.groups.record(
                GroupChange(
                    effect.target_id,
                    "removed",
                    f"admin-leave:{effect.id}",
                    datetime.now(UTC),
                    group.chat_kind,
                    active=False,
                    admin=False,
                    can_send=False,
                )
            )
            if result is None:
                raise RetryEffect(5, "leave_persist_deferred")


class OperationalWorkers:
    def __init__(self, operations, memberships, interval=45, groups=None):
        self.operations, self.memberships, self.interval = operations, memberships, interval
        self.tasks = []
        self.groups = groups

    async def _run(self, action, interval, adaptive=False):
        while True:
            busy = False
            try:
                busy = await action()
            except Exception as error:
                log.error({"event": "operational_worker_failed", **error_fields(error)})
            await asyncio.sleep(0.05 if adaptive and busy else interval)

    async def __aenter__(self):
        self.tasks = [
            asyncio.create_task(self._run(self.operations.tick, 1, True)),
            asyncio.create_task(self._run(self.memberships.flush, self.interval)),
        ]
        if self.groups:
            self.tasks.append(asyncio.create_task(self._run(self.groups.flush_deferred, 5)))
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
