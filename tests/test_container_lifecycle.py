"""Early startup failures release every resource, including cancellation."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from container import Container


@pytest.mark.parametrize(
    "failure", ["logging", "storage", "telegram", "providers", "application", None]
)
@pytest.mark.parametrize("cancelled", [False, True])
async def test_startup_failure_closes_completed_stages_in_reverse_order(failure, cancelled):
    trace = []

    @asynccontextmanager
    async def resource(stage):
        trace.append(("open", stage))
        try:
            yield
        finally:
            trace.append(("close", stage))

    class TestContainer(Container):
        async def step(self, stage):
            await self._stack.enter_async_context(resource(stage))
            if stage == failure:
                if cancelled:
                    raise asyncio.CancelledError()
                raise OSError("Stage failed")

        async def _configure_logging(self):
            await self.step("logging")
            return None

        async def _connect_storage(self, *args):
            await self.step("storage")
            return None, None, None, None

        async def _connect_telegram(self, *args):
            await self.step("telegram")
            return None, None

        async def _create_downloaders(self, *args):
            await self.step("providers")

        async def _configure_application(self, *args):
            await self.step("application")

    container = TestContainer(SimpleNamespace(bot_token="123:placeholder"))

    async def run():
        async with container:
            assert len(trace) == 5 and all(action == "open" for action, _ in trace)

    if failure:
        with pytest.raises(asyncio.CancelledError if cancelled else OSError):
            await run()
    else:
        await run()
    opened = [stage for action, stage in trace if action == "open"]
    closed = [stage for action, stage in trace if action == "close"]
    assert opened == list(reversed(closed))
    with pytest.raises(RuntimeError, match="new container"):
        await container.__aenter__()
