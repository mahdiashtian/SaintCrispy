import asyncio

import pytest

from downloader_bot.jobs import TransferJobs
from downloader_bot.models import DownloadError
from downloader_bot.progress import TransferProgress


async def wait_empty(jobs):
    async with asyncio.timeout(2):
        await jobs.join()


async def test_thousand_jobs_start_immediately_without_a_worker_queue():
    gate = asyncio.Event()
    started = asyncio.Event()
    active = peak = finished = 0

    async def work():
        nonlocal active, peak, finished
        active += 1
        peak = max(peak, active)
        if active == 1000:
            started.set()
        try:
            await gate.wait()
            await asyncio.sleep(0)
            finished += 1
        finally:
            active -= 1

    async with TransferJobs(capacity=1000) as jobs:
        batch = []
        for _ in range(1000):
            job = jobs.reserve(1, 2, TransferProgress())
            jobs.start(job, work)
            batch.append(job)
        with pytest.raises(DownloadError, match="سقف"):
            jobs.reserve(1, 2, TransferProgress())
        await asyncio.wait_for(started.wait(), 1)
        assert active == peak == jobs.active_count == 1000
        assert jobs.count == 1000
        assert jobs.cancel(batch[-1].token, 1, 2)
        await asyncio.gather(batch[-1].task, return_exceptions=True)
        await asyncio.sleep(0)
        replacement = jobs.reserve(1, 2, TransferProgress())
        jobs.start(replacement, work)
        gate.set()
        await wait_empty(jobs)
    assert finished == 1000 and peak == 1000 and active == 0


async def test_cancel_active_job_keeps_capacity_until_cleanup_finishes():
    started, cleaned, next_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def next_work():
        next_done.set()

    async with TransferJobs(capacity=2) as jobs:
        active = jobs.reserve(1, 2, TransferProgress())
        jobs.start(active, blocked)
        queued = jobs.reserve(1, 2, TransferProgress())
        jobs.start(queued, next_work)
        await asyncio.wait_for(started.wait(), 1)
        assert jobs.cancel(active.token, 1, 2)
        await asyncio.wait_for(next_done.wait(), 1)
        await wait_empty(jobs)
        assert cleaned.is_set() and active.progress.phase == "cancelled"


async def test_cancelled_prepared_job_cannot_be_enqueued_and_owner_chat_are_checked():
    jobs = TransferJobs(capacity=2)
    job = jobs.reserve(1, 2, TransferProgress())
    for owner, chat in ((3, 2), (1, 4)):
        with pytest.raises(DownloadError, match="درخواست تو"):
            jobs.cancel(job.token, owner, chat)
    assert jobs.count == 1
    assert jobs.cancel(job.token, 1, 2)
    assert jobs.count == 0
    assert not jobs.start(job, lambda: asyncio.sleep(0))
    with pytest.raises(DownloadError, match="پایان"):
        jobs.cancel(job.token, 1, 2)


@pytest.mark.parametrize("phase", ["publishing", "saving"])
async def test_stop_does_not_interrupt_publication_or_persistence(phase):
    ready, finish = asyncio.Event(), asyncio.Event()
    progress = TransferProgress()

    async def work():
        progress.phase = phase
        ready.set()
        await finish.wait()
        progress.phase = "done"

    async with TransferJobs() as jobs:
        job = jobs.reserve(1, 2, progress)
        jobs.start(job, work)
        await asyncio.wait_for(ready.wait(), 1)
        assert not jobs.cancel(job.token, 1, 2)
        assert not job.task.cancelling()
        finish.set()
        await wait_empty(jobs)
    assert progress.phase == "done"


async def test_shutdown_does_not_cancel_cleanup_a_second_time():
    ready, cleaning, finish_cleanup, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        try:
            ready.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish_cleanup.wait()
            cleaned.set()

    jobs = TransferJobs()
    await jobs.__aenter__()
    job = jobs.reserve(1, 2, TransferProgress())
    jobs.start(job, work)
    await asyncio.wait_for(ready.wait(), 1)
    jobs.cancel(job.token, 1, 2)
    await asyncio.wait_for(cleaning.wait(), 1)
    closing = asyncio.create_task(jobs.__aexit__(None, None, None))
    await asyncio.sleep(0)
    assert not closing.done()
    finish_cleanup.set()
    await asyncio.wait_for(closing, 1)
    assert cleaned.is_set() and job.task.done()
    assert jobs.count == 0


async def test_a_failed_job_does_not_keep_its_capacity_slot():
    complete = asyncio.Event()

    async def fail():
        raise ValueError("not exposed")

    async with TransferJobs(capacity=2) as jobs:
        failed = jobs.reserve(1, 2, TransferProgress())
        jobs.start(failed, fail)
        next_job = jobs.reserve(1, 2, TransferProgress())

        async def success():
            complete.set()

        jobs.start(next_job, success)
        await asyncio.wait_for(complete.wait(), 1)
        await wait_empty(jobs)
    assert failed.progress.phase == "error"
    assert "not exposed" not in failed.progress.error


async def test_independent_task_finishes_while_a_download_is_busy():
    started, release, cached_done = (asyncio.Event() for _ in range(3))

    async def download():
        started.set()
        await release.wait()

    async with TransferJobs(capacity=1000) as jobs:
        job = jobs.reserve(1, 1, TransferProgress())
        jobs.start(job, download)
        await asyncio.wait_for(started.wait(), 1)
        cached = jobs.reserve(2, 2, TransferProgress())

        async def reuse():
            cached_done.set()

        jobs.start(cached, reuse)
        await asyncio.wait_for(cached_done.wait(), 1)
        assert not job.task.done()
        release.set()
        await wait_empty(jobs)


async def test_users_do_not_take_turns_and_every_task_starts_before_completion():
    gate, started = asyncio.Event(), asyncio.Event()
    owners = []
    async with TransferJobs(capacity=1000) as jobs:
        for owner in (1, 1, 1, 2, 2):
            job = jobs.reserve(owner, owner, TransferProgress())

            async def work(owner=owner):
                owners.append(owner)
                if len(owners) == 5:
                    started.set()
                await gate.wait()

            jobs.start(job, work)
        await asyncio.wait_for(started.wait(), 1)
        assert owners == [1, 1, 1, 2, 2] and jobs.active_count == 5
        gate.set()
        await wait_empty(jobs)


async def test_cancellation_before_first_task_step_releases_capacity():
    jobs = TransferJobs(capacity=1)
    job = jobs.reserve(1, 2, TransferProgress())

    async def work():
        pytest.fail("A task stopped before its first step must not run")

    jobs.start(job, work)
    assert jobs.cancel(job.token, 1, 2)
    await asyncio.wait_for(jobs.join(), 1)
    assert jobs.count == 0 and job.task.cancelled()


async def test_second_start_is_rejected():
    async with TransferJobs(capacity=1) as jobs:
        job = jobs.reserve(1, 2, TransferProgress())
        jobs.start(job, lambda: asyncio.sleep(0))
        with pytest.raises(ValueError, match="once"):
            jobs.start(job, lambda: asyncio.sleep(0))
        await jobs.join()
