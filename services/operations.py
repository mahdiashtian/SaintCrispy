"""Bounded resumable job enumeration and leased effect delivery."""

import asyncio
import logging
import uuid

from core.diagnostics import error_fields
from schemas.admin import PermanentEffect, RetryEffect
from schemas.media import DownloadError

log = logging.getLogger("activity")


class OperationsService:
    def __init__(self, repository, uow_factory, admin, delivery, *, management=None):
        self.repository, self.uow_factory, self.admin, self.delivery = (
            repository,
            uow_factory,
            admin,
            delivery,
        )
        self.owner = uuid.uuid4().hex
        self.management = management

    async def enumerate(self):
        job = await self.repository.claim_job(self.owner)
        if not job:
            return
        try:
            await self.admin.authorize(job["requested_by"], root=job["kind"] != "health")
            self.admin.validate_job(job["kind"], job["payload"])
            async with self.uow_factory() as uow:
                if not await uow.outbox.owned_job(job["id"], self.owner):
                    return
                single = (
                    job["kind"] in {"health", "leave", "crawl"}
                    and job["payload"].get("chat_id") is not None
                )
                targets = [job["payload"]["chat_id"]] if single else await uow.outbox.targets(job)
                if (
                    job["kind"] in {"broadcast", "forward"}
                    and job["payload"].get("target_id") is not None
                ):
                    single = True
                    targets = [job["payload"]["target_id"]]
                if single and job["cursor"] is not None:
                    targets = []
                excluded = set(job["payload"].get("exclude", []))
                planned = 0
                skipped = 0
                for target in targets:
                    if target in excluded:
                        planned += 1
                        skipped += 1
                        continue
                    if job["kind"] == "nudge_owners":
                        group = await uow.groups.get(target)
                        contacts = set()
                        mode = job["payload"].get("target", "owner")
                        if group and mode in {"owner", "both"} and group.owner_id:
                            contacts.add(group.owner_id)
                        if group and mode in {"admins", "both"}:
                            contacts.update(
                                identity
                                for identity in group.admin_ids
                                if identity != group.owner_id
                            )
                        recipients = await uow.outbox.started_recipients(contacts)
                        if not recipients:
                            planned += 1
                            skipped += 1
                        for recipient in recipients:
                            planned += 1
                            await uow.outbox.enqueue(
                                f"job:{job['id']}:{target}:{recipient}",
                                "nudge_owners",
                                recipient,
                                {**job["payload"], "group_id": target},
                                job["id"],
                            )
                        continue
                    planned += 1
                    kind = {
                        "broadcast": "message",
                        "forward": "forward",
                        "health": "health",
                        "leave": "leave",
                        "crawl": "crawl",
                        "nudge_admin": "nudge_admin",
                        "nudge_owners": "nudge_owners",
                        "refresh_users": "refresh_users",
                        "report": "report",
                    }[job["kind"]]
                    await uow.outbox.enqueue(
                        f"job:{job['id']}:{target}", kind, target, job["payload"], job["id"]
                    )
                if not await uow.outbox.checkpoint(
                    job,
                    self.owner,
                    targets[-1] if targets else job["cursor"],
                    planned,
                    done=not targets or single,
                ):
                    raise RuntimeError("Lost administrator job lease")
                if skipped:
                    await uow.admin.skipped(job["id"], skipped)
        except Exception as error:
            permanent = isinstance(error, DownloadError) or job["attempt_count"] >= 6
            await self.repository.fail_job(
                job["id"],
                self.owner,
                type(error).__name__,
                min(300, 2 ** job["attempt_count"]),
                permanent,
            )
            log.warning({"event": "admin_job_failed", "job_id": job["id"], **error_fields(error)})
        return True

    async def execute(self, effect):
        if not await self.repository.valid(effect.id, self.owner):
            async with self.uow_factory() as uow:
                await uow.outbox.finish(effect.id, self.owner, "cancelled", "cancelled_job")
            return
        status, error, delay = "sent", None, 0
        try:
            if effect.payload.get("policy") == "management" and (
                self.management is None or not await self.management.allowed(effect)
            ):
                raise DownloadError("management_policy_changed")
            if effect.job_id:
                job = await self.repository.job_owner(effect.job_id)
                if not job or job["status"] == "cancelled":
                    raise DownloadError("cancelled")
                await self.admin.authorize(job["requested_by"], root=job["kind"] != "health")
            elif effect.payload.get("policy") != "management" and (
                effect.target_id != self.admin.main_admin_id
                or await self.admin.repository.setting("lifecycle_notifications", True) is not True
            ):
                raise DownloadError("disabled notification")
            async with asyncio.timeout(15):
                await self.delivery.deliver(effect)
        except DownloadError:
            status, error = "cancelled", "authorization_revoked"
        except PermanentEffect as failure:
            status, error = "dead", failure.code
        except RetryEffect as failure:
            status, error, delay = "pending", failure.code, min(86400, max(1, failure.seconds))
        except Exception as failure:
            status, error, delay = (
                "pending",
                type(failure).__name__,
                min(300, 2**effect.attempt_count),
            )
            log.warning(
                {"event": "outbox_delivery_failed", "effect_id": effect.id, **error_fields(failure)}
            )
        if effect.attempt_count >= 6 and status == "pending" and error != "flood_wait":
            status = "dead"
        async with self.uow_factory() as uow:
            await uow.outbox.finish(effect.id, self.owner, status, error, delay)
        log.info(
            {
                "event": "outbox_outcome",
                "effect_id": effect.id,
                "job_id": effect.job_id,
                "status": status,
                "reason": error,
            }
        )

    async def tick(self):
        busy = await self.enumerate()
        effects = await self.repository.claim(self.owner)
        await asyncio.gather(*(self.execute(effect) for effect in effects))
        await self.repository.settle_jobs()
        return bool(busy or effects)
