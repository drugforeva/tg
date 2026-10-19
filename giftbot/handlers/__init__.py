"""Хендлеры бота. Порядок важен: ввод текста в редакторе стоит раньше остальных текстовых хендлеров."""

from aiogram import Dispatcher

from . import accounts, admin, channels, info, inline, menu, premium, subscription, watch, webapp

ROUTERS = [
    menu.router,
    accounts.router,
    subscription.router,
    info.router,
    watch.router,
    channels.router,
    premium.router,
    inline.router,
    webapp.router,
    admin.router,
]


def setup(dp: Dispatcher) -> None:
    for router in ROUTERS:
        router._parent_router = None  # позволяет собрать диспетчер повторно (тесты)
        dp.include_router(router)
