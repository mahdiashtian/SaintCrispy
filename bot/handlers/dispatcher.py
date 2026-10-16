import logging
from contextlib import nullcontext
from functools import partial

from telethon import errors, events

from bot.checks.identity import has_user_context
from bot.telegram_errors import telegram_failure
from core.diagnostics import error_fields
from downloader_bot.downloaders.instagram.handler import register_handler as register_instagram
from downloader_bot.downloaders.pinterest.handler import register_handler as register_pinterest
from downloader_bot.downloaders.soundcloud.handler import register_handler as register_soundcloud
from downloader_bot.downloaders.tiktok.handler import register_handler as register_tiktok
from downloader_bot.downloaders.xnxx.handler import register_handler as register_xnxx
from downloader_bot.downloaders.xvideos.handler import register_handler as register_xvideos
from downloader_bot.downloaders.youtube.handler import register_handler as register_youtube
from services.access import ProviderAccess

from . import admin as admin_handlers
from .groups import GROUP_UPDATES, GroupTracker
from .home import handle_home
from .limited import LimitedHandlers
from .membership import MembershipTracker
from .quality import handle_quality
from .start import handle_start
from .stop import handle_stop

PROVIDER_HANDLERS = (
    register_soundcloud,
    register_xnxx,
    register_xvideos,
    register_youtube,
    register_instagram,
    register_pinterest,
    register_tiktok,
)

log = logging.getLogger("activity")


class Dispatcher:
    """Isolate every update and validate user context before any side effect."""

    def __init__(self, client, groups=None, gate=None):
        self.client = client
        self.groups = groups
        self.gate = gate

    def add_event_handler(self, callback, builder):
        self._register(callback, builder, require_user=True)

    def add_system_handler(self, callback, builder):
        self._register(callback, builder, require_user=False)

    def _register(self, callback, builder, *, require_user):
        async def dispatch(event):
            if require_user and not has_user_context(event):
                log.warning({"event": "update_context_rejected"})
                return
            try:
                async with self.gate.operation() if self.gate else nullcontext():
                    if (
                        require_user
                        and getattr(event, "is_channel", False)
                        and getattr(event, "is_group", None) is None
                    ):
                        # A cold callback cache may not know whether this channel is
                        # a supergroup. Trust the persisted group kind, never guess.
                        known = await self.groups.get(event.chat_id) if self.groups else None
                        if known is None or known.chat_kind != "supergroup":
                            log.warning({"event": "update_group_context_unknown"})
                            return
                    await callback(event)
            except events.StopPropagation:
                raise
            except errors.RPCError as error:
                log.warning(
                    {
                        "event": "dispatch_telegram_failed",
                        **telegram_failure(error),
                        **error_fields(error),
                    }
                )
            except Exception as error:
                log.error({"event": "dispatch_failed", **error_fields(error)})

        # Preserve callback identity for routing introspection without sharing mutable state.
        dispatch.func = getattr(callback, "func", callback)
        self.client.add_event_handler(dispatch, builder)


def register_handlers(
    client,
    service,
    menus,
    jobs=None,
    limits=None,
    *,
    conversations=None,
    users=None,
    bot_username=None,
    presentation=None,
    max_link_handlers=1000,
    groups=None,
    bot_id=None,
    access=None,
    admin=None,
    memberships=None,
    tracker=None,
    gate=None,
    policy=None,
) -> None:
    access = access or ProviderAccess()
    tracker = tracker or (GroupTracker(client, groups, bot_id) if groups is not None else None)
    membership_tracker = (
        MembershipTracker(client, groups, tracker, memberships, bot_id)
        if memberships and tracker
        else None
    )
    client = Dispatcher(client, groups, gate)
    if tracker:
        client.add_system_handler(tracker.handle_update, events.Raw(GROUP_UPDATES))
        client.add_system_handler(
            tracker.observe,
            events.NewMessage(incoming=True, func=lambda event: event.is_group),
        )
    if membership_tracker:
        client.add_system_handler(membership_tracker.handle_update, events.Raw(GROUP_UPDATES))
        client.add_system_handler(
            membership_tracker.observe,
            events.NewMessage(incoming=True, func=lambda event: event.is_group),
        )
        client.add_system_handler(
            membership_tracker.observe_private,
            events.NewMessage(
                incoming=True, func=lambda event: getattr(event, "is_private", False)
            ),
        )
    if admin is not None:
        client.add_event_handler(
            partial(
                admin_handlers.open_panel,
                admin=admin,
                presentation=presentation,
                conversations=conversations,
            ),
            events.NewMessage(incoming=True, pattern=r"^/admin(?:@\w+)?$"),
        )
        client.add_event_handler(
            partial(
                admin_handlers.callback,
                admin=admin,
                presentation=presentation,
                conversations=conversations,
            ),
            events.CallbackQuery(pattern=rb"^adm:"),
        )
        client.add_event_handler(
            partial(admin_handlers.input_reply, admin=admin, presentation=presentation),
            events.NewMessage(
                incoming=True,
                func=lambda event: getattr(event, "is_private", False) and event.is_reply,
            ),
        )
    if admin is not None and conversations is not None:
        from .admin_reference import handle_message

        client.add_event_handler(
            partial(
                handle_message, admin=admin, conversations=conversations, presentation=presentation
            ),
            events.NewMessage(
                incoming=True, func=lambda event: getattr(event, "is_private", False)
            ),
        )
    if policy is not None:
        client.add_event_handler(
            policy.check_start,
            events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?(?:\s.*)?$"),
        )
        client.add_event_handler(
            policy.recheck, events.CallbackQuery(pattern=rb"^mgjoin:([0-9]+)$")
        )
    client.add_event_handler(
        partial(
            handle_start,
            bot_username=bot_username,
            interval=getattr(limits, "interval", 60),
            conversations=conversations,
            users=users,
            presentation=presentation,
            group_limit=getattr(groups, "links_per_minute", 5),
            access=access,
            management=getattr(policy, "management", None),
        ),
        events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?(?:\s.*)?$"),
    )
    provider_client = LimitedHandlers(
        client,
        limits,
        conversations,
        users,
        presentation,
        max_active=max_link_handlers,
        access=access,
        policy=policy or admin,
    )
    client.add_event_handler(
        partial(
            handle_home,
            jobs=jobs,
            bot_username=bot_username,
            interval=getattr(limits, "interval", 60),
            presentation=presentation,
            access=access,
            management=getattr(policy, "management", None),
        ),
        events.CallbackQuery(pattern=rb"^home:([0-9]+):(help|sites|active|main)$"),
    )
    for register in PROVIDER_HANDLERS:
        registration_client = (
            provider_client if limits is not None or register is register_youtube else client
        )
        register(registration_client, service, menus)
    client.add_event_handler(
        partial(
            handle_quality,
            service=service,
            menus=menus,
            jobs=jobs,
            limits=limits,
            conversations=conversations,
            presentation=presentation,
            access=access,
            policy=policy or admin,
        ),
        events.CallbackQuery(pattern=rb"^(?:sc|xnxx|xv|yt|ig|pin|tt):([0-9a-f]{16}):([0-9]+)$"),
    )
    client.add_event_handler(
        partial(handle_stop, jobs=jobs),
        events.CallbackQuery(pattern=rb"^stop:([0-9a-f]{16})$"),
    )
    if policy is not None:
        client.add_system_handler(policy.observe, events.NewMessage(incoming=True))
