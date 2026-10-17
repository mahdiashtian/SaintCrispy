"""Telegram adapters and owned operational worker lifecycle."""

import asyncio
import base64
import io
import logging
from datetime import UTC, datetime

from telethon import errors, functions, types

from bot.checks.group import saved_input_peer
from core.diagnostics import error_fields
from schemas.admin import PermanentEffect, RetryEffect
from schemas.group import GroupChange, Standing

log = logging.getLogger("telegram")


class EffectDelivery:
    def __init__(
        self,
        client,
        tracker,
        groups,
        *,
        presentation=None,
        rate=None,
        admin=None,
        roster=None,
        memberships=None,
        management=None,
    ):
        self.client, self.tracker, self.groups = client, tracker, groups
        self.presentation, self.rate = presentation, rate
        self.admin = admin
        self.roster, self.memberships, self.management = roster, memberships, management

    async def deliver(self, effect):
        try:
            if effect.kind == "crawl":
                if self.roster is None:
                    raise PermanentEffect("crawl_disabled")
                await self.roster.enqueue(effect.target_id)
                return
            if effect.kind in {"refresh_users", "purge_users"}:
                if self.rate:
                    delay = await self.rate.reserve()
                    if delay > 0:
                        raise RetryEffect(delay, "flood_wait")
                user = await self.client.get_entity(effect.target_id)
                if not isinstance(user, types.User) or user.id != effect.target_id:
                    raise PermanentEffect("not_a_human")
                if effect.kind == "purge_users":
                    await self.memberships.purge_invalid(
                        user.id,
                        deleted=bool(user.deleted),
                        bot=bool(user.bot),
                        root_id=self.admin.main_admin_id,
                    )
                    if self.admin.cache:
                        await self.admin.cache.invalidate("user", user.id)
                    return
                await self.memberships.refresh_profile(
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    deleted=bool(user.deleted),
                    bot=bool(user.bot),
                )
                if self.admin and self.admin.cache:
                    await self.admin.cache.invalidate("user", user.id)
                return
            if effect.kind in {"nudge_admin", "nudge_owners"}:
                owners = effect.kind == "nudge_owners"
                group_id = effect.payload.get("group_id") if owners else effect.target_id
                group = await self.groups.get(group_id, fresh=True)
                if (
                    not group
                    or group.migrated_to
                    or (group.is_active is not False if owners else group.is_active is not True)
                ):
                    raise PermanentEffect("nudge_not_eligible")
                if owners and not await self.admin.can_contact(effect.target_id):
                    raise PermanentEffect("recipient_not_started")
                if not owners:
                    standing = await self.tracker.health(group_id)
                    if standing.standing is not Standing.MEMBER:
                        raise PermanentEffect("nudge_standing_unknown_or_admin")
                peer = await self.resolve_peer(effect.target_id)
                if self.rate:
                    delay = await self.rate.reserve()
                    if delay > 0:
                        raise RetryEffect(delay, "flood_wait")
                from types import SimpleNamespace

                from bot.handlers.admin_render import source_reply
                from bot.texts.promos import need_admin, removed_nudge

                markup = (
                    removed_nudge(
                        group,
                        effect.target_id,
                        effect.random_id,
                        with_name=effect.payload.get("with_name", True),
                    )
                    if owners
                    else need_admin(group, effect.random_id)
                )

                async def send_nudge(text, **options):
                    return await self.client(
                        functions.messages.SendMessageRequest(
                            peer,
                            text,
                            random_id=effect.random_id,
                            no_webpage=True,
                            entities=options.get("formatting_entities", []),
                        )
                    )

                await source_reply(
                    SimpleNamespace(respond=send_nudge), markup, presentation=self.presentation
                )
                return
            if effect.kind == "health":
                report = await self.tracker.health(effect.target_id)
                if report.standing is Standing.UNKNOWN:
                    if report.reason == "persist_deferred":
                        raise RetryEffect(5, "health_persist_deferred")
                    raise PermanentEffect("health_unknown_" + (report.reason or "unresolved"))
                return
            peer = await self.resolve_peer(effect.target_id)
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
            if effect.kind == "report":
                if self.management is None:
                    raise PermanentEffect("reports_unavailable")
                owner = effect.payload.get("requested_by")
                if owner and owner != self.admin.main_admin_id:
                    try:
                        permissions = await self.client.get_permissions(effect.target_id, owner)
                        if not (permissions.is_admin or permissions.is_creator):
                            raise PermanentEffect("report_requester_no_longer_admin")
                    except (errors.RPCError, ValueError) as error:
                        raise PermanentEffect("report_requester_unverifiable") from error
                data, caption = await self.management.report(
                    effect.target_id,
                    period=effect.payload.get("report_period", "week"),
                    start=effect.payload.get("report_start"),
                    end=effect.payload.get("report_end"),
                )
                data_file = io.BytesIO(data)
                data_file.name = "weekly-report.png"
                uploaded = await self.client.upload_file(data_file)
                await self.client(
                    functions.messages.SendMediaRequest(
                        peer=peer,
                        media=types.InputMediaUploadedPhoto(uploaded),
                        message=caption,
                        random_id=effect.random_id,
                        reply_to=types.InputReplyToMessage(effect.payload["reply_to"])
                        if effect.payload.get("reply_to")
                        else None,
                    )
                )
            elif effect.kind == "archive_document":
                data = effect.payload["document"]
                document = types.InputDocument(
                    data["document_id"],
                    data["access_hash"],
                    base64.b64decode(data["file_reference"], validate=True),
                )
                await self.client(
                    functions.messages.SendMediaRequest(
                        peer=peer,
                        media=types.InputMediaDocument(document),
                        message=effect.payload.get("caption", ""),
                        random_id=effect.random_id,
                    )
                )
            elif effect.kind == "message" and not effect.payload.get("copy"):
                markup = None
                if effect.payload.get("management", {}).get("feature") == "promo":
                    from bot.texts.promos import promo

                    markup = promo(
                        await self.groups.get(effect.target_id, fresh=True), effect.random_id
                    )
                elif effect.payload.get("group_event"):
                    from bot.texts.group_lifecycle import admin_event

                    markup = admin_event(effect.payload["group_event"])
                elif effect.payload.get("group_announcement"):
                    from dataclasses import asdict

                    from bot.texts.group_lifecycle import announcement

                    group = await self.groups.get(effect.target_id, fresh=True)
                    markup = announcement(
                        effect.payload["group_announcement"], asdict(group), seed=effect.random_id
                    )
                text = markup if markup is not None else effect.payload["text"]

                async def send(text, **options):
                    return await self.client(
                        functions.messages.SendMessageRequest(
                            peer,
                            text,
                            random_id=effect.random_id,
                            no_webpage=True,
                            entities=options.get("formatting_entities", []),
                            reply_to=types.InputReplyToMessage(effect.payload["reply_to"])
                            if effect.payload.get("reply_to")
                            else None,
                        )
                    )

                if markup is not None:
                    from types import SimpleNamespace

                    from bot.handlers.admin_render import source_reply

                    await source_reply(
                        SimpleNamespace(respond=send), markup, presentation=self.presentation
                    )
                elif self.presentation:
                    await self.presentation._send(send, text, {"parse_mode": None})
                else:
                    await send(text)
            elif effect.kind == "forward" or effect.payload.get("copy"):
                source = await self.client.get_input_entity(effect.payload["source_peer"])
                result = await self.client(
                    functions.messages.ForwardMessagesRequest(
                        from_peer=source,
                        id=[effect.payload["source_id"]],
                        random_id=[effect.random_id],
                        to_peer=peer,
                        drop_author=bool(effect.payload.get("copy")),
                    )
                )
                if self.management and effect.payload.get("footer"):
                    outgoing = next(
                        (
                            item.id
                            for item in getattr(result, "updates", [])
                            if isinstance(item, types.UpdateMessageID)
                            and item.random_id == effect.random_id
                        ),
                        None,
                    )
                    if outgoing:
                        await self.management.notice(
                            f"forward-footer:{effect.id}",
                            effect.target_id,
                            effect.payload["footer"],
                            reply_to=outgoing,
                        )
            else:
                raise PermanentEffect("unknown_effect_kind")
        except (errors.UserNotParticipantError, errors.ChatForbiddenError) as error:
            self.refuse_storage_route(effect, error)
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
            self.refuse_storage_route(effect, error)
            if effect.target_id < 0:
                await self.tracker.send_failure(effect.target_id, error)
            elif self.admin and isinstance(
                error, (errors.UserIsBlockedError, errors.InputUserDeactivatedError)
            ):
                await self.admin.recipient_unavailable(effect.target_id, type(error).__name__)
            raise PermanentEffect(type(error).__name__) from error
        except errors.RPCError as error:
            self.refuse_storage_route(effect, error)
            if effect.target_id < 0:
                await self.tracker.send_failure(effect.target_id, error)
            if isinstance(error, (errors.BadRequestError, errors.ForbiddenError)):
                raise PermanentEffect(type(error).__name__) from error
            raise RetryEffect(5, type(error).__name__) from error

    def refuse_storage_route(self, effect, error):
        data = effect.payload.get("management", {})
        if (
            self.management
            and data.get("feature") in {"storage", "watch"}
            and type(error).__name__
            in {
                "ChatForwardsRestrictedError",
                "ChatWriteForbiddenError",
                "ChatAdminRequiredError",
                "UserNotParticipantError",
                "ChatForbiddenError",
                "ChannelPrivateError",
                "ChannelInvalidError",
                "PeerIdInvalidError",
            }
        ):
            self.management.refuse_route(data.get("source_peer"), effect.target_id)

    async def resolve_peer(self, target_id):
        try:
            return await self.client.get_input_entity(target_id)
        except ValueError:
            saved = (
                await self.groups.peer(target_id)
                if type(target_id) is int and target_id < 0
                else None
            )
            if saved is None:
                raise
            return saved_input_peer(saved)

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
    def __init__(self, operations, memberships, interval=45, groups=None, management=None):
        self.operations, self.memberships, self.interval = operations, memberships, interval
        self.tasks = []
        self.groups = groups
        self.management = management

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
        if self.management:
            self.tasks.append(asyncio.create_task(self._run(self.management.tick, 30)))
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
