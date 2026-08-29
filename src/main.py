from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher

from src.core.config import Settings, get_settings
from src.core.logging import setup_logging
from src.database.connection import Database
from src.database.schema import init_schema
from src.handlers import setup_dispatcher
from src.handlers.sender import AiogramMessageSender
from src.services.monitor import SlotMonitor
from src.services.notifier import Notifier
from src.services.scraper import SlotScraper
from src.services.status import StatusService
from src.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    def _request_stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: _request_stop())


async def _request_polling_stop(
    dispatcher: Dispatcher,
    polling_task: asyncio.Task[None],
) -> None:
    if polling_task.done():
        return
    try:
        await asyncio.wait_for(dispatcher.stop_polling(), timeout=5)
    except RuntimeError as exc:
        logger.warning("polling_stop_skipped", extra={"error": str(exc)})
    except TimeoutError:
        logger.warning("polling_stop_timeout")


async def _supervise_runtime(
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    monitor: SlotMonitor,
    stop: asyncio.Event,
) -> None:
    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot),
        name="telegram-polling",
    )
    monitor_task = asyncio.create_task(
        monitor.run(stop),
        name="slot-monitor",
    )
    stop_task = asyncio.create_task(stop.wait(), name="shutdown-signal")
    tasks = (polling_task, monitor_task, stop_task)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        await _request_polling_stop(dispatcher, polling_task)
        await monitor.shutdown()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                raise result
    finally:
        stop.set()
        await monitor.shutdown()
        await _request_polling_stop(dispatcher, polling_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _amain(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.connect()
    scraper: SlotScraper | None = None
    bot: Bot | None = None
    monitor: SlotMonitor | None = None
    try:
        await init_schema(database.connection)
        started_at = datetime.now(timezone.utc)
        scraper = SlotScraper(settings)
        bot = Bot(token=settings.bot_token)
        sender = AiogramMessageSender(bot)
        notifier = Notifier(
            database=database,
            sender=sender,
            city_name=settings.city_name,
            target_url=str(settings.target_url),
            admin_ids=settings.admin_ids,
        )
        monitor = SlotMonitor(
            settings=settings,
            database=database,
            scraper=scraper,
            notifier=notifier,
            started_at=started_at,
        )
        await monitor.restore_state()
        if settings.check_once:
            await monitor.run_once()
            return
        dp = Dispatcher()
        dp["subscription_service"] = SubscriptionService(database)
        dp["status_service"] = StatusService(monitor)
        dp["city_name"] = settings.city_name
        setup_dispatcher(dp, admin_ids=frozenset(settings.admin_ids))

        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await _supervise_runtime(
            dispatcher=dp,
            bot=bot,
            monitor=monitor,
            stop=stop,
        )
    finally:
        if monitor is not None:
            await monitor.shutdown()
        if scraper is not None:
            await scraper.stop()
        if bot is not None:
            await bot.session.close()
        await database.close()
        logger.info("shutdown_complete")


async def main() -> None:
    setup_logging()
    settings = get_settings()
    logger.info(
        "starting",
        extra={
            "city": settings.city_name,
            "interval": settings.check_interval_seconds,
            "cdp_url": settings.cdp_url,
            "check_once": settings.check_once,
        },
    )
    await _amain(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
