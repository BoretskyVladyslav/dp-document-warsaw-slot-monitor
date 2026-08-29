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


async def _amain(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.connect()
    scraper: SlotScraper | None = None
    bot: Bot | None = None
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
        )
        monitor = SlotMonitor(
            settings=settings,
            database=database,
            scraper=scraper,
            notifier=notifier,
            started_at=started_at,
        )
        await monitor.restore_state()
        dp = Dispatcher()
        dp["subscription_service"] = SubscriptionService(database)
        dp["status_service"] = StatusService(monitor)
        dp["city_name"] = settings.city_name
        setup_dispatcher(dp, admin_ids=frozenset(settings.admin_ids))

        stop = asyncio.Event()
        _install_signal_handlers(stop)

        async def _watch_stop() -> None:
            await stop.wait()
            logger.info("shutdown_requested")
            await dp.stop_polling()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dp.start_polling(bot))
            tg.create_task(monitor.run(stop))
            tg.create_task(_watch_stop())
    finally:
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
            "headless": settings.headless,
        },
    )
    await _amain(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
