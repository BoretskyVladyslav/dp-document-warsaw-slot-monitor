from aiogram import Dispatcher, Router

from src.handlers.admin import build_admin_router
from src.handlers.commands import router as user_router


def setup_dispatcher(dp: Dispatcher, *, admin_ids: frozenset[int]) -> Router:
    dp.include_router(user_router)
    dp.include_router(build_admin_router(admin_ids))
    return user_router
