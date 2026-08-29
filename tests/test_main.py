from __future__ import annotations

import asyncio
import unittest

from src.main import _supervise_runtime


class FakeDispatcher:
    def __init__(self, *, polling_error: BaseException | None = None) -> None:
        self.polling_error = polling_error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stop_calls = 0

    async def start_polling(self, bot: object) -> None:
        del bot
        self.started.set()
        if self.polling_error is not None:
            raise self.polling_error
        await self.release.wait()

    async def stop_polling(self) -> None:
        self.stop_calls += 1
        self.release.set()


class FakeMonitor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.shutdown_calls = 0

    async def run(self, stop: asyncio.Event) -> None:
        self.started.set()
        await stop.wait()

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class RuntimeSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_signal_closes_polling_and_monitor(self) -> None:
        dispatcher = FakeDispatcher()
        monitor = FakeMonitor()
        stop = asyncio.Event()
        runtime = asyncio.create_task(
            _supervise_runtime(
                dispatcher=dispatcher,  # type: ignore[arg-type]
                bot=object(),  # type: ignore[arg-type]
                monitor=monitor,  # type: ignore[arg-type]
                stop=stop,
            )
        )
        await dispatcher.started.wait()
        await monitor.started.wait()

        stop.set()
        await asyncio.wait_for(runtime, timeout=2)

        self.assertGreaterEqual(dispatcher.stop_calls, 1)
        self.assertGreaterEqual(monitor.shutdown_calls, 1)
        self._assert_no_runtime_tasks()

    async def test_polling_exit_stops_monitor(self) -> None:
        dispatcher = FakeDispatcher()
        monitor = FakeMonitor()
        stop = asyncio.Event()
        runtime = asyncio.create_task(
            _supervise_runtime(
                dispatcher=dispatcher,  # type: ignore[arg-type]
                bot=object(),  # type: ignore[arg-type]
                monitor=monitor,  # type: ignore[arg-type]
                stop=stop,
            )
        )
        await dispatcher.started.wait()
        await monitor.started.wait()

        dispatcher.release.set()
        await asyncio.wait_for(runtime, timeout=2)

        self.assertTrue(stop.is_set())
        self.assertGreaterEqual(monitor.shutdown_calls, 1)
        self._assert_no_runtime_tasks()

    async def test_polling_failure_is_propagated_after_cleanup(self) -> None:
        dispatcher = FakeDispatcher(polling_error=RuntimeError("polling failed"))
        monitor = FakeMonitor()
        stop = asyncio.Event()

        with self.assertRaisesRegex(RuntimeError, "polling failed"):
            await _supervise_runtime(
                dispatcher=dispatcher,  # type: ignore[arg-type]
                bot=object(),  # type: ignore[arg-type]
                monitor=monitor,  # type: ignore[arg-type]
                stop=stop,
            )

        self.assertTrue(stop.is_set())
        self.assertGreaterEqual(monitor.shutdown_calls, 1)
        self._assert_no_runtime_tasks()

    def _assert_no_runtime_tasks(self) -> None:
        names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done()
        }
        self.assertFalse(
            names.intersection(
                {"telegram-polling", "slot-monitor", "shutdown-signal"}
            )
        )


if __name__ == "__main__":
    unittest.main()
